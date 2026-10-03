"""Windows tray and desktop shortcut integration; standard library only."""
import base64
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import sys
import threading

TRAY_CLASS = 'ClaudeMaxManagerTray'
TRAY_TITLE = 'Claude Max Manager Tray'
CALLBACK = 0x8001
RESTORE = 0x8002
OPEN, EXIT = 1001, 1002
WNDPROC = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASS(c.Structure):
    _fields_ = [('style', w.UINT), ('proc', WNDPROC), ('clsExtra', c.c_int),
               ('wndExtra', c.c_int), ('instance', w.HINSTANCE), ('icon', w.HICON),
               ('cursor', w.HANDLE), ('background', w.HBRUSH),
               ('menuName', w.LPCWSTR), ('className', w.LPCWSTR)]


class GUID(c.Structure):
    _fields_ = [('a', w.DWORD), ('b', w.WORD), ('d', w.WORD), ('bytes', w.BYTE * 8)]


class ICONDATA(c.Structure):
    _fields_ = [('size', w.DWORD), ('window', w.HWND), ('id', w.UINT),
               ('flags', w.UINT), ('callback', w.UINT), ('icon', w.HICON),
               ('tip', w.WCHAR * 128), ('state', w.DWORD), ('stateMask', w.DWORD),
               ('info', w.WCHAR * 256), ('timeout', w.UINT), ('title', w.WCHAR * 64),
               ('infoFlags', w.DWORD), ('guid', GUID), ('balloonIcon', w.HICON)]


user = c.WinDLL('user32', use_last_error=True)
shell = c.WinDLL('shell32', use_last_error=True)
kernel = c.WinDLL('kernel32', use_last_error=True)


