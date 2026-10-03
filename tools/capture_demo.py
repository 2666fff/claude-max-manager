"""Capture the actual Tk interface using only synthetic, offline account data."""
import ctypes
import datetime as dt
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from core import write
from enhanced import Store
from manager import App


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    with tempfile.TemporaryDirectory(prefix='claude-max-demo-') as temporary:
        base = Path(temporary)
        live = base / 'live'
        root = base / 'accounts'
        for slot in range(1, 13):
            identity = {'accountUuid': f'demo-account-{slot}',
                        'emailAddress': f'account-{slot}@example.invalid'}
            write(root / f'max-{slot}' / '.claude.json', {'oauthAccount': identity})
            # No real credential or even token-like value exists in this fixture.
            write(root / f'max-{slot}' / '.credentials.json', {'claudeAiOauth': {}})
            if slot == 1:
                write(live / '.claude.json', {'oauthAccount': identity})
        store = Store(root, live, live / '.claude.json', process_check=lambda: [])
        with patch('manager.Store', return_value=store), patch.object(App, 'refresh', lambda _, **kwargs: None), \
                patch('urllib.request.urlopen', side_effect=RuntimeError('Demo is strictly offline')):
            window = tk.Tk()
            app = App(window)
            window.title('Claude Max Manager — 离线演示 / 虚构数据')
            app.subtitle.config(text='离线界面演示 · 所有账号、额度与时间均为虚构数据')
            now = time.time()
            for slot in range(1, 13):
                five = 100 if slot in (1, 10) else (slot - 1) * 7
                week = 100 if slot in (5, 12) else (slot - 1) * 5
                def reset(hours):
                    return dt.datetime.fromtimestamp(now + hours * 3600, dt.timezone.utc).isoformat()
                app.render({'slot': slot, 'plan': 'max', 'checked': now if slot == 1 else now - 3600, 'cached': slot != 1,
                    'auth_expires': (now + 30 * 86400) * 1000,
                    'scoped': [{'name': 'Opus', 'utilization': week}],
                    'windows': {'five_hour': {'utilization': five, 'resets_at': reset(2)},
                                'seven_day': {'utilization': week, 'resets_at': reset(48)}}})
            app.note.config(text='演示数据 · 未读取本机账号 · 未发送任何网络请求')
            window.update()
            app.canvas.yview_moveto(0)
            window.update()
            output = PROJECT / 'docs/screenshots/dashboard.png'
            output.parent.mkdir(parents=True, exist_ok=True)
            handle = window.winfo_id()
            # Capture the window itself, not the desktop or other applications.
            capture = subprocess.Popen(['powershell.exe', '-NoProfile', '-File', str(PROJECT / 'tools/capture_window.ps1'),
                            '-WindowHandle', str(handle), '-OutputPath', str(output)],
                            creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 30
            def capture_done():
                if capture.poll() is not None:
                    window.after_idle(window.destroy)
                elif time.monotonic() > deadline:
                    capture.kill()
                    capture.wait()
                    window.after_idle(window.destroy)
                else:
                    window.after(30, capture_done)
            window.after(30, capture_done)
            window.mainloop()
            if capture.returncode:
                raise RuntimeError('Demo window capture failed')
            if not app.window_destroyed:
                window.destroy()
            print('Saved synthetic-data UI screenshot:', output.relative_to(PROJECT))


if __name__ == '__main__':
    main()
