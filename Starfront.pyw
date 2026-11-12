#!/usr/bin/env pythonw
"""Double-clickable launcher: opens the Starfront window with no console."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Before anything else. With no console behind this window, a failure during
# startup would otherwise leave nothing at all to look at — and logging used to
# begin only once the server thread imported the application, which is far too
# late to explain a window that never appears.
from astrocontrol import logs

logs.setup()

# `Starfront.exe --fleet`: the fleet dashboard for every Starfront PC, in the
# browser, from the same program (0.2.52).
if "--fleet" in sys.argv[1:]:
    from astrocontrol.fleet.__main__ import serve
    args = sys.argv[1:]
    path = args[args.index("--fleet-config") + 1] if "--fleet-config" in args[:-1] else None
    serve(config_path=path, browser=True)
    sys.exit(0)

from astrocontrol import desktop

try:
    desktop.run()
except BaseException:
    logs.logging.getLogger("astrocontrol.crash").critical(
        "the window failed to start", exc_info=True)
    raise
