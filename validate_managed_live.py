"""Explicit live smoke test: tiny safe-mode inference in disposable config only."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch
import accounts
from core import read, write
from enhanced import Store, runner_active
import runner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exhausted-slot', type=int, required=True)
    parser.add_argument('--available-slot', type=int, required=True)
    options = parser.parse_args()
    if runner_active():
        raise RuntimeError('A managed task is already active; validation cancelled.')
    # Required profiles: a truly exhausted account and an available authorized one.
    exhausted, available = options.exhausted_slot, options.available_slot
    if exhausted == available or min(exhausted, available) < 1:
        raise ValueError('Select two different positive account slots.')
    protected = [Path.home()/'.claude.json', accounts.LIVE/'.credentials.json']
    before = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    with tempfile.TemporaryDirectory(prefix='claude-managed-validation-') as directory:
        base = Path(directory)
        live = base/'live'
        root = base/'accounts'
        cwd = base/'project'
        cwd.mkdir()
        for slot in [exhausted, available]:
            for name in ['.credentials.json','.claude.json']:
                write(root/f'max-{slot}'/name, read(accounts.ROOT/f'max-{slot}'/name))
        for name in ['.credentials.json','.claude.json']:
            write(live/name,read(root/f'max-{exhausted}'/name))
        config=read(live/'.claude.json')
        config.update(hasCompletedOnboarding=True)
        write(live/'.claude.json',config)
        store=Store(root,live,live/'.claude.json',process_check=lambda:[])
        rows={slot:store.quota(slot) for slot in [exhausted,available]}
        if store.relevant(rows[exhausted]) != 100 or store.relevant(rows[available]) is None or store.relevant(rows[available]) >= 95:
            raise RuntimeError('Live quota prerequisites are not met.')
        original_quota=store.quota
        first=True
        def quota(slot):
            nonlocal first
            if slot==exhausted and first:
                first=False
                # Inject one stale pre-request observation to exercise a REAL server rejection.
                row=copy.deepcopy(rows[slot])
                row['windows']={'five_hour':{'utilization':0},'seven_day':{'utilization':0}}
                row['scoped']=[]
                return row
            return original_quota(slot)
        original_popen=subprocess.Popen
        def launch(command,*args,**kwargs):
            if '-p' in command:
                assert kwargs['env']['CLAUDE_CONFIG_DIR']==str(live)
                command=list(command)+['--safe-mode','--tools','','--strict-mcp-config',str(base/'mcp.json')]
            return original_popen(command,*args,**kwargs)
        write(base/'mcp.json',{'mcpServers':{}})
        prompt=root/('task-'+uuid.uuid4().hex+'.txt')
        prompt.write_text('Reply exactly OK. Do not use tools or modify any files.',encoding='utf-8')
        session=str(uuid.uuid4())
        print('Starting isolated real limit-to-resume validation.',flush=True)
        with patch.object(runner,'Store',return_value=store), patch.object(runner,'running_claude',return_value=[]), patch.object(store,'quota',side_effect=quota), patch('subprocess.Popen',side_effect=launch), patch.object(sys,'argv',['runner','--cwd',str(cwd),'--session',session,'--prompt-file',str(prompt),'--permission-mode','default']):
            runner.main()
        state=read(root/'runner.json')
        events=[json.loads(line) for line in (root/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        assert any(e.get('event')=='managed_limit_exit' and e.get('slot')==exhausted for e in events),events
        assert any(e.get('event')=='managed_turn_complete' and e.get('slot')==available for e in events),events
        assert state['session']==session and state['status']=='本轮完成，已停止自动提交',state
        sessions=list(live.rglob(session+'.jsonl'))
        assert sessions,'Session transcript was not persisted'
        print('PASS: real limit, verified account switch, same-session successful completion and persisted transcript.',flush=True)
    # Live official CLI may legitimately refresh its own tokens; report rather than overwrite.
    after={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    print('Global configuration unchanged:',before==after,flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
