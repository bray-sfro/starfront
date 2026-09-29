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

from astrocontrol import desktop

try:
    desktop.run()
except BaseException:
    logs.logging.getLogger("astrocontrol.crash").critical(
        "the window failed to start", exc_info=True)
    raise
