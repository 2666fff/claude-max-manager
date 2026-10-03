"""Explicit live validation of credential reload in ONE isolated CLI process."""
import argparse
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import accounts
from core import read, write
from enhanced import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--available-slot', type=int, required=True)
    parser.add_argument('--exhausted-slot', type=int, required=True)
    parser.add_argument('--during-tool', action='store_true',
                        help='Also prove adoption within a turn, during an isolated sleep tool call.')
    args = parser.parse_args()
    available, exhausted = args.available_slot, args.exhausted_slot
    if available == exhausted or min(available, exhausted) < 1:
        raise ValueError('Select two different positive slots.')
    protected = [Path.home()/'.claude.json', accounts.LIVE/'.credentials.json']
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    with tempfile.TemporaryDirectory(prefix='claude-hot-validation-') as temporary:
        base = Path(temporary)
        live, root, cwd = base/'live', base/'accounts', base/'project'
        cwd.mkdir()
        for slot in (available, exhausted):
            for name in ('.credentials.json', '.claude.json'):
                write(root/f'max-{slot}'/name, read(accounts.ROOT/f'max-{slot}'/name))
        for name in ('.credentials.json', '.claude.json'):
            write(live/name, read(root/f'max-{available}'/name))
        store = Store(root, live, live/'.claude.json', process_check=lambda: [])
        write(base/'mcp.json', {'mcpServers': {}})
        session = str(uuid.uuid4())
        env = accounts.environment(available)
        env['CLAUDE_CONFIG_DIR'] = str(live)
        command = [str(accounts.CLI), '-p', '--input-format', 'stream-json',
            '--output-format', 'stream-json', '--verbose', '--safe-mode', '--tools', 'Bash' if args.during_tool else '',
            '--strict-mcp-config', str(base/'mcp.json'), '--session-id', session]
        if args.during_tool:
            command += ['--allowedTools', 'Bash(sleep:*)']
        events = queue.Queue()
        child = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
        def reader(stream, kind):
            for line in stream:
                events.put((kind, line))
        for stream, kind in [(child.stdout, 'stdout'), (child.stderr, 'stderr')]:
            threading.Thread(target=reader, args=(stream, kind), daemon=True).start()
        print('Isolated CLI started; one PID for all turns:', child.pid, flush=True)
        store.process_check = lambda: [child.pid] if child.poll() is None else []
        def turn(label, prompt='Reply exactly OK. Do not use tools.', on_tool=None):
            if child.poll() is not None:
                raise RuntimeError('CLI exited before turn '+label)
            child.stdin.write(json.dumps({'type': 'user', 'session_id': session,
                'message': {'role': 'user', 'content': prompt}})+'\n')
            child.stdin.flush()
            deadline = time.monotonic()+90
            limit = False
            while time.monotonic() < deadline:
                try:
                    kind, line = events.get(timeout=1)
                except queue.Empty:
                    if child.poll() is not None:
                        raise RuntimeError('CLI exited during '+label)
                    continue
                if kind != 'stdout':
                    continue  # Raw stderr may contain private config; never publish it.
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'assistant' and event.get('error') == 'rate_limit':
                    limit = True
                if on_tool and event.get('type') == 'assistant':
                    for block in event.get('message', {}).get('content', []):
                        if block.get('type') == 'tool_use' and block.get('name') == 'Bash':
                            on_tool()
                            on_tool = None
                            break
                if event.get('type') == 'result':
                    assert event.get('session_id') == session, 'Session changed'
                    verdict = 'limit' if limit else ('error' if event.get('is_error') else 'success')
                    print(label+': '+verdict+'; process still running='+str(child.poll() is None), flush=True)
                    return verdict
            raise TimeoutError('No terminal response for '+label)
        def activate(slot):
            # Exercise the same public backend called by the desktop interface.
            store.switch(slot, allow_running=True)
            time.sleep(2)  # Allow the CLI file watcher to observe an atomic replace.
        try:
            assert turn('available before switch') == 'success'
            if args.during_tool:
                switched = []
                def during_tool():
                    activate(exhausted)
                    switched.append(True)
                    print('Swapped credentials after assistant requested Bash sleep, before its next inference.',flush=True)
                assert turn('exhausted within same turn',
                    'Use the Bash tool to run exactly sleep 8 once, then reply OK. Do not run any other command.',
                    during_tool) == 'limit'
                assert switched, 'Tool boundary was not exercised'
            else:
                activate(exhausted)
                assert turn('exhausted after live switch') == 'limit'
            activate(available)
            assert turn('available after second live switch') == 'success'
            assert child.poll() is None, 'Same process must remain alive'
            print('PASS: success -> rate_limit -> success with unchanged PID and session.', flush=True)
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.terminate()  # Only our isolated, tools-disabled validation process.
                child.wait(timeout=10)
    after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    print('Global configuration unchanged:', before == after, flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
