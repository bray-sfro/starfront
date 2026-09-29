#!/usr/bin/env pythonw
"""Starfront with the stack watchdog armed.

Use this when the window stops responding.  Every ten seconds it writes what
each thread is doing to `~/Starfront/logs/stacks.log`, so when it freezes the
last dump says which thread stopped and what it was sitting in — a hang leaves
no exception behind, so there is nothing else to go on.

Identical to the normal launcher in every other way.  It costs a few kilobytes a
minute, which is why it is a separate shortcut rather than the default.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("ASTRO_STACKDUMP", "10")

from astrocontrol import logs

logs.setup()

from astrocontrol import desktop

try:
    desktop.run(debug=True)
except BaseException:
    logs.logging.getLogger("astrocontrol.crash").critical(
        "the window failed to start", exc_info=True)
    raise
