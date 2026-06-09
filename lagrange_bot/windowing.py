from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass


def enable_dpi_awareness() -> None:
    """Ask Windows for physical pixels instead of DPI-scaled logical pixels."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


enable_dpi_awareness()


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    rect: tuple[int, int, int, int]
    client_rect: tuple[int, int, int, int]

    @property
    def client_capture_rect(self) -> tuple[int, int, int, int]:
        left, top, right, bottom = self.client_rect
        return left, top, max(0, right - left), max(0, bottom - top)


def _user32():
    return ctypes.windll.user32


def _get_window_title(hwnd: int) -> str:
    user32 = _user32()
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0, 0, 0, 0
    return rect.left, rect.top, rect.right, rect.bottom


def _get_client_rect_on_screen(hwnd: int) -> tuple[int, int, int, int]:
    user32 = _user32()
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return 0, 0, 0, 0
    top_left = wintypes.POINT(rect.left, rect.top)
    bottom_right = wintypes.POINT(rect.right, rect.bottom)
    user32.ClientToScreen(hwnd, ctypes.byref(top_left))
    user32.ClientToScreen(hwnd, ctypes.byref(bottom_right))
    return top_left.x, top_left.y, bottom_right.x, bottom_right.y


def list_visible_windows() -> list[WindowInfo]:
    user32 = _user32()
    windows: list[WindowInfo] = []

    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.IsIconic(hwnd):
            return True
        title = _get_window_title(hwnd).strip()
        if not title:
            return True
        rect = _get_window_rect(hwnd)
        client_rect = _get_client_rect_on_screen(hwnd)
        width = client_rect[2] - client_rect[0]
        height = client_rect[3] - client_rect[1]
        if width <= 80 or height <= 60:
            return True
        windows.append(WindowInfo(int(hwnd), title, rect, client_rect))
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    windows.sort(key=lambda item: item.title.casefold())
    return windows


def find_window(title_contains: str) -> WindowInfo:
    needle = title_contains.casefold().strip()
    if not needle:
        raise RuntimeError("window title text is empty")
    matches = [item for item in list_visible_windows() if needle in item.title.casefold()]
    if not matches:
        raise RuntimeError(f"no visible window title contains: {title_contains!r}")
    return matches[0]


def get_window(hwnd: int) -> WindowInfo:
    title = _get_window_title(hwnd).strip()
    if not title:
        raise RuntimeError(f"window has no title: hwnd={hwnd}")
    return WindowInfo(
        hwnd=int(hwnd),
        title=title,
        rect=_get_window_rect(hwnd),
        client_rect=_get_client_rect_on_screen(hwnd),
    )


def focus_window(hwnd: int) -> None:
    user32 = _user32()
    user32.ShowWindow(wintypes.HWND(hwnd), 9)  # SW_RESTORE
    user32.SetForegroundWindow(wintypes.HWND(hwnd))
    user32.BringWindowToTop(wintypes.HWND(hwnd))


def set_window_topmost(hwnd: int, topmost: bool = True) -> None:
    user32 = _user32()
    hwnd_insert_after = -1 if topmost else -2  # HWND_TOPMOST / HWND_NOTOPMOST
    flags = 0x0001 | 0x0002 | 0x0010 | 0x0040  # NOSIZE | NOMOVE | NOACTIVATE | SHOWWINDOW
    if not user32.SetWindowPos(
        wintypes.HWND(hwnd),
        wintypes.HWND(hwnd_insert_after),
        0,
        0,
        0,
        0,
        flags,
    ):
        raise RuntimeError(f"SetWindowPos failed for hwnd={hwnd}")
