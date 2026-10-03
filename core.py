"""Account storage and atomic manual switching used by the desktop and runner."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import accounts
HERE = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def running_claude():
    code = "@(Get-Process -Name claude -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id) | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', code],
        capture_output=True, encoding='utf-8', timeout=15, creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError('无法检查 Claude Code 进程，未执行切换。')
    data = json.loads(result.stdout or '[]')
    return data if isinstance(data, list) else [data]


class Store:
    def __init__(self, root=accounts.ROOT, live=accounts.LIVE, config=None, process_check=running_claude):
        self.root = Path(root)
        self.live = Path(live)
        self.config = Path(config) if config else Path.home() / '.claude.json'
        self.process_check = process_check
        self.root.mkdir(parents=True, exist_ok=True)

    def init_first(self):
        target = self.root / 'max-1'
        if not target.exists():
            target.mkdir()
            write(target / '.claude.json', {'oauthAccount': read(self.config)['oauthAccount']})
            write(target / '.credentials.json', read(self.live / '.credentials.json'))

    def recover(self):
        pending = self.root / 'pending-switch.json'
        if pending.exists():
            if self.process_check():
                raise RuntimeError('上次切换未完成，且 Claude 正在运行。请退出 Claude 后重新打开工具以恢复原账号。')
            write(self.live / '.credentials.json', read(self.root / 'switch-backup/credentials.json'))
            write(self.config, read(self.root / 'switch-backup/config.json'))
            pending.unlink()

    def profile(self, slot):
        return self.root / f'max-{slot}'

    def slots(self):
        return sorted(int(p.name[4:]) for p in self.root.iterdir()
            if p.is_dir() and p.name.startswith('max-') and p.name[4:].isdigit()
            and (p / '.claude.json').is_file() and (p / '.credentials.json').is_file())

    def new_enrollment(self):
        path = self.root / ('.enroll-' + uuid.uuid4().hex)
        path.mkdir()
        return path

    def finish_enrollment(self, path):
        path = Path(path).resolve()
        if path.parent != self.root.resolve() or not path.name.startswith('.enroll-'):
            raise RuntimeError('授权目录不属于本工具。')
        identity = read(path / '.claude.json')['oauthAccount']
        credentials = read(path / '.credentials.json')['claudeAiOauth']
        if not identity.get('accountUuid') or not identity.get('emailAddress') or not credentials.get('accessToken'):
            raise RuntimeError('官方授权信息不完整，请重新增加账号。')
        if credentials.get('subscriptionType') != 'max':
            raise RuntimeError('该账号未被识别为 Max，未加入账号列表。')
        for slot in self.slots():
            saved = self.identity(slot)
            if saved['accountUuid'] == identity['accountUuid'] or saved['emailAddress'].casefold() == identity['emailAddress'].casefold():
                raise RuntimeError('这个账号已经在列表中，未重复添加。需要更新授权时请点击原卡片的“重新授权”。')
        slot = max(self.slots(), default=0) + 1
        target = self.profile(slot)
        if target.exists() or target.resolve().parent != self.root.resolve():
            raise RuntimeError('账号保存目录已存在，未覆盖。')
        path.rename(target)
        return slot

    def discard_enrollment(self, path):
        path = Path(path).resolve()
        if path.parent != self.root.resolve() or not path.name.startswith('.enroll-'):
            raise RuntimeError('未清理：授权目录不属于本工具。')
        if path.exists():
            shutil.rmtree(path)

    def identity(self, slot):
        return read(self.profile(slot) / '.claude.json')['oauthAccount']

    def current(self):
        return read(self.config).get('oauthAccount', {}).get('accountUuid')

    def credential_path(self, slot):
        if self.identity(slot)['accountUuid'] == self.current():
            return self.live / '.credentials.json'
        return self.profile(slot) / '.credentials.json'

    def quota(self, slot):
        identity = self.identity(slot)
        cred = read(self.credential_path(slot))['claudeAiOauth']
        result = {'slot': slot, 'email': identity['emailAddress'],
            'current': identity['accountUuid'] == self.current(),
            'plan': cred.get('subscriptionType', '未知'), 'checked': time.time()}
        expires = cred.get('expiresAt')
        if expires and expires / 1000 < time.time():
            result['error'] = '登录令牌已过期，请重新授权；运行中的 CLI 刷新后也可重试'
            return result
        req = urllib.request.Request('https://api.anthropic.com/api/oauth/usage', headers={
            'Authorization': 'Bearer ' + cred['accessToken'],
            'anthropic-beta': 'oauth-2025-04-20', 'User-Agent': 'claude-code/2.1.288'})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                data = json.load(response)
            # Retain only quota fields, never headers or authentication values.
            result['windows'] = {key: value for key, value in data.items()
                if (key == 'five_hour' or key.startswith('seven_day')) and isinstance(value, dict)
                and 'utilization' in value}
            if not all(key in result['windows'] for key in ['five_hour', 'seven_day']):
                result['error'] = '官方返回缺少五小时或周额度，不能判断是否可用'
        except urllib.error.HTTPError as exc:
            result['error'] = {401: '登录已失效，请重新授权', 403: '官方拒绝额度查询',
                429: '查询过于频繁，稍后重试'}.get(exc.code, f'官方查询失败 HTTP {exc.code}')
        except (urllib.error.URLError, TimeoutError, ValueError):
            result['error'] = '网络超时或响应无效，稍后刷新'
        return result

    def switch(self, slot, *, allow_running=False):
        processes = self.process_check()
        if processes and not allow_running:
            raise RuntimeError('Claude Code 仍在运行（PID ' + ', '.join(map(str, processes)) +
                '）。请先在原终端保存进度并退出 Claude，再切换；本工具不会强行终止任务。')
        incoming_config = read(self.profile(slot) / '.claude.json')
        incoming_creds = read(self.profile(slot) / '.credentials.json')
        identity = incoming_config['oauthAccount']
        if identity['accountUuid'] == self.current():
            return '该账号已经是默认账号。'
        token = incoming_creds['claudeAiOauth']
        if not token.get('accessToken') or token.get('subscriptionType') != 'max':
            raise RuntimeError('目标账号缺少有效 Max 授权。')
        if token.get('expiresAt', 0) <= time.time() * 1000:
            raise RuntimeError('目标账号登录令牌已过期，请先重新授权。')
        original_config = read(self.config)
        original_creds = read(self.live / '.credentials.json')
        switched_creds = {**original_creds, 'claudeAiOauth': incoming_creds['claudeAiOauth']}
        old_uuid = original_config['oauthAccount']['accountUuid']
        old_slot = next((s for s in self.slots() if self.identity(s)['accountUuid'] == old_uuid), None)
        if old_slot is None:
            raise RuntimeError('当前登录账号不在账号列表中，未覆盖未知账号。')
        # Preserve the latest official token refresh before switching away.
        write(self.profile(old_slot) / '.credentials.json', original_creds)
        saved = read(self.profile(old_slot) / '.claude.json')
        saved['oauthAccount'] = original_config['oauthAccount']
        write(self.profile(old_slot) / '.claude.json', saved)
        backup = self.root / 'switch-backup'
        write(backup / 'config.json', original_config)
        write(backup / 'credentials.json', original_creds)
        updated = dict(original_config)
        updated['oauthAccount'] = identity
        # These are account-specific cached server responses, not user settings.
        for key in ['cachedExtraUsageDisabledReason', 'hasAvailableSubscription',
                    'passesEligibilityCache', 'clientDataCacheSlots', 'promoStartupStatusCache',
                    'modelAccessCache', 'orgModelDefaultCache', 'cachedArtifactRoster']:
            updated.pop(key, None)
        if self.process_check() and not allow_running:
            raise RuntimeError('切换前发现新启动的 Claude Code，已取消切换。')
        pending = self.root / 'pending-switch.json'
        write(pending, {'from': old_slot, 'to': slot})
        try:
            write(self.live / '.credentials.json', switched_creds)
            write(self.config, updated)
            env = accounts.environment(1)
            env.pop('CLAUDE_CONFIG_DIR', None)
            if self.live != accounts.LIVE:
                env['CLAUDE_CONFIG_DIR'] = str(self.live)
            validation = subprocess.run([str(accounts.CLI), 'auth', 'status', '--json'],
                cwd=str(HERE), env=env, capture_output=True, text=True, encoding='utf-8',
                timeout=30, creationflags=NO_WINDOW)
            status = json.loads(validation.stdout)
            if validation.returncode or not status.get('loggedIn') or status.get('email') != identity['emailAddress']:
                raise RuntimeError('官方 CLI 未确认目标账号，已恢复原账号。')
            pending.unlink()
        except Exception:
            write(self.live / '.credentials.json', original_creds)
            write(self.config, original_config)
            pending.unlink(missing_ok=True)
            raise
        return ('已切换到 ' + identity['emailAddress'] +
                ('。运行中会话的后续请求将读取新凭据；已发出的请求不变，限额后的消息需重新提交。' if allow_running
                 else '。新启动的 Claude Code 将使用此账号。'))
