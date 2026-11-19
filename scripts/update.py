"""Download, verify and install an update from the station's GitHub release."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile


APP = Path('/opt/hobo-u30')
CONFIG_DIR = Path('/etc/u30-network')
TOKEN = CONFIG_DIR / 'github-release-token'
STATE = CONFIG_DIR / 'software-update.json'
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
DEVICE_LOCK = Path('/var/lib/u30-update/device.lock')
API = 'https://api.github.com/repos/Max042004/hobo-u30/releases'
ASSET = 'hobo-u30-main.zip'
MAX_BYTES = 32 * 1024 * 1024
MAX_UNPACKED = 128 * 1024 * 1024
SYSTEMCTL = '/usr/bin/systemctl'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request(url, token, accept, limit, *, download=False):
    opener = build_opener(NoRedirect())
    headers = {'Accept': accept, 'User-Agent': 'HOBO-U30-Updater',
               'X-GitHub-Api-Version': '2022-11-28', 'Authorization': 'Bearer ' + token}
    try:
        response = opener.open(Request(url, headers=headers), timeout=25)
    except HTTPError as exc:
        try:
            target = urlsplit(exc.headers.get('Location', ''))
            if not (download and exc.code in (301, 302, 303, 307, 308)
                    and target.scheme == 'https' and target.netloc in
                    ('release-assets.githubusercontent.com', 'objects.githubusercontent.com')):
                raise ValueError(f'GitHub 回應 HTTP {exc.code}，請確認連線和唯讀憑證。') from None
        finally:
            exc.close()
        # A signed asset URL never receives the API credential.
        response = opener.open(Request(target.geturl(), headers={'User-Agent': 'HOBO-U30-Updater'}), timeout=25)
    with response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('GitHub 回傳的檔案大小超過上限。')
    return data


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){2}', value):
        raise ValueError('無法辨識軟體版本。')
    return tuple(map(int, value.split('.')))


def download_release(token, installed):
    release = json.loads(request(API + '/latest', token, 'application/vnd.github+json', 1024 * 1024))
    if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
        raise ValueError('GitHub 沒有提供正式發布版本。')
    tag = release.get('tag_name')
    if not isinstance(tag, str) or not tag.startswith('v'):
        raise ValueError('GitHub Release 版本標籤格式錯誤。')
    target = tag[1:]
    if version(target) <= version(installed):
        return None
    assets = [asset for asset in release['assets'] if asset.get('name') == ASSET and asset.get('state') == 'uploaded']
    if len(assets) != 1:
        raise ValueError('GitHub Release 缺少更新安裝 ZIP。')
    asset = assets[0]
    digest, size, asset_id = asset.get('digest'), asset.get('size'), asset.get('id')
    if (not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest)
            or type(size) is not int or not 0 < size <= MAX_BYTES
            or type(asset_id) is not int or asset_id <= 0):
        raise ValueError('GitHub Release 的檔案校驗資訊無效。')
    data = request(f'{API}/assets/{asset_id}', token, 'application/octet-stream', size, download=True)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest[7:]:
        raise ValueError('更新檔驗證失敗。')
    return target, data


def unpack(archive, destination, target):
    seen = set()
    roots = set()
    with ZipFile(archive) as package:
        members = package.infolist()
        if len(members) > 5000 or sum(member.file_size for member in members) > MAX_UNPACKED:
            raise ValueError('更新檔解壓大小超過上限。')
        for member in members:
            name = member.filename.rstrip('/')
            parts = name.split('/')
            mode = stat.S_IFMT(member.external_attr >> 16)
            if (any(part in ('', '.', '..') or ':' in part or '\\' in part for part in parts)
                    or name in seen or mode not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError('更新 ZIP 內含無效的檔案路徑。')
            seen.add(name)
            roots.add(parts[0])
            path = destination.joinpath(*PurePosixPath(name).parts)
            if member.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with package.open(member) as source, path.open('xb') as output:
                    shutil.copyfileobj(source, output)
    if len(roots) != 1:
        raise ValueError('更新 ZIP 必須包含一個安裝目錄。')
    source = destination / roots.pop()
    if ((source / 'VERSION').read_text(encoding='utf-8').strip() != target
            or not (source / 'scripts/install.py').is_file()):
        raise ValueError('更新 ZIP 與 GitHub Release 標籤不符。')
    return source


def write_state(state, **fields):
    payload = {'state': state, 'boot_id': BOOT_ID.read_text().strip(), **fields}
    with tempfile.NamedTemporaryFile(dir=STATE.parent, prefix='.software-update-', delete=False,
                                     mode='w', encoding='utf-8') as output:
        temporary = Path(output.name)
        json.dump(payload, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, STATE)


def read_token():
    with TOKEN.open('r', encoding='ascii') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != 0:
            raise ValueError('GitHub 憑證檔須由 root 擁有，權限為 0600。')
        token = stream.read(513).strip()
    if not re.fullmatch(r'[A-Za-z0-9_]{1,512}', token):
        raise ValueError('請確認 Pi 上的 GitHub 唯讀憑證。')
    return token


def install_release(source):
    import fcntl
    with DEVICE_LOCK.open('rb') as lock:
        deadline = time.monotonic() + 180
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('U30 正在備份或套用設定，請等作業完成後再更新。') from None
                time.sleep(1)
        subprocess.run(['/usr/bin/python3', '-I', '-B', str(source / 'scripts/install.py'), '--update-only'],
                       check=True)


def main():
    installed = (APP / 'VERSION').read_text().strip()
    target = None
    try:
        write_state('checking')
        release = download_release(read_token(), installed)
        if release is None:
            write_state('current')
            return
        target, data = release
        with tempfile.TemporaryDirectory(prefix='hobo-u30-update-') as directory:
            staging = Path(directory)
            archive = staging / ASSET
            archive.write_bytes(data)
            source = unpack(archive, staging / 'release', target)
            write_state('installing', version=target)
            install_release(source)
        write_state('rebooting', version=target, reboot_at=time.monotonic())
        time.sleep(3)
        subprocess.run([SYSTEMCTL, '--no-block', 'reboot'], check=True)
    except Exception as exc:
        # Network exceptions can include signed download URLs. Only report types.
        print('Software update failed:', type(exc).__name__, flush=True)
        message = str(exc) if type(exc) is ValueError else '軟體更新未完成，請查看 Pi 更新服務日誌。'
        if target and (APP / 'VERSION').is_file() and (APP / 'VERSION').read_text().strip() == target:
            message = '請檢查 Pi 的更新與重開狀態。'
        write_state('failed', error=message)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
