"""python -m astrocontrol.fleet [--config fleet.json] [--host H] [--port N] [--no-browser]"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

from . import config as fleet_config
from .core import Fleet
from .server import create_app


def build(config_path=None, settings=None):
    settings = settings or fleet_config.load(config_path)
    fleet = Fleet(settings)
    return fleet, create_app(fleet)


def serve(config_path=None, host=None, port=None, browser=False) -> None:
    import uvicorn

    windowed = sys.stdout is None or sys.stderr is None   # Starfront.exe has no console
    if windowed:
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    fleet, app = build(config_path)
    listen = fleet.settings["listen"]
    host = host or listen.get("host") or "127.0.0.1"
    port = int(port or listen.get("port") or 8790)
    print(f"Starfront fleet: {len(fleet.nodes)} PCs from {fleet.settings['source']}")
    for node in fleet.nodes.values():
        print(f"  {node.name:<20} {node.base}")
    print(f"Dashboard on http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/")
    fleet.start()
    if browser:
        url = f"http://127.0.0.1:{port}/"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                           log_config=None if windowed else uvicorn.config.LOGGING_CONFIG))
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    try:
        server.run()
    finally:
        fleet.stop()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m astrocontrol.fleet")
    parser.add_argument("--config", help="fleet.json (default: next to Starfront's data)")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--browser", action="store_true", help="open the page")
    args = parser.parse_args(argv)
    serve(args.config, args.host, args.port, browser=args.browser)


if __name__ == "__main__":
    main()
