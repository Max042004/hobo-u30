"""Serialized, backed-up native logger controls; Pi polling survives failures."""
import json
import logging
import time
from copy import deepcopy

from history_tools import HistoryConflict
from station import now, read_header
from u30_settings import writer_session, apply_deployment, validate_settings


class NativeControl:
    def init_native_control(self):
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key='native_control'").fetchone()
        self.native_policy = {'enabled': True, 'blocked': False, 'paused': False,
                              **(json.loads(row[0]) if row else {})}
        self.native_requested = None
        self.native_candidate = None

    def save_native_policy(self, **changes):
        policy = {**self.native_policy, **changes, 'updated_utc': now()}
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES ('native_control', ?)",
                       (json.dumps(policy, ensure_ascii=False),))
        self.native_policy = policy

    def native_status(self):
        return deepcopy(self.native_policy)

    def queue_native(self, action, serial=None, enabled=None, expected_deployment=None, expected_channels=None):
        with self.lock:
            if self.device_busy():
                raise HistoryConflict('裝置已有工作執行中')
            if action == 'policy':
                if type(enabled) is not bool:
                    raise ValueError('請指定是否自動納入新感測器')
                self.save_native_policy(enabled=enabled)
                self.native_candidate = None
                return {'saved': True}
            if action not in ('pause', 'resume'):
                raise ValueError('無效的 U30 記錄操作')
            snapshot = self.state.get('snapshot')
            last = self.state.get('last_success_utc')
            from datetime import datetime
            if (not self.state['usb_connected'] or not snapshot or not last or
                    time.time() - datetime.fromisoformat(last).timestamp() > 30):
                raise HistoryConflict('請先重新讀取 U30 最新狀態')
            if not serial or snapshot['serial'] != serial:
                raise HistoryConflict('U30 序號已變更，請重新讀取')
            if action == 'pause' and snapshot.get('status_code') not in (1, 3, 5):
                raise HistoryConflict('U30 目前已停止記錄')
            if action == 'resume':
                if snapshot.get('status_code') != 0:
                    raise HistoryConflict('U30 尚未暫停，請先暫停後再恢復')
                self.native_configuration(snapshot)
                if (expected_deployment != snapshot['archive'].get('deployment') or
                        not isinstance(expected_channels, list) or
                        expected_channels != sorted(c['key'] for c in snapshot['sensors'])):
                    raise HistoryConflict('部署或感測通道已變更，請重新預覽恢復記錄')
            self.native_requested = self.native_job(action, snapshot)
        self.wake.set()
        return {'accepted': True}

    @staticmethod
    def native_configuration(snapshot):
        archive = snapshot['archive']
        if archive.get('metadata_valid') is False or archive.get('pointer_valid') is False:
            raise HistoryConflict('記憶體狀態未確認；Pi 採集繼續，請使用全站設定的原始備份流程')
        if not archive.get('channels') or not snapshot.get('sensors'):
            raise HistoryConflict('尚無完整的感測通道資訊')
        if archive.get('sampling_interval_seconds', 0):
            raise HistoryConflict('原部署含額外平均取樣，請先使用全站採集設定')
        return validate_settings({'name': snapshot.get('name') or archive.get('name') or 'U30 Station',
                                  'interval': archive['logging_interval_seconds'],
                                  'memory_mode': 'wrap' if archive['wrap_enabled'] else 'stop',
                                  'sample_interval': 0, 'start_mode': 'immediate'})

    @staticmethod
    def native_job(action, snapshot):
        return {'action': action, 'serial': snapshot['serial'],
                'deployment': snapshot['archive'].get('deployment'),
                'channels': sorted(c['key'] for c in snapshot.get('sensors', []))}

    def observe_native(self, snapshot):
        """Only confirmed additions trigger writes. Never remap a running logger."""
        with self.lock:
            if not self.native_policy['enabled'] or self.native_policy['blocked'] or self.device_busy():
                self.native_candidate = None
                return
            archive = snapshot['archive']
            old = {c['key'] for c in archive.get('channels', [])}
            # Native headers retain disconnected sensors. During an explicit
            # pause compare against what was actually connected at pause time,
            # so reconnecting an old sensor also triggers recording recovery.
            paused_baseline = (self.native_policy.get('paused') and
                    self.native_policy.get('baseline_serial') == snapshot['serial'] and
                    self.native_policy.get('baseline_deployment') == archive.get('deployment'))
            if paused_baseline:
                old = set(self.native_policy.get('baseline_channels', []))
            current = {c['key'] for c in snapshot.get('sensors', [])}
            if (snapshot.get('status_code') not in (0, 1) or (not old and not paused_baseline) or not current - old or old - current or
                    archive.get('metadata_valid') is False or archive.get('pointer_valid') is False or
                    any(c.get('quality') != 'ok' for c in snapshot.get('sensors', []))):
                self.native_candidate = None
                return
            job = self.native_job('auto', snapshot)
            if self.native_candidate != job:
                self.native_candidate = job
                return
            try:
                self.native_configuration(snapshot)
            except (ValueError, HistoryConflict) as exc:
                self.save_native_policy(blocked=True, error=str(exc))
                return
            # Persist a latch before queueing: a crash must never replay a launch.
            self.save_native_policy(blocked=True, error='自動部署尚未完成；若作業中斷，請核對後手動恢復')
            self.native_requested = job
            self.native_candidate = None
        self.wake.set()

    def native_success(self):
        with self.lock:
            self.save_native_policy(paused=False, blocked=False, error=None,
                                    baseline_serial=None, baseline_deployment=None, baseline_channels=None)
            self.native_candidate = None

    def stop_native(self, job):
        with writer_session() as client:
            # System identity remains available even when archive metadata fails.
            system = client.transport.transact(client.host_address, 0, 1, b'', 2).payload
            if len(system) < 29 or str(int.from_bytes(system[13:17], 'big')) != job['serial']:
                raise HistoryConflict('U30 裝置已變更，未停止記錄')
            if job['action'] != 'pause':
                _, archive = read_header(client)
                if archive['deployment'] != job['deployment']:
                    raise HistoryConflict('U30 部署已變更，未停止記錄')
            client.stop_logging()

    def execute_native(self, job):
        action = job['action']
        with self.lock:
            self.state['deployment'] = {'running': True, 'phase': '正在暫停 U30 記錄' if action == 'pause' else '新感測器／恢復記錄：完整備份與設定核對中'}
            self.state['sync'] = {'running': action != 'pause', 'bytes': 0, 'total': 524288}
        try:
            if action == 'pause':
                self.stop_native(job)
                with self.lock:
                    self.save_native_policy(paused=True, baseline_serial=job['serial'],
                                            baseline_deployment=job['deployment'], baseline_channels=job['channels'])
                    self.state['deployment']['phase'] = 'U30 已暫停；Pi 繼續採集，可插入新感測器'
                return
            # Fresh read and identity checks happen BEFORE stopping or writing.
            self.reader.invalidate()
            before = self.reader.read()
            self.check_native_job(before, job)
            if action == 'resume' and before.get('status_code') != 0:
                raise HistoryConflict('U30 狀態已改變，請重新讀取後再操作')
            config = self.native_configuration(before)
            with self.lock:
                self.save_native_policy(blocked=True, error='記錄恢復流程尚未完成，請核對後再操作')
            self.stop_native(job)
            self.reader.invalidate()
            stopped = self.reader.read()  # R is issued only after O confirms stopped.
            self.check_native_job(stopped, job)
            if stopped.get('status_code') != 0:
                raise HistoryConflict('U30 未確認停止，未重新部署')
            self.native_configuration(stopped)
            result = apply_deployment(self.store.directory / 'backups', config, job['serial'],
                                      self.progress, job['channels'], job['deployment'])
            self.activate_sampling(config['interval'], job['serial'], result['archive']['deployment'])
            # Archive import cannot undo a verified hardware launch.
            try:
                self.store.import_archive(self.store.directory / 'backups' / result['backup'])
            except Exception as exc:
                result['import_error'] = str(exc)
            self.native_success()
            with self.lock:
                self.state['deployment'].update(result=result, phase='新增感測通道已寫入並核對' if action == 'auto' else 'U30 記錄已恢復並核對')
        except Exception as exc:
            with self.lock:
                try:
                    self.save_native_policy(blocked=True, error=str(exc))
                except Exception:
                    logging.getLogger('u30').exception('Could not persist native control failure')
                    self.native_policy = {**self.native_policy, 'blocked': True, 'error': str(exc)}
                self.state['deployment'].update(error=str(exc), phase='U30 操作未完成；Pi 繼續採集，不自動重試寫入')
        finally:
            self.reader.invalidate()
            with self.lock:
                self.state['deployment']['running'] = False
                self.state['sync']['running'] = False

    @staticmethod
    def check_native_job(snapshot, job):
        if (snapshot['serial'] != job['serial'] or snapshot['archive'].get('deployment') != job['deployment'] or
                sorted(c['key'] for c in snapshot.get('sensors', [])) != job['channels'] or
                any(c.get('quality') != 'ok' for c in snapshot.get('sensors', []))):
            raise HistoryConflict('U30 或感測通道已變更，未重新部署；請確認連接後再恢復')
