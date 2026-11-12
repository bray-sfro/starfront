"""Message boxes a camera driver puts up on Starfront's own camera thread (0.2.52e).

2026-11-04, FSQ106: ZWO's ASCOM camera driver (ASCOM.ASICamera2) had lost its
camera, read its exposure limits as 0 to 0 s, and answered StartExposure with
a modal box - "Please input Exposure Duration between 0 and 0s, and restart
exposure!" - on the thread that made the call. A modal box does not return
until somebody clicks OK, so the exposure never started, Abort queued behind
it, and the telescope sat there until Bray clicked it away.

The camera is now checked before StartExposure, so that box should not come
up at all (`AscomCamera.probe`). This is the net under it: once a second, the
top-level windows of each camera's own apartment thread are looked at, and a
box saying the driver has lost the camera is closed - with OK, exactly as a
person would - and the camera marked as not answering, which is what sends it
to be reconnected. Only windows of Starfront's own camera threads are ever
looked at, and only boxes with that message are closed; any other window a
driver puts up there is reported once and left alone.
"""
from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
import weakref
from typing import Any

_LOG = logging.getLogger("astrocontrol.session")

#: What a driver box says when its camera has gone. ZWO's wording, and the
#: same box for a gain or a bin outside limits it has forgotten.
LOST_PATTERNS = (
    re.compile(r"exposure\s+duration\s+between", re.I),
    re.compile(r"restart\s+exposure", re.I),
    re.compile(r"please\s+input\s+.{0,40}\bbetween\b", re.I),
)

POLL_SECONDS = 1.0


def says_lost(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in LOST_PATTERNS)


class _Win32:
    """The real windows, through pywin32. Absent off Windows."""

    def __init__(self) -> None:
        import win32api                                  # noqa: F401
        import win32con
        import win32gui
        self._gui = win32gui
        self._api = win32api
        self._con = win32con

    def dialogs(self, thread_id: int) -> list[tuple[int, str]]:
        found: list[int] = []

        def each(hwnd: int, _extra: Any) -> bool:
            with contextlib.suppress(Exception):
                if self._gui.IsWindowVisible(hwnd):
                    found.append(hwnd)
            return True

        with contextlib.suppress(Exception):
            self._gui.EnumThreadWindows(int(thread_id), each, None)
        out: list[tuple[int, str]] = []
        for hwnd in found:
            texts = [self._gui.GetWindowText(hwnd)]

            def child(sub: int, _extra: Any) -> bool:
                with contextlib.suppress(Exception):
                    texts.append(self._gui.GetWindowText(sub))
                return True

            with contextlib.suppress(Exception):
                self._gui.EnumChildWindows(hwnd, child, None)
            with contextlib.suppress(Exception):
                if self._gui.GetClassName(hwnd) != "#32770":     # not a dialog box
                    continue
            out.append((hwnd, " | ".join(t for t in texts if t)))
        return out

    def close(self, hwnd: int) -> None:
        # OK first - what a person clicking it does - then a plain close.
        with contextlib.suppress(Exception):
            self._api.PostMessage(hwnd, self._con.WM_COMMAND, self._con.IDOK, 0)
        time.sleep(0.3)
        with contextlib.suppress(Exception):
            if self._gui.IsWindow(hwnd):
                self._api.PostMessage(hwnd, self._con.WM_CLOSE, 0, 0)


#: Replaced by the checks with a scripted one.
BACKEND: Any = None


def _backend() -> Any:
    global BACKEND
    if BACKEND is None:
        try:
            BACKEND = _Win32()
        except Exception:                                # noqa: BLE001 - not Windows
            BACKEND = False
    return BACKEND or None


_watched: "weakref.WeakSet[Any]" = weakref.WeakSet()
_said: dict[int, str] = {}
_lock = threading.Lock()
_thread: threading.Thread | None = None


def _say(camera: Any, message: str, level: str) -> None:
    say = getattr(camera, "say", None)
    if callable(say):
        with contextlib.suppress(Exception):
            say(message, level)
            return
    _LOG.log(logging.WARNING if level in ("warn", "error") else logging.INFO, message)


def dismiss(camera: Any) -> list[str]:
    """Close any lost-camera box on this camera's thread; what they said."""
    backend = _backend()
    if backend is None:
        return []
    from . import ascom
    thread_id = ascom.channel_thread_id(getattr(camera, "_channel", "default"))
    if not thread_id:
        return []
    closed: list[str] = []
    for hwnd, text in backend.dialogs(thread_id):
        if says_lost(text):
            backend.close(hwnd)
            closed.append(text)
            reason = (f"its driver put up \"{text[:160]}\", which it does once it "
                      "has lost the camera; Starfront closed it")
            camera.lost_reason = reason
            _say(camera, f"Camera: {camera.name} is not responding - {reason}", "warn")
        elif _said.get(hwnd) != text:
            _said[hwnd] = text
            _say(camera, f"Camera: the driver for {camera.name} has a window open "
                 f"on the camera's thread (\"{text[:160]}\"); it is holding up the "
                 "camera until somebody closes it at the PC", "warn")
    return closed


def _loop() -> None:
    while True:
        time.sleep(POLL_SECONDS)
        with _lock:
            cameras = list(_watched)
        for camera in cameras:
            with contextlib.suppress(Exception):
                if getattr(camera, "connected", False):
                    dismiss(camera)


def watch(camera: Any) -> None:
    """Keep an eye on this camera's thread while it is connected."""
    global _thread
    if _backend() is None:
        return
    with _lock:
        _watched.add(camera)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, daemon=True,
                                       name="driver-dialogs")
            _thread.start()


def unwatch(camera: Any) -> None:
    with _lock:
        _watched.discard(camera)
