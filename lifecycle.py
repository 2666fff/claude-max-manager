"""Local process lifecycle evidence; never record exception messages or locals."""
import ctypes
import datetime as dt
import faulthandler
import os
from pathlib import Path
import threading
import traceback

from core import read, write


def now():
    return dt.datetime.now().astimezone().isoformat()


class Lifecycle:
    def __init__(self, root, log):
        self.root = Path(root)
        self.path = self.root / 'manager-runtime.json'
        self.log = log
        self.lock = threading.RLock()
        previous = read(self.path) if self.path.exists() else None
        fault = self.root / 'manager-fault.log'
        self.previous_unclean = bool(previous and previous['status'] == 'running')
        if self.previous_unclean:
            # An abrupt kill cannot write its own exit event. Do not call this
            # a Python crash: an external kill or native fault is also possible.
            self.log('manager_unclean_exit', code=previous['pid'],
                     reason='exit not recorded; native_trace=' + str(fault.exists() and fault.stat().st_size > 0))
        if fault.exists():
            os.replace(fault, self.root / 'manager-fault.previous.log')
        self.fault_file = fault.open('w', encoding='utf-8')
        self.state = {'pid': os.getpid(), 'started': now(), 'heartbeat': now(), 'status': 'running',
                      'previous': previous}
        # Retain one previous run, not an ever-growing nested history.
        if previous:
            self.state['previous'] = {k: v for k, v in previous.items() if k != 'previous'}
        try:
            faulthandler.enable(file=self.fault_file, all_threads=True)
            write(self.path, self.state)
            self.log('manager_started', code=self.state['pid'])
        except Exception:
            self.close()
            raise

    def pulse(self):
        with self.lock:
            self.state['heartbeat'] = now()
            write(self.path, self.state)

    def error(self, stage, kind, tb):
        # Frame locations are useful for fixes; source lines, locals and str(exc)
        # can contain account information and must never enter diagnostics.
        frames = [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                  for f in traceback.extract_tb(tb)] if tb else []
        with self.lock:
            self.state['last_error'] = {'at': now(), 'stage': stage, 'type': kind.__name__, 'frames': frames}
            write(self.path, self.state)
            self.log('manager_exception', reason=f'{stage}: {kind.__name__}', code=self.state['pid'])

    def finish(self, status, reason):
        with self.lock:
            self.state.update(status=status, ended=now(), reason=reason)
            write(self.path, self.state)
            self.log('manager_stopped', code=self.state['pid'], reason=f'{status}: {reason}')

    def close(self):
        faulthandler.disable()
        self.fault_file.close()


class InstanceMutex:
    """Release the Windows handle on every exit, including duplicate launches."""
    def __init__(self, name='Local\\ClaudeMaxManager'):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        self.kernel = kernel
        ctypes.set_last_error(0)
        self.handle = kernel.CreateMutexW(None, False, name)
        error = ctypes.get_last_error()
        if not self.handle:
            raise ctypes.WinError(error)
        self.existing = error == 183

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
