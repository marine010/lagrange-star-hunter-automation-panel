from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Any

from .config import BotConfig
from .windowing import WindowInfo


def _load_pil_image():
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for screenshots") from exc
    return Image


def _capture_backend(config: BotConfig) -> str:
    return str(config.screen.get("capture_backend", "mss")).strip().casefold()


def _is_wgc_backend(config: BotConfig) -> bool:
    return _capture_backend(config) in {"wgc", "windows_graphics_capture"}


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"", "none", "auto", "default", "null"}:
        return None
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def capture_monitor_for_rect(
    config: BotConfig,
    left: int,
    top: int,
    width: int,
    height: int,
) -> tuple[dict[str, int], tuple[int, int]]:
    expand_top = max(0, int(config.screen.get("capture_expand_top_pixels", 0)))
    if expand_top <= 0:
        return {"left": left, "top": top, "width": width, "height": height}, (left, top)

    capture_top = max(0, top - expand_top)
    actual_expand_top = top - capture_top
    monitor = {
        "left": left,
        "top": capture_top,
        "width": width,
        "height": height + actual_expand_top,
    }
    origin = (left, top)
    return monitor, origin


def capture_region(monitor: dict[str, int], config: BotConfig) -> Any:
    backend = _capture_backend(config)
    if backend == "pyautogui":
        import pyautogui

        return pyautogui.screenshot(
            region=(
                int(monitor["left"]),
                int(monitor["top"]),
                int(monitor["width"]),
                int(monitor["height"]),
            )
        ).convert("RGB")

    if backend in {"imagegrab", "pillow", "pil"}:
        return _capture_imagegrab_region(monitor)

    if backend in {"wgc", "windows_graphics_capture"}:
        raise RuntimeError("screen.capture_backend='wgc' requires a window hwnd capture")

    if backend not in {"mss", ""}:
        raise RuntimeError(f"unsupported capture backend: {backend}")

    try:
        import mss

        with mss.mss() as sct:
            shot = sct.grab(monitor)
        Image = _load_pil_image()
        return Image.frombytes("RGB", shot.size, shot.rgb)
    except ImportError:
        return _capture_imagegrab_region(monitor)


def capture_window_client(
    window: WindowInfo,
    config: BotConfig,
) -> tuple[Any, tuple[int, int], dict[str, int]]:
    left, top, width, height = window.client_capture_rect
    if width <= 0 or height <= 0:
        raise RuntimeError(f"invalid client rect for window: {window.title}")
    monitor, origin = capture_monitor_for_rect(config, left, top, width, height)

    if _is_wgc_backend(config):
        return _capture_wgc_window_region(window, monitor, config), origin, monitor

    return capture_region(monitor, config), origin, monitor


def stop_wgc_sessions() -> None:
    with _WGC_SESSIONS_LOCK:
        sessions = list(_WGC_SESSIONS.values())
        _WGC_SESSIONS.clear()
    for session in sessions:
        session.stop()


def _capture_imagegrab_region(monitor: dict[str, int]) -> Any:
    left = int(monitor["left"])
    top = int(monitor["top"])
    right = left + int(monitor["width"])
    bottom = top + int(monitor["height"])
    from PIL import ImageGrab

    return ImageGrab.grab(bbox=(left, top, right, bottom)).convert("RGB")


def _capture_wgc_window_region(window: WindowInfo, monitor: dict[str, int], config: BotConfig) -> Any:
    timeout_seconds = float(config.screen.get("wgc_frame_timeout_seconds", 3.0))
    update_interval_ms = max(16, int(config.screen.get("wgc_minimum_update_interval_ms", 250)))
    draw_border = _optional_bool(config.screen.get("wgc_draw_border"))
    session = _wgc_session_for_hwnd(window.hwnd, timeout_seconds, update_interval_ms, draw_border)
    frame = session.frame(timeout_seconds)
    crop_box = _wgc_crop_box(window, monitor, frame.size)
    cropped = frame.crop(crop_box)
    expected_size = (int(monitor["width"]), int(monitor["height"]))
    if cropped.size != expected_size:
        cropped = cropped.resize(expected_size)
    return cropped.convert("RGB")


def _wgc_session_for_hwnd(
    hwnd: int,
    timeout_seconds: float,
    update_interval_ms: int,
    draw_border: bool | None,
) -> "_WgcWindowSession":
    with _WGC_SESSIONS_LOCK:
        session = _WGC_SESSIONS.get(hwnd)
        if (
            session is not None
            and not session.closed
            and session.update_interval_ms == update_interval_ms
            and session.requested_draw_border == draw_border
        ):
            return session
        if session is not None:
            session.stop()
        session = _WgcWindowSession(hwnd, timeout_seconds, update_interval_ms, draw_border)
        _WGC_SESSIONS[hwnd] = session
        return session


