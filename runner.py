"""Run one explicitly selected Claude session, rotating only after a limit exit."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid

import accounts
from core import read, write, HERE, running_claude
from enhanced import Store


def classify(messages, returncode):
    result = next((m for m in reversed(messages) if m.get('type') == 'result'), None)
    if result and not result.get('is_error') and returncode == 0:
        return 'complete'
    # Only structured terminal failures trigger rotation; ordinary prose never does.
    if any(m.get('type') == 'assistant' and m.get('error') == 'rate_limit' for m in messages):
        return 'limit'
    if result and result.get('is_error'):
        errors = result.get('errors', [])
        if any(isinstance(e, dict) and e.get('type') in ('rate_limit_error', 'rate_limit') for e in errors):
            return 'limit'
    return 'error'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cwd', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--prompt-file', required=True)
    parser.add_argument('--permission-mode', choices=['default', 'acceptEdits', 'auto'], default='auto')
    args = parser.parse_args()
    uuid.UUID(args.session)
    cwd = Path(args.cwd).resolve(strict=True)
    prompt_file = Path(args.prompt_file).resolve(strict=True)
    prompt = prompt_file.read_text(encoding='utf-8')
    store = Store()
    stopfile = store.root / 'runner.stop'
    statefile = store.root / 'runner.json'
    if prompt_file.parent != store.root.resolve() or not prompt_file.name.startswith('task-'):
        raise RuntimeError('任务指令文件必须位于本工具的账号数据目录。')
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, 'Local\\ClaudeMaxRunner')
    if kernel.GetLastError() == 183:
        prompt_file.unlink(missing_ok=True)
        raise RuntimeError('已有受管任务运行中。')
    stopfile.unlink(missing_ok=True)

    def state(status, **fields):
        write(statefile, {'status': status, 'pid': os.getpid(), 'session': args.session,
                         'cwd': str(cwd), 'updated': time.time(), **fields})
        print(status, flush=True)

    attempts = 0
    failed_slots = {}  # Server rejection overrides even a temporarily stale usage response.
    resume = args.resume
    child = None
    try:
        while not stopfile.exists():
            processes = running_claude()
            if processes:
                state('等待其他 Claude Code 退出', processes=processes)
                for _ in range(10):
                    if stopfile.exists():
                        break
                    time.sleep(1)
                continue
            rows = [store.quota(slot) for slot in store.slots()]
            excluded = [slot for slot, until in failed_slots.items() if until > time.time()]
            target = store.choose(rows, exclude=excluded)
            if target is None:
                state('等待可用账号额度恢复')
                for _ in range(30):
                    if stopfile.exists():
                        break
                    time.sleep(1)
                continue
            if store.identity(target)['accountUuid'] != store.current():
                store.switch(target)
            state('启动受管会话', slot=target)
            env = accounts.environment(1)
            env.pop('CLAUDE_CONFIG_DIR', None)
            if store.live != accounts.LIVE:
                env['CLAUDE_CONFIG_DIR'] = str(store.live)
            command = [str(accounts.CLI), '-p', '--output-format', 'stream-json', '--verbose',
                       '--permission-mode', args.permission_mode,
                       '--resume' if resume else '--session-id', args.session]
            # Prompt through stdin, not process command-line or persistent logs.
            child = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                errors='replace', creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            child.stdin.write(prompt)
            child.stdin.close()
            events = queue.Queue()
            def reader(stream, kind):
                for line in stream:
                    events.put((kind, line))
                events.put((kind + '_closed', ''))
            threading.Thread(target=reader, args=(child.stdout, 'stdout'), daemon=True).start()
            threading.Thread(target=reader, args=(child.stderr, 'stderr'), daemon=True).start()
            terminal_messages = []
            saw_session = False
            stop_sent = False
            closed = set()
            state('运行中', slot=target, child_pid=child.pid)
            while child.poll() is None or len(closed) < 2:
                if stopfile.exists() and not stop_sent and child.poll() is None:
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                    stop_sent = True
                    state('已请求停止，等待 Claude 保存会话', slot=target, child_pid=child.pid)
                try:
                    kind, line = events.get(timeout=1)
                except queue.Empty:
                    continue
                if kind.endswith('_closed'):
                    closed.add(kind)
                    continue
                if kind == 'stderr':
                    # Visible to the owner, never saved in the event log.
                    print(line.rstrip(), file=sys.stderr, flush=True)
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get('session_id') == args.session:
                    saw_session = True
                if message.get('type') in ('result', 'assistant', 'rate_limit_event'):
                    terminal_messages.append({k: v for k, v in message.items()
                        if k in ('type', 'is_error', 'errors', 'isApiErrorMessage', 'error', 'rate_limit_info')})
                    terminal_messages = terminal_messages[-64:]
                if message.get('type') == 'assistant':
                    for content in message.get('message', {}).get('content', []):
                        if content.get('type') == 'text':
                            print(content.get('text', ''), flush=True)
            code = child.wait()
            child = None
            if stop_sent or stopfile.exists():
                state('已停止，可使用同一会话继续')
                return
            verdict = classify(terminal_messages, code)
            if verdict == 'complete':
                state('本轮完成，已停止自动提交')
                store.log('managed_turn_complete', session=args.session, slot=target)
                return
            if verdict != 'limit' or not saw_session:
                state('任务出错或需要人工处理，未重复执行', code=code)
                store.log('managed_turn_error', session=args.session, slot=target, code=code)
                return
            attempts += 1
            row = next(r for r in rows if r['slot'] == target)
            failed_slots[target] = time.time() + 300
            for message in terminal_messages:
                info = message.get('rate_limit_info', {})
                if info.get('status') == 'rejected' and isinstance(info.get('resetsAt'), (float, int)):
                    failed_slots[target] = max(failed_slots[target], info['resetsAt'])
            for window in row.get('windows', {}).values():
                if window.get('utilization', 0) >= 100 and window.get('resets_at'):
                    import datetime as dt
                    reset = dt.datetime.fromisoformat(window['resets_at'].replace('Z', '+00:00')).timestamp()
                    failed_slots[target] = max(failed_slots[target], reset)
            store.invalidate(target)
            store.log('managed_limit_exit', session=args.session, slot=target)
            state('额度中断，准备换号恢复同一会话', attempts=attempts)
            resume = True
            prompt = '继续这个会话中尚未完成的用户任务。先核对已完成操作和当前文件状态，避免重复执行。若需要用户输入或任务已完成，请明确说明并结束本轮。'
        state('已停止')
    except KeyboardInterrupt:
        if child and child.poll() is None:
            child.send_signal(signal.CTRL_BREAK_EVENT)
            child.wait()
        state('已停止')
    except Exception as exc:
        if child and child.poll() is None:
            child.send_signal(signal.CTRL_BREAK_EVENT)
            child.wait()
        state('受管任务异常，已停止自动操作', error_type=type(exc).__name__)
        print(str(exc), file=sys.stderr)
    finally:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)
        prompt_file.unlink(missing_ok=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