def signature(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, list(args)
    return fn


signature(kernel, 'GetModuleHandleW', w.HMODULE, w.LPCWSTR)
signature(user, 'RegisterClassW', w.ATOM, c.POINTER(WNDCLASS))
signature(user, 'UnregisterClassW', w.BOOL, w.LPCWSTR, w.HINSTANCE)
signature(user, 'CreateWindowExW', w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
          c.c_int, c.c_int, c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p)
signature(user, 'DefWindowProcW', c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
signature(user, 'DestroyWindow', w.BOOL, w.HWND)
signature(user, 'PostMessageW', w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
signature(user, 'FindWindowW', w.HWND, w.LPCWSTR, w.LPCWSTR)
signature(user, 'GetMessageW', w.BOOL, c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
signature(user, 'TranslateMessage', w.BOOL, c.POINTER(w.MSG))
signature(user, 'DispatchMessageW', c.c_ssize_t, c.POINTER(w.MSG))
signature(user, 'PostQuitMessage', None, c.c_int)
signature(user, 'LoadIconW', w.HICON, w.HINSTANCE, w.LPCWSTR)
signature(user, 'RegisterWindowMessageW', w.UINT, w.LPCWSTR)
signature(user, 'CreatePopupMenu', w.HMENU)
signature(user, 'AppendMenuW', w.BOOL, w.HMENU, w.UINT, c.c_size_t, w.LPCWSTR)
signature(user, 'TrackPopupMenu', w.UINT, w.HMENU, w.UINT, c.c_int, c.c_int,
          c.c_int, w.HWND, c.c_void_p)
signature(user, 'DestroyMenu', w.BOOL, w.HMENU)
signature(user, 'GetCursorPos', w.BOOL, c.POINTER(w.POINT))
signature(user, 'SetForegroundWindow', w.BOOL, w.HWND)
signature(shell, 'Shell_NotifyIconW', w.BOOL, w.DWORD, c.POINTER(ICONDATA))


def restore_existing():
    hwnd = user.FindWindowW(TRAY_CLASS, TRAY_TITLE)
    return bool(hwnd and user.PostMessageW(hwnd, RESTORE, 0, 0))


class Tray:
    def __init__(self, events):
        self.events = events
        self.hwnd = None
        self.data = None
        self.error = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name='system-tray', daemon=True)
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError('系统托盘初始化超时，未隐藏主窗口。')
        if self.error:
            raise RuntimeError('系统托盘初始化失败：' + self.error)

    def _menu(self):
        menu = user.CreatePopupMenu()
        if not menu:
            raise c.WinError(c.get_last_error())
        try:
            user.AppendMenuW(menu, 0, OPEN, '打开主窗口')
            user.AppendMenuW(menu, 0x800, 0, None)
            user.AppendMenuW(menu, 0, EXIT, '退出')
            point = w.POINT()
            user.GetCursorPos(c.byref(point))
            user.SetForegroundWindow(self.hwnd)
            selected = user.TrackPopupMenu(menu, 0x100 | 0x2, point.x, point.y, 0, self.hwnd, None)
            user.PostMessageW(self.hwnd, 0, 0, 0)
            if selected:
                self.events.put(('tray', 'exit' if selected == EXIT else 'open'))
        finally:
            user.DestroyMenu(menu)

    def _run(self):
        instance = kernel.GetModuleHandleW(None)
        taskbar_created = user.RegisterWindowMessageW('TaskbarCreated')
        @WNDPROC
        def callback(hwnd, message, wp, lp):
            try:
                if message == RESTORE:
                    self.events.put(('tray', 'open'))
                elif message == CALLBACK:
                    if lp in (0x202, 0x203):
                        self.events.put(('tray', 'open'))
                    elif lp == 0x205:
                        self._menu()
                elif message == taskbar_created and self.data:
                    if not shell.Shell_NotifyIconW(0, c.byref(self.data)):
                        raise RuntimeError('资源管理器重启后无法恢复托盘图标')
                elif message == 0x10:
                    user.DestroyWindow(hwnd)
                elif message == 0x2:
                    if self.data:
                        shell.Shell_NotifyIconW(2, c.byref(self.data))
                    self.hwnd = None
                    user.PostQuitMessage(0)
                else:
                    return user.DefWindowProcW(hwnd, message, wp, lp)
            except Exception as exc:
                self.events.put(('tray_error', str(exc)))
            return 0
        self.callback = callback  # Keep ctypes callback alive until window destruction.
        cls = WNDCLASS(proc=callback, instance=instance, className=TRAY_CLASS)
        registered = False
        try:
            if not user.RegisterClassW(c.byref(cls)):
                raise c.WinError(c.get_last_error())
            registered = True
            self.hwnd = user.CreateWindowExW(0, TRAY_CLASS, TRAY_TITLE, 0,
                                             0, 0, 0, 0, None, None, instance, None)
            if not self.hwnd:
                raise c.WinError(c.get_last_error())
            icon = user.LoadIconW(None, c.cast(c.c_void_p(32512), w.LPCWSTR))
            self.data = ICONDATA(size=c.sizeof(ICONDATA), window=self.hwnd, id=1,
                                 flags=1 | 2 | 4, callback=CALLBACK, icon=icon,
                                 tip='Claude Max · 点击打开，右键退出')
            if not shell.Shell_NotifyIconW(0, c.byref(self.data)):
                raise RuntimeError('Windows 未接受托盘图标')
            self.ready.set()
            msg = w.MSG()
            while True:
                result = user.GetMessageW(c.byref(msg), None, 0, 0)
                if result == -1:
                    raise c.WinError(c.get_last_error())
                if result == 0:
                    break
                user.TranslateMessage(c.byref(msg))
                user.DispatchMessageW(c.byref(msg))
        except Exception as exc:
            self.error = str(exc)
            if self.ready.is_set():
                self.events.put(('tray_error', self.error))
            self.ready.set()
        finally:
            if self.hwnd:
                user.DestroyWindow(self.hwnd)
            self.hwnd = None
            if registered:
                user.UnregisterClassW(TRAY_CLASS, instance)

    def close(self):
        if self.hwnd:
            user.PostMessageW(self.hwnd, 0x1F, 0, 0)  # Dismiss a popup before shutdown.
            user.PostMessageW(self.hwnd, 0x10, 0, 0)
        self.thread.join(timeout=5)


def create_shortcut(script):
    python = Path(sys.executable).with_name('pythonw.exe')
    if not python.is_file():
        raise RuntimeError('未找到 pythonw.exe，无法创建桌面启动入口。')
    env = os.environ.copy()
    env['CCM_SHORTCUT_PYTHON'] = str(python)
    env['CCM_SHORTCUT_SCRIPT'] = str(Path(script).resolve(strict=True))
    code = '''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$shell = New-Object -ComObject WScript.Shell
$desktop = $shell.SpecialFolders.Item('Desktop')
$path = Join-Path $desktop 'Claude Max 账号管理.lnk'
$shortcut = $shell.CreateShortcut($path)
$shortcut.TargetPath = $env:CCM_SHORTCUT_PYTHON
$shortcut.Arguments = '"' + $env:CCM_SHORTCUT_SCRIPT + '"'
$shortcut.WorkingDirectory = Split-Path -LiteralPath $env:CCM_SHORTCUT_SCRIPT
$shortcut.Description = 'Claude Max 账号与额度管理'
$shortcut.IconLocation = $env:CCM_SHORTCUT_PYTHON + ',0'
$shortcut.Save()
if (-not (Test-Path -LiteralPath $path)) { throw '快捷方式未保存' }
Write-Output $path
'''
    encoded = base64.b64encode(code.encode('utf-16le')).decode('ascii')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-EncodedCommand', encoded],
                            env=env, capture_output=True, encoding='utf-8', timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('创建快捷方式失败：' + result.stderr.strip())
    return result.stdout.strip()
