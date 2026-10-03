"""Windows UI lifecycle checks with synthetic accounts; no network or real login."""
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import test_features
from desktop import user, RESTORE
from manager import App


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


if __name__ == '__main__':
    unittest.main()
