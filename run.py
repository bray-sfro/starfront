#!/usr/bin/env python3
"""Start Starfront.

By default this opens the application window on the capture PC.  The interface
is still served over HTTP underneath, so `--server` gives you the same screen on
a tablet or laptop on the same network.
"""

from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Starfront equipment control")
    parser.add_argument("--browser", action="store_true",
                        help="open the interface in a web browser instead of a window")
    parser.add_argument("--server", action="store_true",
                        help="serve only; open nothing (use with --host 0.0.0.0)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; use 0.0.0.0 to reach the rig from another device")
    parser.add_argument("--port", type=int, default=0,
                        help="port to listen on (default: pick a free one; 8765 with --server)")
    parser.add_argument("--reload", action="store_true", help="auto-reload during development")
    parser.add_argument("--debug", action="store_true",
                        help="developer tools in the application window")
    args = parser.parse_args()

    # Before anything else: a crash that leaves no trace cannot be fixed, and
    # the window has no console behind it to leave one on.
    from astrocontrol import logs
    path = logs.setup()
    print(f"Logging to {path}")

    if args.browser or args.server or args.reload:
        port = args.port or 8765
        url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{port}/"
        print(f"Starfront -> {url}")
        if args.browser and not args.reload:
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        uvicorn.run("astrocontrol.main:app", host=args.host, port=port,
                    reload=args.reload, log_level="info")
        return

    from astrocontrol import desktop
    desktop.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
