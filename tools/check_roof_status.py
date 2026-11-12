"""The roof badge is a line, not a command (0.2.53g).

    python tools/check_roof_status.py
"""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp(prefix="roof-status-"))

from astrocontrol import roofstatus                            # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))


def line(state, locked="", now=100.0, connected=True):
    roofstatus.reset()
    dome = {"connected": connected, "canShutter": True, "shutterState": state}
    return roofstatus.observe(dome, locked, now)


opened = line("opening")
later = roofstatus.observe(
    {"connected": True, "canShutter": True, "shutterState": "opening"}, "", 112.4)
case("opening names itself and the elapsed seconds",
     later["text"].startswith("Roof opening…") and "12s" in later["text"], later["text"])

case("then open", line("open")["text"] == "Roof open")
roofstatus.reset()
closing = roofstatus.observe(
    {"connected": True, "canShutter": True, "shutterState": "closing"}, "", 50)
closing = roofstatus.observe(
    {"connected": True, "canShutter": True, "shutterState": "closing"}, "", 58)
case("closing names itself and the elapsed seconds",
     closing["text"].startswith("Roof closing…") and "8s" in closing["text"], closing["text"])
case("then closed", line("closed")["text"] == "Roof closed")

locked = line("closed", "not open after 180 s (it reads opening)")
case("a timeout or technician lock is the text",
     locked["text"].startswith("Roof locked:") and "180" in locked["text"], locked["text"])

err = line("error")
case("an error is the text", err["text"] == "Roof error", err["text"])

case("no dome, no badge", line("open", connected=False)["connected"] is False)

# Observing does not grow a command list. There is nothing to command.
case("the helper has no open", not hasattr(roofstatus, "request_open"))

failed = len(results) - sum(results)
print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
