'use strict';
(() => {
    const t = (zh, en) => window.U30I18n?.language() === 'en' ? en : zh;
    let network;
    let pending = false;
    let follow = false;
    let error = '';

    function render() {
        const update = network?.software_update;
        $('software-update-title').textContent = t('軟體更新', 'Software update');
        $('software-update-help').textContent = t(
            '從 GitHub 安裝最新正式版本。更新成功後，Raspberry Pi 會自動重新啟動。',
            'Install the latest stable GitHub release. Raspberry Pi reboots after a successful update.');
        $('software-update-label').textContent = t('Pi 設定密碼', 'Pi setup password');
        $('software-update-submit').textContent = t('檢查並更新', 'Check and update');
        $('software-update-submit').disabled = pending || follow || !update?.available || !!network?.busy;
        $('software-update-version').textContent = update?.installed
            ? t('目前版本 v', 'Installed version v') + update.installed : '';
        let message = error;
        if (!message && update) {
            const version = update.version ? 'v' + update.version : '';
            const phases = {
                queued: t('更新已排入。', 'Update queued.'),
                checking: t('正在檢查 GitHub 正式版本。', 'Checking GitHub for a stable release.'),
                installing: t('正在安裝 ', 'Installing ') + version,
                rebooting: t('更新已安裝，Pi 即將重開。', 'Update installed. Pi is about to reboot.'),
                success: t('已更新並重新啟動，版本 ', 'Updated and restarted. Version ') + version,
                current: t('沒有可安裝的較新正式版本。', 'No newer stable release is available.'),
                failed: t(update.error || '軟體更新未完成。', 'Software update did not finish. Check the Pi update service log.')
            };
            message = phases[update.state] || '';
        }
        if (!message && !update?.available) {
            message = t('請由維護人員在 Pi 設定 GitHub 唯讀憑證以啟用更新。',
                'Ask the maintainer to configure a read-only GitHub credential on this Pi.');
        }
        notice('software-update-notice', message || null);
    }

    window.renderSoftwareUpdate = value => {
        network = value;
        if (value?.software_update) follow = !!value.software_update.busy;
        render();
    };

    $('software-update-form').addEventListener('submit', async event => {
        event.preventDefault();
        if (pending || follow || !network?.software_update?.available) return;
        pending = true;
        error = '';
        render();
        try {
            await api('/api/network/software-update', 'POST', {setup_key: $('software-update-key').value});
            follow = true;
            await loadNetwork();
        } catch (exc) {
            error = window.U30I18n?.translate(exc.message) || exc.message;
        } finally {
            $('software-update-key').value = '';
            pending = false;
            render();
        }
    });
    window.addEventListener('u30-language', render);
    window.renderSoftwareUpdate(window.u30NetworkState);
})();
