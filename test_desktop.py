"""Windows UI lifecycle checks with synthetic accounts; no network or real login."""
import time
import io
import json
import tkinter as tk
import unittest
from unittest.mock import patch

import test_features
from desktop import user, RESTORE
from manager import App
from core import read, write


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_features.ContractTests()
        self.fixture.setUp()
        self.store_patch = patch('manager.Store', return_value=self.fixture.store)
        self.refresh_patch = patch.object(App, 'refresh', lambda _: None)
        self.store_patch.start()
        self.refresh_patch.start()
        self.window = tk.Tk()
        self.app = App(self.window)
        self.window.update()

    def exists(self):
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    def pump(self, condition, timeout=4):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if self.exists():
                self.window.update()
            if condition():
                return
            time.sleep(.02)
        self.fail('Timed out waiting for the actual window lifecycle')

    def tearDown(self):
        if self.exists():
            self.window.destroy()
        self.app.tray.close()
        self.refresh_patch.stop()
        self.store_patch.stop()
        self.fixture.tearDown()

    def test_close_minimize_and_tray_restore(self):
        self.app.close()
        self.assertEqual(self.window.state(), 'withdrawn')
        user.PostMessageW(self.app.tray.hwnd, RESTORE, 0, 0)
        self.pump(lambda: self.window.state() == 'normal')
        self.window.iconify()
        self.pump(lambda: self.window.state() == 'withdrawn')
        user.PostMessageW(self.app.tray.hwnd, RESTORE, 0, 0)
        self.pump(lambda: self.window.state() == 'normal')
        self.assertTrue(self.app.tray.thread.is_alive())

    def test_tray_exit_waits_for_write_then_removes_icon(self):
        self.app.working = True
        self.app.events.put(('tray', 'exit'))
        self.pump(lambda: self.app.exit_requested)
        self.assertTrue(self.exists())
        self.app.working = False
        self.pump(lambda: not self.exists())
        self.assertIsNone(self.app.tray.hwnd)
        self.assertFalse(self.app.tray.thread.is_alive())

    def test_hidden_refresh_at_cache_deadline_then_auto_switch(self):
        self.refresh_patch.stop()
        now = time.time()
        clock = [now]
        for slot in self.fixture.store.slots():
            row = self.fixture.row(slot, five=88 if slot == 1 else 10)
            row.update(checked=now, next_poll=now + 300.6)
            write(self.fixture.store.profile(slot) / 'usage-cache.json', row)
        self.app.last_refresh = now
        self.app.close()
        calls = []
        def response(request, **kwargs):
            slot = int(request.get_header('Authorization').rsplit('-', 1)[1])
            calls.append(slot)
            return io.BytesIO(json.dumps({'five_hour': {'utilization': 100 if slot == 1 else 10},
                                         'seven_day': {'utilization': 20}}).encode())
        with patch('manager.time.time', side_effect=lambda: clock[0]), \
             patch('enhanced.urllib.request.urlopen', side_effect=response), \
             patch.object(self.fixture.store, 'switch', return_value='switched') as switch:
            # Old timer fires at 300, returns the still-valid 88% cache.
            clock[0] = now + 300
            self.app.refresh()
            self.pump(lambda: not self.app.refreshing and not self.app.working)
            self.assertEqual(calls, [])
            self.assertEqual(self.app.results[1]['windows']['five_hour']['utilization'], 88)
            # New heartbeat must retry promptly, without another full 300s wait.
            clock[0] = now + 331
            self.pump(lambda: switch.called)
            self.pump(lambda: not self.app.working)
            self.assertEqual(sorted(calls), [1, 2, 5])
            switch.assert_called_once_with(2, allow_running=True)
            self.assertEqual(self.window.state(), 'withdrawn')
            saved = read(self.fixture.store.root / 'last-status.json')
            self.assertEqual(next(r for r in saved['accounts'] if r['slot'] == 1)['windows']['five_hour']['utilization'], 100)
            # Next genuine deadline also triggers, proving recurrence.
            clock[0] = now + 632
            self.pump(lambda: len(calls) == 6 and not self.app.refreshing and not self.app.working)

    def test_callback_error_does_not_strand_done_event(self):
        self.app.refreshing = True
        self.app.events.put(('quota', self.fixture.row(1)))
        self.app.events.put(('done', None))
        with patch.object(self.app, 'render', side_effect=ValueError('synthetic private content')):
            self.pump(lambda: not self.app.refreshing)
        self.assertIn('error', self.app.results[1])
        log = (self.fixture.store.root / 'events.jsonl').read_text(encoding='utf-8')
        self.assertIn('monitor_error', log)
        self.assertNotIn('synthetic private content', log)

    def test_worker_setup_failure_finishes_refresh(self):
        self.refresh_patch.stop()
        with patch('manager.concurrent.futures.ThreadPoolExecutor', side_effect=RuntimeError('fixture')):
            self.app.refresh()
            self.pump(lambda: not self.app.refreshing)
        self.assertTrue(all(row.get('error') for row in self.app.results.values()))
        self.assertFalse(self.app.refresh_button.instate(['disabled']))

    def test_deadline_check_survives_exception_and_respects_backoff(self):
        now = time.time()
        for slot in self.fixture.store.slots():
            self.app.results[slot] = {'slot': slot, 'error': 'limited', 'next_poll': now + 800}
        with patch.object(self.app, 'refresh') as refresh:
            self.app.auto_refresh()
            refresh.assert_not_called()
            self.app.results[1]['next_poll'] = 0
            refresh.side_effect = [OSError('fixture'), None]
            self.pump(lambda: refresh.call_count >= 2)


if __name__ == '__main__':
    unittest.main()