def _wgc_crop_box(
    window: WindowInfo,
    monitor: dict[str, int],
    frame_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    frame_bounds = _extended_frame_bounds(window)
    frame_left, frame_top, frame_right, frame_bottom = frame_bounds
    frame_width = max(1, frame_right - frame_left)
    frame_height = max(1, frame_bottom - frame_top)
    scale_x = frame_size[0] / frame_width
    scale_y = frame_size[1] / frame_height

    left = int(round((int(monitor["left"]) - frame_left) * scale_x))
    top = int(round((int(monitor["top"]) - frame_top) * scale_y))
    right = left + int(round(int(monitor["width"]) * scale_x))
    bottom = top + int(round(int(monitor["height"]) * scale_y))

    left = max(0, min(frame_size[0], left))
    top = max(0, min(frame_size[1], top))
    right = max(left, min(frame_size[0], right))
    bottom = max(top, min(frame_size[1], bottom))
    if right <= left or bottom <= top:
        raise RuntimeError(
            "WGC frame did not contain the requested client region: "
            f"frame_size={frame_size}, frame_bounds={frame_bounds}, monitor={monitor}"
        )
    return left, top, right, bottom


def _extended_frame_bounds(window: WindowInfo) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    try:
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(window.hwnd),
            ctypes.c_uint(9),  # DWMWA_EXTENDED_FRAME_BOUNDS
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if hr == 0 and rect.right > rect.left and rect.bottom > rect.top:
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return window.rect


def _is_wgc_border_toggle_unsupported(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).casefold()
        if "capture border" in message and "not supported" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


def _wgc_draw_border_attempts(draw_border: bool | None) -> tuple[bool | None, ...]:
    if draw_border is None:
        return (None,)
    return (draw_border, None)


class _WgcWindowSession:
    def __init__(self, hwnd: int, timeout_seconds: float, update_interval_ms: int, draw_border: bool | None):
        self.hwnd = int(hwnd)
        self.update_interval_ms = int(update_interval_ms)
        self.requested_draw_border = draw_border
        self.draw_border: bool | None = draw_border
        self._condition = threading.Condition()
        self._frame: Any | None = None
        self._closed = False
        self._error: BaseException | None = None
        self._control: Any | None = None

        try:
            import numpy as np
            from PIL import Image
            from windows_capture import WindowsCapture
        except ImportError as exc:
            raise RuntimeError("windows-capture is required for screen.capture_backend='wgc'") from exc

        for candidate_draw_border in _wgc_draw_border_attempts(draw_border):
            self._reset_state()
            try:
                self._start_capture(WindowsCapture, np, Image, candidate_draw_border, timeout_seconds)
                self.draw_border = candidate_draw_border
                break
            except BaseException as exc:
                self.stop()
                if candidate_draw_border is not None and _is_wgc_border_toggle_unsupported(exc):
                    continue
                raise

    def _reset_state(self) -> None:
        with self._condition:
            self._frame = None
            self._closed = False
            self._error = None

    def _start_capture(
        self,
        WindowsCapture: Any,
        np: Any,
        Image: Any,
        draw_border: bool | None,
        timeout_seconds: float,
    ) -> None:
        capture = WindowsCapture(
            cursor_capture=False,
            draw_border=draw_border,
            minimum_update_interval=self.update_interval_ms,
            window_hwnd=self.hwnd,
        )

        @capture.event
        def on_frame_arrived(frame, _capture_control):
            try:
                buffer = np.array(frame.frame_buffer, copy=True)
                if buffer.ndim != 3 or buffer.shape[2] < 3:
                    raise RuntimeError(f"unexpected WGC frame buffer shape: {buffer.shape}")
                rgb = buffer[:, :, [2, 1, 0]]
                image = Image.fromarray(rgb, "RGB")
                with self._condition:
                    self._frame = image
                    self._condition.notify_all()
            except BaseException as exc:
                with self._condition:
                    self._error = exc
                    self._condition.notify_all()

        @capture.event
        def on_closed():
            with self._condition:
                self._closed = True
                self._condition.notify_all()

        self._control = capture.start_free_threaded()
        try:
            self.frame(timeout_seconds)
        except BaseException:
            self.stop()
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    def frame(self, timeout_seconds: float) -> Any:
        with self._condition:
            if self._frame is None and self._error is None and not self._closed:
                self._condition.wait(timeout=max(0.1, timeout_seconds))
            if self._error is not None:
                raise RuntimeError(f"WGC capture failed for hwnd={self.hwnd}") from self._error
            if self._frame is None:
                raise TimeoutError(f"WGC capture timed out for hwnd={self.hwnd}")
            return self._frame.copy()

    def stop(self) -> None:
        control = self._control
        self._control = None
        if control is None:
            return
        try:
            control.stop()
            control.wait()
        except Exception:
            pass


_WGC_SESSIONS: dict[int, _WgcWindowSession] = {}
_WGC_SESSIONS_LOCK = threading.Lock()
