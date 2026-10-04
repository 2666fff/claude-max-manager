"""Explicit live validation of credential reload in ONE isolated CLI process."""
import argparse
from contextlib import contextmanager, ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

import accounts
from core import read, write
from enhanced import Store


@contextmanager
def observe_requests():
    """Forward real API traffic on loopback; retain only message auth digests."""
    digests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Request paths, headers and bodies never enter a log.

        def forward(self):
            payload = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            headers = {key: value for key, value in self.headers.items()
                       if key.casefold() not in ('host', 'connection', 'content-length')}
            if self.command == 'POST' and self.path.split('?', 1)[0] == '/v1/messages':
                digests.append(hashlib.sha256(self.headers.get('Authorization', '').encode()).hexdigest())
            request = urllib.request.Request('https://api.anthropic.com' + self.path,
                data=payload if self.command == 'POST' else None, headers=headers, method=self.command)
            try:
                upstream = urllib.request.urlopen(request, timeout=90)
            except urllib.error.HTTPError as exc:
                upstream = exc  # Preserve the real service error for the CLI.
            except (urllib.error.URLError, TimeoutError):
                self.send_error(502, 'Official API transport failed')
                return
            with upstream:
                body = upstream.read()
                self.send_response(upstream.getcode())
                for key, value in upstream.headers.items():
                    if key.casefold() not in ('connection', 'transfer-encoding', 'content-length'):
                        self.send_header(key, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        do_POST = forward
        do_GET = forward

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', digests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--available-slot', type=int, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--exhausted-slot', type=int)
    target.add_argument('--alternate-slot', type=int,
                        help='Use a second valid login and verify outgoing OAuth credentials through loopback.')
    parser.add_argument('--during-tool', action='store_true',
                        help='Also prove adoption within a turn, during an isolated sleep tool call.')
    args = parser.parse_args()
    available = args.available_slot
    exhausted = args.alternate_slot if args.alternate_slot is not None else args.exhausted_slot
    if available == exhausted or min(available, exhausted) < 1:
        raise ValueError('Select two different positive slots.')
    if args.alternate_slot and args.during_tool:
        raise ValueError('Use --exhausted-slot for the during-tool scenario.')
    protected = [Path.home()/'.claude.json', accounts.LIVE/'.credentials.json']
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    source = Store()
    with tempfile.TemporaryDirectory(prefix='claude-hot-validation-') as temporary, ExitStack() as stack:
        base = Path(temporary)
        live, root, cwd = base/'live', base/'accounts', base/'project'
        cwd.mkdir()
        for slot in (available, exhausted):
            # The default CLI may have renewed its token since the profile copy.
            write(root/f'max-{slot}'/'.credentials.json', read(source.credential_path(slot)))
            config = source.config if source.identity(slot)['accountUuid'] == source.current() else source.profile(slot)/'.claude.json'
            write(root/f'max-{slot}'/'.claude.json', read(config))
        for name in ('.credentials.json', '.claude.json'):
            write(live/name, read(root/f'max-{available}'/name))
        store = Store(root, live, live/'.claude.json', process_check=lambda: [])
        write(base/'mcp.json', {'mcpServers': {}})
        session = str(uuid.uuid4())
        env = accounts.environment(available)
        env['CLAUDE_CONFIG_DIR'] = str(live)
        observed = None
        if args.alternate_slot:
            endpoint, observed = stack.enter_context(observe_requests())
            env['ANTHROPIC_BASE_URL'] = endpoint
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
        def turn(label, prompt='Reply exactly OK. Do not use tools.', on_tool=None, slot=None):
            if child.poll() is not None:
                raise RuntimeError('CLI exited before turn '+label)
            if observed is not None:
                observed.clear()
                bearer = 'Bearer ' + read(store.credential_path(slot))['claudeAiOauth']['accessToken']
                expected_digest = hashlib.sha256(bearer.encode()).hexdigest()
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
                    if observed is not None:
                        assert observed and all(d == expected_digest for d in observed), 'Unexpected OAuth credential on real message request'
                        print(f'Real message requests matched expected slot {slot}: {len(observed)}; no token recorded.', flush=True)
                    return verdict
            raise TimeoutError('No terminal response for '+label)
        def activate(slot):
            # Exercise the same public backend called by the desktop interface.
            store.switch(slot, allow_running=True)
            time.sleep(2)  # Allow the CLI file watcher to observe an atomic replace.
        try:
            assert turn('available before switch', slot=available) == 'success'
            if args.alternate_slot:
                activate(exhausted)
                assert turn('alternate after live switch', slot=exhausted) in ('success', 'limit')
            elif args.during_tool:
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
            assert turn('available after second live switch', slot=available) == 'success'
            assert child.poll() is None, 'Same process must remain alive'
            print('PASS: ' + ('two real OAuth credentials adopted' if observed is not None else 'success -> rate_limit -> success')
                  + ' with unchanged PID and session.', flush=True)
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.terminate()  # Only our isolated, tools-disabled validation process.
                child.wait(timeout=10)
    after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    print('Global configuration unchanged:', before == after, flush=True)
    assert before == after, 'Global configuration changed during isolated validation'


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
