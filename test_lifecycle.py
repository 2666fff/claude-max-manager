"""Real Windows process/job tests and local diagnostics with no live accounts."""
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from core import read
from desktop import launch_independent, process_in_job
from lifecycle import Lifecycle


class BasicLimit(c.Structure):
    _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong), ('flags', w.DWORD),
                ('min_ws', c.c_size_t), ('max_ws', c.c_size_t), ('active', w.DWORD),
                ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]


class ExtendedLimit(c.Structure):
    _fields_ = [('basic', BasicLimit), ('io', c.c_ulonglong * 6),
                ('process_memory', c.c_size_t), ('job_memory', c.c_size_t),
                ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='Claude 管理 测试 ')
        self.root = Path(self.temp.name)
        self.events = []

    def tearDown(self):
        self.temp.cleanup()

    def log(self, event, **fields):
        self.events.append({'event': event, **fields})

    def wait_file(self, path, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(.05)
        self.fail('Child process did not reach its entry point')

    def test_error_keeps_frames_but_excludes_message_and_locals(self):
        runtime = Lifecycle(self.root, self.log)
        try:
            try:
                private_token = 'must-never-enter-diagnostics'
                raise ValueError(private_token)
            except ValueError as exc:
                runtime.error('ui', type(exc), exc.__traceback__)
            saved = read(runtime.path)
            self.assertEqual(saved['last_error']['type'], 'ValueError')
            self.assertEqual(saved['last_error']['frames'][-1]['file'], 'test_lifecycle.py')
            self.assertNotIn(private_token, runtime.path.read_text(encoding='utf-8') + str(self.events))
            runtime.finish('clean', 'tray_exit')
        finally:
            runtime.close()
        second = Lifecycle(self.root, self.log)
        try:
            self.assertFalse(second.previous_unclean)
            second.finish('clean', 'tray_exit')
        finally:
            second.close()

    def test_abrupt_process_exit_is_detected_on_restart(self):
        code = '''import os,sys
from lifecycle import Lifecycle
r=Lifecycle(sys.argv[1],lambda *a,**k:None)
r.pulse()
os._exit(17)
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.root)],
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 17)
        runtime = Lifecycle(self.root, self.log)
        try:
            self.assertTrue(runtime.previous_unclean)
            self.assertEqual(self.events[0]['event'], 'manager_unclean_exit')
            self.assertNotIn('crash', self.events[0]['reason'])
            runtime.finish('clean', 'tray_exit')
        finally:
            runtime.close()

    def test_independent_launch_survives_host_job_close(self):
        kernel = c.WinDLL('kernel32', use_last_error=True)
        for name, result, args in [
            ('CreateJobObjectW', w.HANDLE, [c.c_void_p, w.LPCWSTR]),
            ('SetInformationJobObject', w.BOOL, [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]),
            ('AssignProcessToJobObject', w.BOOL, [w.HANDLE, w.HANDLE]),
            ('OpenProcess', w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
            ('WaitForSingleObject', w.DWORD, [w.HANDLE, w.DWORD]),
            ('CloseHandle', w.BOOL, [w.HANDLE])]:
            fn = getattr(kernel, name)
            fn.restype, fn.argtypes = result, args
        probe = self.root / 'probe 子进程.py'
        probe.write_text('''import json,os,sys,time
from pathlib import Path
from desktop import process_in_job
root=Path(sys.argv[1]);name=sys.argv[2]
(root/(name+'.json')).write_text(json.dumps({'pid':os.getpid(),'in_job':process_in_job(),'arguments':sys.argv[3:]}))
while not (root/'stop').exists():time.sleep(.05)
''', encoding='utf-8')
        host_script = self.root / 'host.py'
        # Explicit sys.path lets both disposable processes import project code.
        project = str(Path(__file__).resolve().parent)
        probe.write_text('import sys\nsys.path.insert(0,' + repr(project) + ')\n' + probe.read_text(encoding='utf-8'), encoding='utf-8')
        host_script.write_text('import sys\nsys.path.insert(0,' + repr(project) + ')\n' + '''import subprocess,time
from pathlib import Path
from desktop import launch_independent
root=Path(sys.argv[1]);probe=root/'probe 子进程.py'
while not (root/'go').exists():time.sleep(.05)
subprocess.Popen([sys.executable,str(probe),str(root),'direct'],creationflags=subprocess.CREATE_NO_WINDOW)
launch_independent(probe,[str(root),'independent','literal & $ Unicode 空格'])
while True:time.sleep(1)
''', encoding='utf-8')
        job = kernel.CreateJobObjectW(None, None)
        self.assertTrue(job)
        handles = []
        host = None
        try:
            limits = ExtendedLimit()
            limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE, only this test's private job.
            self.assertTrue(kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))
            host = subprocess.Popen([sys.executable, str(host_script), str(self.root)], creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertTrue(kernel.AssignProcessToJobObject(job, int(host._handle)))
            (self.root / 'go').touch()
            for name in ['direct', 'independent']:
                self.wait_file(self.root / (name + '.json'))
            direct = read(self.root / 'direct.json')
            independent = read(self.root / 'independent.json')
            self.assertTrue(direct['in_job'])
            self.assertFalse(independent['in_job'])
            self.assertEqual(independent['arguments'], ['literal & $ Unicode 空格'])
            for row in [direct, independent]:
                handle = kernel.OpenProcess(0x1000 | 0x100000, False, row['pid'])
                self.assertTrue(handle)
                handles.append(handle)
            self.assertTrue(kernel.CloseHandle(job))
            job = None
            host.wait(timeout=5)
            self.assertEqual(kernel.WaitForSingleObject(handles[0], 5000), 0)
            self.assertEqual(kernel.WaitForSingleObject(handles[1], 0), 258)  # Still alive.
            (self.root / 'stop').touch()
            self.assertEqual(kernel.WaitForSingleObject(handles[1], 5000), 0)
        finally:
            (self.root / 'stop').touch()
            if job:
                kernel.CloseHandle(job)
            if host:
                host.wait(timeout=5)
            for handle in handles:
                kernel.WaitForSingleObject(handle, 5000)
                kernel.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
