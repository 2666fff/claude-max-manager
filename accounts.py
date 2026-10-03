"""Enroll Max accounts through the unmodified official CLI, in isolated folders.

Does not switch the live account or resume/stop any coding session.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

HOME_DIR = Path.home()
ROOT = HOME_DIR / '.claude-max-accounts'
def locate_cli():
    explicit = os.environ.get('CLAUDE_CODE_EXECUTABLE')
    if explicit:
        return Path(explicit).expanduser()
    candidates = [HOME_DIR / '.local/bin/claude.exe',
                  Path(os.environ.get('APPDATA', str(HOME_DIR / 'AppData/Roaming'))) /
                  'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe']
    discovered = shutil.which('claude.exe')
    if discovered:
        candidates.append(Path(discovered))
    return next((p for p in candidates if p.is_file()), candidates[0])


CLI = locate_cli()
LIVE = HOME_DIR / '.claude'
AUTH_OVERRIDES = (
    'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
    'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK',
    'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
)


def profile(slot):
    saved = ROOT / ('max-' + str(slot))
    return LIVE if slot == 1 and not saved.exists() else saved


def environment(slot):
    env = os.environ.copy()
    for key in AUTH_OVERRIDES:
        env.pop(key, None)
    if profile(slot) == LIVE:
        env.pop('CLAUDE_CONFIG_DIR', None)
    else:
        env['CLAUDE_CONFIG_DIR'] = str(profile(slot))
    return env


def status(slot):
    if not profile(slot).exists():
        return {'slot': slot, 'loggedIn': False}
    result = subprocess.run(
        [str(CLI), 'auth', 'status', '--json'], env=environment(slot),
        cwd=str(Path(__file__).parent), capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=30,
    )
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError(f'账号 {slot}: 官方 CLI 未返回有效认证状态 (exit={result.returncode})')
    return {'slot': slot, **{key: data.get(key) for key in
        ['loggedIn', 'email', 'orgId', 'subscriptionType', 'configDirectory']}}


def enroll():
    ROOT.mkdir(exist_ok=True)
    known = {}
    for slot in range(1, 5):
        info = status(slot)
        if info.get('loggedIn'):
            identity = (info.get('email') or '').lower()
            if not identity:
                raise RuntimeError(f'账号 {slot} 缺少身份信息，停止以避免重复授权。')
            if identity in known:
                raise RuntimeError(f'账号 {slot} 与账号 {known[identity]} 重复；尚未完成四账号接入。')
            known[identity] = slot
            print(f'账号 {slot} 已登录: {info["email"]} ({info.get("subscriptionType")})', flush=True)
    for slot in range(2, 5):
        if status(slot).get('loggedIn'):
            continue
        profile(slot).mkdir(exist_ok=True)
        while True:
            print(f'\n现在接入 Max 账号 {slot}/4。请在官方网页选择尚未接入的账号。', flush=True)
            print('当前运行中的 Claude Code 不会退出。按 Ctrl+C 可取消。', flush=True)
            result = subprocess.run([str(CLI), 'auth', 'login', '--claudeai'],
                env=environment(slot), cwd=str(Path(__file__).parent))
            if result.returncode:
                raise RuntimeError(f'官方登录未完成 (exit={result.returncode})。重新运行此工具可接续。')
            info = status(slot)
            identity = (info.get('email') or '').lower()
            if not info.get('loggedIn') or not identity:
                raise RuntimeError('官方登录后无法确认账号身份。')
            if identity in known:
                print(f'这是已接入的账号 {known[identity]}，请重新选择另一个账号。', flush=True)
                input('按 Enter 再次打开官方登录；Ctrl+C 取消：')
                continue
            if info.get('subscriptionType') != 'max':
                raise RuntimeError('该账号未被官方 CLI 识别为 Max；请核对订阅后继续。')
            known[identity] = slot
            print(f'账号 {slot} 接入成功: {info["email"]}', flush=True)
            break
    print('\n四个账号已独立授权。自动切换尚需配置和真实验证，当前会话未改动。', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'enroll'])
    args = parser.parse_args()
    if not CLI.is_file():
        raise RuntimeError('找不到官方 Claude Code，请安装后重试，或设置 CLAUDE_CODE_EXECUTABLE 指向 claude.exe。')
    if args.command == 'status':
        print(json.dumps([status(slot) for slot in sorted({1} | {int(p.name[4:]) for p in ROOT.glob('max-*') if p.name[4:].isdigit() and (p / '.credentials.json').is_file()})], ensure_ascii=False, indent=2))
    else:
        enroll()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except KeyboardInterrupt:
        print('\n已取消，当前 Claude Code 会话保持原状。')
        sys.exit(130)
    except Exception as exc:
        print(f'错误: {exc}', file=sys.stderr)
        sys.exit(1)
