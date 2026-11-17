"""Utilidades Win32: tela cheia, focar app, atalho global, autostart, instância única."""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from ctypes import wintypes

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetForegroundWindow.restype = wintypes.HWND
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.MonitorFromPoint.restype = wintypes.HANDLE
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateMutexW.restype = wintypes.HANDLE

MONITOR_DEFAULTTONEAREST = 2
MONITOR_DEFAULTTOPRIMARY = 1
GW_OWNER = 4
SW_RESTORE = 9
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20
WS_EX_LAYERED = 0x80000
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _exe_name(hwnd) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
DWMWA_CLOAKED = 14
_dwm = ctypes.WinDLL("dwmapi")


def _cloaked(hwnd) -> bool:
    v = ctypes.c_int(0)
    _dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
    return v.value != 0


def window_under_island():
    """Janela "de verdade" mais de cima no topo-centro da tela principal,
    pulando a própria ilha, overlays transparentes (NVIDIA, Discord...),
    janelas ocultas/de outra área de trabalho virtual e minimizadas.
    Retorna (hwnd, rect, monitor_rect) ou None se for o desktop."""
    primary = user32.MonitorFromPoint(wintypes.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(primary, ctypes.byref(mi)):
        return None
    m = mi.rcMonitor
    px, py = (m.left + m.right) // 2, m.top + 4
    me = os.getpid()
    found = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        if not (r.left <= px < r.right and r.top <= py < r.bottom):
            return True
        cls = _class_name(hwnd)
        if cls in ("Progman", "WorkerW"):
            found.append(None)
            return False
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT) or cls in _SHELL_CLASSES:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me or _cloaked(hwnd) or not _window_text(hwnd):
            return True
        found.append((hwnd, r, m))
        return False

    user32.EnumWindows(proto(cb), 0)  # ordem Z: de cima para baixo
    return found[0] if found else None


def foreground_layout(own_hwnd: int = 0):
    """Como a janela debaixo da ilha ocupa o topo da tela principal.

    Retorna (tela_cheia, cobre_topo):
      tela_cheia: ocupa a tela inteira (jogo, vídeo em tela cheia, slides)
      cobre_topo: a borda de cima dela passa debaixo da ilha (janela
                  maximizada: abas do navegador, barra de título)
    """
    hit = window_under_island()
    if hit is None:
        return (False, False)
    _, r, m = hit
    full = r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom
    covers = r.top <= m.top + 12
    return (full, covers)


def _top_level_windows():
    out = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER):
            title = _window_text(hwnd)
            if title:
                out.append((hwnd, title))
        return True

    user32.EnumWindows(proto(cb), 0)
    return out


def focus_app(exe_hint: str, title_hint: str = "") -> bool:
    """Traz para frente a janela do app que está tocando."""
    exe_hint = (exe_hint or "").lower()
    th = (title_hint or "").lower()[:40]
    wins = _top_level_windows()
    cands = [(h, t) for h, t in wins if exe_hint and exe_hint in _exe_name(h)]
    pick = None
    if th:
        pick = next((h for h, t in cands if th in t.lower()), None)
        if pick is None and not cands:
            pick = next((h for h, t in wins if th in t.lower()), None)
    if pick is None and cands:
        pick = cands[0][0]
    if pick is None:
        return False
    try:
        if user32.IsIconic(pick):
            user32.ShowWindow(pick, SW_RESTORE)
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        my_tid = kernel32.GetCurrentThreadId()
        attached = bool(fg_tid and fg_tid != my_tid and user32.AttachThreadInput(my_tid, fg_tid, True))
        user32.SetForegroundWindow(pick)
        user32.BringWindowToTop(pick)
        if attached:
            user32.AttachThreadInput(my_tid, fg_tid, False)
        return True
    except Exception:
        log.exception("não consegui focar o app")
        return False


def set_click_through(hwnd: int, enabled: bool):
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    new = (style | WS_EX_TRANSPARENT | WS_EX_LAYERED) if enabled else (style & ~WS_EX_TRANSPARENT)
    if new != style:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new)


def reassert_topmost(hwnd: int):
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def window_at(x: int, y: int) -> int:
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    h = user32.WindowFromPoint(wintypes.POINT(x, y))
    return int(h) if h else 0


# ---------------- atalho global ----------------
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012


class HotkeyThread(threading.Thread):
    """Registra um atalho global numa thread com loop de mensagens próprio."""

    def __init__(self, mods: int, vk: int, callback):
        super().__init__(name="hotkey", daemon=True)
        self.mods, self.vk, self.callback = mods, vk, callback
        self._tid = None
        self.ok = threading.Event()
        self.failed = False

    def run(self):
        self._tid = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, self.mods | MOD_NOREPEAT, self.vk):
            self.failed = True
            self.ok.set()
            log.warning("atalho global já está em uso por outro programa")
            return
        self.ok.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                try:
                    self.callback()
                except Exception:
                    log.exception("callback do atalho")
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


# ---------------- autostart / instância única ----------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "MusicVideoBar"


def autostart_command() -> str:
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    main = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
    return f'"{exe}" "{main}"'


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass


_mutex = None


def claim_single_instance(name="Local\\MusicVideoBarIsland") -> bool:
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
