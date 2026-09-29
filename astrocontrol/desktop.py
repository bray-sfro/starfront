"""The desktop application window.

Starfront still runs as a local HTTP server - that is what makes the same
interface reachable from a tablet when you want it - but on the capture PC it
opens in its own window with a real menu bar rather than in a browser.  The
window is Edge WebView2, which ships with Windows, so this adds one small Python
dependency and nothing else.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

WINDOW_TITLE = "Starfront"
BACKGROUND = "#0a0c11"


def free_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for a port nobody is using."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


class ServerThread:
    """uvicorn on a background thread, with a clean shutdown."""

    def __init__(self, host: str, port: int, log_level: str = "warning") -> None:
        config = uvicorn.Config("astrocontrol.main:app", host=host, port=port,
                                log_level=log_level)
        self.server = uvicorn.Server(config)
        self.host = host
        self.port = port
        self._thread = threading.Thread(target=self.server.run, daemon=True,
                                        name="uvicorn")

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}/"

    def start(self, timeout: float = 20.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.started:
                return
            if not self._thread.is_alive():
                raise RuntimeError("the Starfront server failed to start")
            time.sleep(0.05)
        raise RuntimeError("the Starfront server did not start in time")

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=10.0)


class Bridge:
    """Python called from the page: things a web page cannot do on its own.

    Every public attribute here is walked recursively by pywebview when it builds
    `window.pywebview.api`, so the window reference is kept private: exposing it
    would send that walk through the whole WebView2 object graph off the UI
    thread, which hangs the window before the interface ever loads.
    """

    def __init__(self) -> None:
        self._window: Any = None

    def pick_folder(self, start: str = "") -> str | None:
        """Native folder chooser for the capture directory."""
        import webview

        if self._window is None:
            return None
        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG, directory=start or "", allow_multiple=False)
        if not result:
            return None
        return str(result[0]) if isinstance(result, (list, tuple)) else str(result)

    def pick_file(self, start: str = "") -> str | None:
        """Native file chooser, for pointing at phd2.exe."""
        import os

        import webview

        if self._window is None:
            return None
        folder = os.path.dirname(start) if start else ""
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, directory=folder, allow_multiple=False,
            file_types=("Programs (*.exe)", "All files (*.*)")
            if os.name == "nt" else ())
        if not result:
            return None
        return str(result[0]) if isinstance(result, (list, tuple)) else str(result)

    def pick_master(self, start: str = "") -> str | None:
        """Native file chooser for a master frame built elsewhere: FITS or XISF."""
        import os

        import webview

        if self._window is None:
            return None
        folder = start if os.path.isdir(start) else os.path.dirname(start)
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, directory=folder or "", allow_multiple=False,
            file_types=("Master frames (*.fit;*.fits;*.fts;*.xisf)",
                        "All files (*.*)")
            if os.name == "nt" else ())
        if not result:
            return None
        return str(result[0]) if isinstance(result, (list, tuple)) else str(result)

    def pick_fits(self, start: str = "") -> str | None:
        """Native file chooser for a FITS frame to frame against."""
        import os

        import webview

        if self._window is None:
            return None
        folder = start if os.path.isdir(start) else os.path.dirname(start)
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, directory=folder or "", allow_multiple=False,
            file_types=("FITS frames (*.fit;*.fits;*.fts)", "All files (*.*)")
            if os.name == "nt" else ())
        if not result:
            return None
        return str(result[0]) if isinstance(result, (list, tuple)) else str(result)


def _in_page(window: Any, script: str) -> None:
    """Run JS in the page from a menu handler without blocking the GUI thread."""
    threading.Thread(target=lambda: window.evaluate_js(script), daemon=True).start()


def build_menu(window: Any, server: ServerThread) -> list:
    import webview.menu as wm

    def equipment() -> None:
        _in_page(window, "window.toggleEquipment && window.toggleEquipment()")

    def disconnect_all() -> None:
        _in_page(window, "document.getElementById('btnDisconnectAll').click()")

    def reload_ui() -> None:
        _in_page(window, "location.reload()")

    def open_in_browser() -> None:
        import webbrowser
        webbrowser.open(server.url)

    def fit_view() -> None:
        _in_page(window, "document.getElementById('btnFit').click()")

    def actual_size() -> None:
        _in_page(window, "document.getElementById('btnActual').click()")

    return [
        wm.Menu("File", [
            wm.MenuAction("Open in browser", open_in_browser),
            wm.MenuSeparator(),
            wm.MenuAction("Quit", window.destroy),
        ]),
        wm.Menu("Equipment", [
            wm.MenuAction("Connect equipment…\tE", equipment),
            wm.MenuSeparator(),
            wm.MenuAction("Disconnect all", disconnect_all),
        ]),
        wm.Menu("View", [
            wm.MenuAction("Fit image\tF", fit_view),
            wm.MenuAction("Actual size (1:1)", actual_size),
            wm.MenuSeparator(),
            wm.MenuAction("Reload interface", reload_ui),
        ]),
    ]


def run(host: str = "127.0.0.1", port: int = 0, debug: bool = False) -> None:
    """Start the server and open the window.  Returns when the window closes.

    Every step is logged.  Starting this window is the one stretch of the
    program with no interface to report through and, under `pythonw`, no console
    either — so a window that never appears, or appears wrong, has to be
    diagnosable from the log alone.
    """
    log = logging.getLogger("astrocontrol.desktop")
    log.info("starting the window (pywebview %s)", _webview_version())
    try:
        import webview
    except ImportError as exc:                     # pragma: no cover - install guard
        raise SystemExit(
            "The desktop window needs pywebview:  pip install pywebview\n"
            "Or run the interface in a browser instead:  python run.py --browser"
        ) from exc

    server = ServerThread(host, port or free_port(host if host != "0.0.0.0" else "127.0.0.1"))
    server.start()
    log.info("server listening on %s", server.url)

    bridge = Bridge()
    window = webview.create_window(
        WINDOW_TITLE, server.url,
        js_api=bridge,
        width=1680, height=1000, min_size=(1180, 720),
        background_color=BACKGROUND,
        text_select=False,
        confirm_close=False,
    )
    bridge._window = window
    log.info("window created")

    # A menu that cannot be built must not cost the whole window: opening
    # without a menu bar is a nuisance, failing to open at all is a lost night.
    # Either way it says so, because a silently menu-less window is exactly the
    # sort of thing that gets noticed and not explained.
    menu = []
    try:
        menu = build_menu(window, server)
        log.info("menu built: %d top-level items", len(menu))
    except Exception:                              # noqa: BLE001 - reported below
        log.exception("the menu bar could not be built; opening without it")

    # The Starfront mark in the title bar and on the taskbar. Bundled with
    # the web folder, so it is found the same way in the packed program.
    icon = Path(__file__).resolve().parent / "web" / "starfront.ico"
    extra = {"icon": str(icon)} if icon.is_file() else {}
    try:
        log.info("handing over to the webview event loop")
        webview.start(menu=menu, debug=debug, **extra)
        log.info("the window was closed")
    except TypeError:
        # Older pywebview builds take no `menu` or `icon` argument at all.
        # Better a window with no menu bar than no window.
        log.exception("this pywebview does not accept a menu; opening without one")
        webview.start(debug=debug)
    finally:
        server.stop()
        log.info("server stopped")


def _webview_version() -> str:
    try:
        import webview
        return str(getattr(webview, "__version__", "unknown"))
    except Exception:                              # noqa: BLE001 - only for the log
        return "not installed"
