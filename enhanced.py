"""Local account lifecycle, measured quotas, and conservative rotation policy.

Protocol references (independently implemented):
realiti4/claude-swap OAuth / proper-lockfile coordination;
countzero/switch_claude_account usage backoff and identity reconciliation.
"""
from contextlib import contextmanager, ExitStack
import copy
import ctypes
import datetime as dt
import email.utils
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import accounts
from core import Store as BasicStore, read, write, HERE, NO_WINDOW


@contextmanager
def directory_lock(path, timeout=12):
    """Atomic directory acquisition; never steal another process's lock."""
    path = Path(path)
    deadline = time.monotonic() + timeout
    while True:
        try:
            path.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError('凭据正在被其他进程使用，请稍后重试；未覆盖锁。')
            time.sleep(.15)
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(3):
            try:
                os.utime(path)
            except OSError:
                return
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)
        path.rmdir()


def retry_seconds(value, now=None):
    now = time.time() if now is None else now
    try:
        return max(1, float(value))
    except (TypeError, ValueError):
        try:
            return max(1, email.utils.parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return 300


class AuthProblem(RuntimeError):
    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def runner_active():
    kernel = ctypes.windll.kernel32
    kernel.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
    kernel.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel.OpenMutexW(0x00100000, False, 'Local\\ClaudeMaxRunner')
    if not handle:
        return False
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle(handle)
    return True


def require_live_switch_support():
    """Gate live writes on the exact native CLI version exercised end to end."""
    if sys.platform != 'win32':
        raise RuntimeError('运行中切换目前只在 Windows 验证；请退出 Claude 后切换。')
    result = subprocess.run([str(accounts.CLI), '--version'], capture_output=True,
                            encoding='utf-8', timeout=15, creationflags=NO_WINDOW)
    version = result.stdout.strip().split(' ', 1)[0]
    if result.returncode or version != '2.1.288':
        raise RuntimeError('此 Claude 版本尚未验证运行中切换（已验证 2.1.288）；请退出 Claude 后切换。')


class Store(BasicStore):
    DEFAULTS = {'auto_enabled': False, 'threshold': 95, 'cooldown': 300,
                'poll_seconds': 300, 'model': '', 'min_improvement': 5}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mutex = threading.RLock()

    def settings(self):
        path = self.root / 'settings.json'
        return {**self.DEFAULTS, **(read(path) if path.exists() else {})}

    def save_settings(self, data):
        updated = {**self.settings(), **data}
        if not 50 <= int(updated['threshold']) <= 100:
            raise ValueError('切换阈值应为50–100%。')
        if not 180 <= int(updated['poll_seconds']) <= 3600:
            raise ValueError('查询间隔应为180–3600秒。')
        if not 60 <= int(updated['cooldown']) <= 3600:
            raise ValueError('切换冷却应为60–3600秒。')
        write(self.root / 'settings.json', updated)
        return updated

    def meta(self, slot):
        path = self.profile(slot) / 'manager.json'
        return {'alias': '', 'enabled': True, **(read(path) if path.exists() else {})}

    def set_meta(self, slot, **changes):
        with self.mutex:
            write(self.profile(slot) / 'manager.json', {**self.meta(slot), **changes})

    def log(self, event, **fields):
        # Only explicitly selected non-secret fields enter the log.
        row = {'at': dt.datetime.now().astimezone().isoformat(), 'event': event,
               **{k: v for k, v in fields.items() if k in ['slot', 'from_slot', 'reason', 'session', 'code']}}
        path = self.root / 'events.jsonl'
        with self.mutex:
            if path.exists() and path.stat().st_size > 1024 * 1024:
                os.replace(path, self.root / 'events.previous.jsonl')
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')

    def remove(self, slot):
        with self.mutex, directory_lock(self.root / '.manager.lock'):
            if self.identity(slot)['accountUuid'] == self.current():
                raise RuntimeError('不能移除当前默认账号，请先切换到其他账号。')
            source = self.profile(slot).resolve()
            archive = self.root / 'removed'
            archive.mkdir(exist_ok=True)
            target = archive / f'max-{slot}-{time.time_ns()}'
            if source.parent != self.root.resolve() or target.resolve().parent != archive.resolve():
                raise RuntimeError('账号路径核对失败。')
            source.rename(target)
            self.log('account_removed', slot=slot)

    def restore_removed(self):
        archive = self.root / 'removed'
        entries = sorted(archive.glob('max-*'), key=lambda p: p.stat().st_mtime) if archive.exists() else []
        if not entries:
            raise RuntimeError('没有可恢复的已移除账号。')
        with self.mutex, directory_lock(self.root / '.manager.lock'):
            source = entries[-1].resolve()
            identity = read(source / '.claude.json')['oauthAccount']
            if any(self.identity(s)['accountUuid'] == identity['accountUuid'] for s in self.slots()):
                raise RuntimeError('该账号已在列表中，未重复恢复。')
            target = self.profile(max(self.slots(), default=0) + 1)
            if source.parent != archive.resolve() or target.resolve().parent != self.root.resolve():
                raise RuntimeError('恢复路径核对失败。')
            source.rename(target)
            self.log('account_restored', slot=int(target.name[4:]))

    @contextmanager
    def credential_locks(self, folder, config=None):
        with ExitStack() as stack:
            stack.enter_context(directory_lock(folder / '.oauth_refresh.lock'))
            stack.enter_context(directory_lock(folder.with_name(folder.name + '.lock')))
            if config:
                stack.enter_context(directory_lock(config.with_name(config.name + '.lock')))
            yield

    def token(self, slot, force=False):
        with self.mutex, directory_lock(self.root / '.manager.lock'):
            current = self.identity(slot)['accountUuid'] == self.current()
            folder = self.live if current else self.profile(slot)
            # The active CLI owns refresh of its live token; it may use a newer protocol.
            if current and self.process_check():
                doc = read(folder / '.credentials.json')
                token = doc['claudeAiOauth']
                if token.get('expiresAt', 0) <= time.time() * 1000:
                    raise AuthProblem('当前 CLI 的访问令牌已过期，等待 CLI 刷新；备用账号仍可独立维护')
                saved = read(self.profile(slot) / '.credentials.json')
                saved['claudeAiOauth'] = token
                write(self.profile(slot) / '.credentials.json', saved)
                return token
            with self.credential_locks(folder):
                path = folder / '.credentials.json'
                journal = self.profile(slot) / '.refresh-result.json'
                doc = read(path)
                if journal.exists():
                    pending = read(journal)
                    fingerprint = hashlib.sha256(json.dumps(doc['claudeAiOauth'], sort_keys=True).encode()).hexdigest()
                    if fingerprint == pending['before']:
                        doc['claudeAiOauth'] = pending['oauth']
                        write(path, doc)
                    elif doc['claudeAiOauth'] != pending['oauth']:
                        raise AuthProblem('令牌恢复记录与当前凭据不一致，已保留记录等待检查')
                    saved = read(self.profile(slot) / '.credentials.json')
                    saved['claudeAiOauth'] = pending['oauth']
                    write(self.profile(slot) / '.credentials.json', saved)
                    journal.unlink()
                token = doc['claudeAiOauth']
                if not force and token.get('expiresAt', 0) > (time.time() + 300) * 1000:
                    return token
                fingerprint = hashlib.sha256(json.dumps(token, sort_keys=True).encode()).hexdigest()
                meta = self.meta(slot)
                if meta.get('refresh_retry_at', 0) > time.time():
                    raise AuthProblem('令牌刷新正在冷却，稍后自动重试')
                if meta.get('dead_token') == fingerprint:
                    raise AuthProblem('刷新授权已失效，需要重新授权', True)
                if not token.get('refreshToken') or (token.get('refreshTokenExpiresAt') and token['refreshTokenExpiresAt'] <= time.time() * 1000):
                    raise AuthProblem('刷新授权已到期，需要重新授权', True)
                payload = {'grant_type': 'refresh_token', 'client_id': token.get('clientId') or '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
                           'refresh_token': token['refreshToken']}
                if token.get('scopes'):
                    payload['scope'] = ' '.join(token['scopes'])
                request = urllib.request.Request('https://platform.claude.com/v1/oauth/token',
                    data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                    'User-Agent': 'claude-code/2.1.288', 'anthropic-beta': 'oauth-2025-04-20',
                    'anthropic-version': '2023-06-01'}, method='POST')
                try:
                    with urllib.request.urlopen(request, timeout=12) as response:
                        result = json.load(response)
                except urllib.error.HTTPError as exc:
                    try:
                        error = json.loads(exc.read()).get('error')
                    except (ValueError, AttributeError):
                        error = None
                    if exc.code in (400, 401, 403) and error == 'invalid_grant':
                        self.set_meta(slot, dead_token=fingerprint)
                        self.log('reauthorization_required', slot=slot)
                        raise AuthProblem('刷新授权被官方撤销，需要重新授权', True)
                    delay = max(300, retry_seconds(exc.headers.get('Retry-After'))) if exc.code == 429 else 300
                    self.set_meta(slot, refresh_retry_at=time.time() + delay)
                    raise AuthProblem(f'令牌刷新暂时失败 HTTP {exc.code}，稍后自动重试')
                except (urllib.error.URLError, TimeoutError, ValueError):
                    raise AuthProblem('令牌刷新网络异常，稍后自动重试')
                if not result.get('access_token') or not isinstance(result.get('expires_in'), (int, float)):
                    raise AuthProblem('官方刷新响应缺少必要字段')
                fresh = dict(token)
                fresh.update(accessToken=result['access_token'], expiresAt=int((time.time() + result['expires_in']) * 1000))
                if result.get('refresh_token'):
                    fresh['refreshToken'] = result['refresh_token']
                if result.get('scope'):
                    fresh['scopes'] = result['scope'].split()
                if isinstance(result.get('refresh_token_expires_in'), (int, float)):
                    fresh['refreshTokenExpiresAt'] = int((time.time() + result['refresh_token_expires_in']) * 1000)
                write(journal, {'before': fingerprint, 'oauth': fresh})
                doc['claudeAiOauth'] = fresh
                write(path, doc)
                saved = read(self.profile(slot) / '.credentials.json')
                saved['claudeAiOauth'] = fresh
                write(self.profile(slot) / '.credentials.json', saved)
                journal.unlink()
                self.set_meta(slot, dead_token=None, refresh_retry_at=0)
                self.log('token_refreshed', slot=slot)
                return fresh

    def quota(self, slot):
        identity = self.identity(slot)
        base = {'slot': slot, 'email': identity['emailAddress'], 'current': identity['accountUuid'] == self.current(),
                'plan': 'max', 'meta': {key: self.meta(slot)[key] for key in ('alias', 'enabled')}}
        cache_path = self.profile(slot) / 'usage-cache.json'
        cache = read(cache_path) if cache_path.exists() else {}
        if cache.get('next_poll', 0) > time.time():
            return {**cache, **base, 'cached': True}
        try:
            token = self.token(slot, force=bool(cache.get('refresh_on_retry')))
            base['auth_expires'] = token.get('refreshTokenExpiresAt')
            request = urllib.request.Request('https://api.anthropic.com/api/oauth/usage', headers={
                'Authorization': 'Bearer ' + token['accessToken'], 'anthropic-beta': 'oauth-2025-04-20'})
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.load(response)
            windows = {key: value for key, value in data.items() if
                (key == 'five_hour' or key.startswith('seven_day')) and isinstance(value, dict) and 'utilization' in value}
            for key in ['five_hour', 'seven_day']:
                value = windows.get(key, {}).get('utilization')
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                    raise ValueError('官方响应缺少有效额度窗口')
            scoped = []
            for limit in data.get('limits', []):
                model = (limit.get('scope') or {}).get('model') or {}
                if model.get('display_name') and isinstance(limit.get('percent'), (float, int)):
                    scoped.append({'name': model['display_name'], 'utilization': limit['percent'], 'resets_at': limit.get('resets_at')})
            result = {**base, 'windows': windows, 'scoped': scoped, 'checked': time.time(),
                      'next_poll': time.time() + self.settings()['poll_seconds'], 'failures': 0}
        except (AuthProblem, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as exc:
            failures = cache.get('failures', 0) + 1
            delay = min(1800, 180 * 2 ** min(failures - 1, 4))
            permanent = isinstance(exc, AuthProblem) and exc.permanent
            if isinstance(exc, urllib.error.HTTPError):
                if exc.code == 429:
                    delay = max(delay, retry_seconds(exc.headers.get('Retry-After')))
                message = {429: '额度查询限频，按官方要求等待重试', 401: '访问令牌被拒绝，稍后刷新重试',
                           403: '官方拒绝额度查询'}.get(exc.code, f'额度查询失败 HTTP {exc.code}')
            elif isinstance(exc, (AuthProblem, RuntimeError)):
                message = str(exc)
            else:
                message = '额度查询网络或响应异常，稍后重试'
            result = {**base, 'error': message, 'reauth_required': permanent, 'failures': failures,
                      'refresh_on_retry': isinstance(exc, urllib.error.HTTPError) and exc.code == 401,
                      'checked': cache.get('checked'), 'next_poll': time.time() + delay,
                      'last_good': cache.get('last_good') or ({'windows': cache['windows'], 'checked': cache['checked']} if 'windows' in cache else None)}
            self.log('quota_error', slot=slot, reason=message)
        write(cache_path, result)
        return result

    def invalidate(self, slot):
        (self.profile(slot) / 'usage-cache.json').unlink(missing_ok=True)

    def relevant(self, row):
        if row.get('error') or not row.get('checked') or time.time() - row['checked'] > 600:
            return None
        windows = row.get('windows', {})
        if not all(k in windows for k in ['five_hour', 'seven_day']):
            return None
        selected = [windows['five_hour'], windows['seven_day']]
        model = self.settings()['model'].strip().casefold()
        if model:
            selected += [w for w in row.get('scoped', []) if w['name'].casefold() == model]
            selected += [w for key, w in windows.items() if key.casefold() == 'seven_day_' + model]
        values = [w.get('utilization') for w in selected]
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) for v in values):
            return None
        return max(values)

    def choose(self, rows, exclude=()):
        usable = []
        for row in rows:
            slot = row['slot']
            level = self.relevant(row)
            if slot not in exclude and self.meta(slot)['enabled'] and level is not None and level < self.settings()['threshold']:
                usable.append((level, slot))
        return min(usable)[1] if usable else None

    def switch(self, slot, *, allow_running=False):
        # Resolve compatibility before acquiring the official file-write locks.
        if allow_running and self.process_check():
            require_live_switch_support()
            active_pids = self.process_check()
            for path in (self.live / 'sessions').glob('*.json'):
                info = read(path)
                if info.get('pid') in active_pids and info.get('version') not in (None, '2.1.288'):
                    raise RuntimeError('发现未验证版本的运行中会话，请退出该会话后切换。')
        self.token(slot)
        with self.mutex, directory_lock(self.root / '.manager.lock'), self.credential_locks(self.live, self.config):
            message = super().switch(slot, allow_running=allow_running)
            write(self.root / 'rotation.json', {'last_switch': time.time(), 'slot': slot})
            self.log('account_switched', slot=slot)
            return message

    def auto_step(self, rows):
        settings = self.settings()
        if not settings['auto_enabled']:
            return '自动模式已关闭'
        if runner_active():
            return '受管任务正在管理账号选择，桌面自动模式等待'
        current = next((r for r in rows if self.identity(r['slot'])['accountUuid'] == self.current()), None)
        if not current or self.relevant(current) is None:
            return '当前额度未知，等待有效查询结果'
        if self.relevant(current) < settings['threshold']:
            return '自动监控中，当前账号尚未达到切换阈值'
        rotation = self.root / 'rotation.json'
        if rotation.exists() and time.time() - read(rotation)['last_switch'] < settings['cooldown']:
            return '切换冷却中，避免账号来回切换'
        target = self.choose(rows, exclude=[current['slot']])
        if target is None:
            return '没有额度充足且启用的备用账号，等待额度恢复'
        if self.relevant(current) - self.relevant(next(r for r in rows if r['slot'] == target)) < settings['min_improvement']:
            return '备用额度差异较小，保持当前账号'
        self.switch(target, allow_running=True)
        return f'已自动切换到账号 {target}，后续请求使用新账号；已限额的消息需重新提交或由受管任务接续'
