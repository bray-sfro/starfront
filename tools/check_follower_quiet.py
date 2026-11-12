"""The Rokinon never uploads and never adds to the collaboration hours (0.2.53g).

Its L subs were reported under the FSQ85 agent and counted toward the
project. A follower frame is not queued, not sent, and not included in the
session report. A report that is sent names the camera and that camera's
focal length.

    python tools/check_follower_quiet.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp(prefix="follower-quiet-"))

from astrocontrol.autoupload import AutoUploader                    # noqa: E402
from astrocontrol.collabclient import CollabClient                  # noqa: E402
from astrocontrol.config import Config                              # noqa: E402
from astrocontrol.targets import TargetStore                        # noqa: E402

results: list[bool] = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))


class Conf:
    def __init__(self):
        from astrocontrol.config import DEFAULTS
        self.data = dict(DEFAULTS["collab"])

    def section(self, name):
        return dict(self.data) if name == "collab" else {}


FOLLOWER = {"role": "follower", "camera": "ZWO ASI6200MC Pro", "bayer": "RGGB",
            "colour": True, "mono": False, "upload": None, "name": "Rokinon 135mm"}
MASTER = {"role": "master", "camera": "ZWO ASI6200MM Pro", "colour": False,
          "mono": True, "upload": None, "name": "FSQ85"}

tmp = Path(tempfile.mkdtemp(prefix="fq-"))
up = AutoUploader(Conf(), SimpleNamespace(_ready=lambda: None), folder=tmp / "q")
light = tmp / "rokinon.fits"
light.write_bytes(b"not-a-real-upload")
queued = up.light_saved(str(light), {"collabProject": "97fcae827a9f",
                                    "collabTask": "481536614589"},
                        "Rokinon 135mm", None, identity=FOLLOWER)
case("a follower light is not queued", queued is False and up.items == [])
up.add(light, "97fcae827a9f", "light", "481536614589", "Rokinon 135mm",
       identity=FOLLOWER)
calls = []
up.http = lambda method, path, body: calls.append((method, path)) or (200, {"already": True}, {})
up.send(up.items[0])
case("a follower light is not sent",
     up.items[0]["state"] == "refused" and calls == [],
     f"state={up.items[0]['state']} calls={calls}")

# Session report. 12.25 h of Rokinon L must not be posted. The FSQ85's own
# 300 s is posted, with that camera's name and focal length.
config = Config(tmp / "settings.json")
config.update("optics", {"focalLength": 450.0, "pixelSize": 3.76})
config.update("camera", {"colour": False, "binning": 1})
config.update("collab", {"enabled": True, "token": "rig-token"})


class MasterRig:
    config = config


class Rigs:
    master = MasterRig()


store = TargetStore(tmp / "targets.json")
target = store.create("m42 horse test mosaic", 5.588, -4.9, 0.0,
                      collab={"project": "97fcae827a9f", "task": "481536614589",
                              "projectName": "m42 horse test mosaic"})
store.add_panel_integration(
    target["id"], "2026-11-05", 20, "L", 300.0,
    telescope="FSQ85", instrument="ZWO ASI6200MM Pro",
    focal_length=455.0, role="master", colour=False)
store.add_panel_integration(
    target["id"], "2026-11-05", 20, "L", 12.25 * 3600.0,
    telescope="Rokinon 135mm", instrument="ZWO ASI6200MC Pro",
    focal_length=135.0, role="follower", colour=True, bayer="RGGB")

posted = []


def fake_call(method, path, body=None):
    posted.append((method, path, body))
    rows = (body or {}).get("contributions") or []
    return {"recorded": [{"id": f"c{i}", "accepted": True, "verdict": {"summary": "ok"}}
                         for i in range(len(rows))]}


client = CollabClient(Rigs(), config, store)
client._call = fake_call
sent = client.report_pending()
body = posted[0][2] if posted else {}
rows = body.get("contributions") or []
case("the follower produces no session report",
     sent == 1 and len(rows) == 1 and rows[0]["seconds"] == 300.0
     and rows[0]["filterName"] == "L",
     f"sent={sent} rows={rows}")
case("the Rokinon's 12.25 h is not in the report",
     all(float(row.get("seconds") or 0) < 1000 for row in rows)
     and not any("6200MC" in str(row.get("instrument") or "") for row in rows)
     and not any("Rokinon" in str(row) for row in rows))
case("the report names the camera and its focal length",
     rows and rows[0].get("instrument") == "ZWO ASI6200MM Pro"
     and rows[0].get("focalLength") == 455.0,
     str(rows[0] if rows else None))
case("a second poll sends nothing more", client.report_pending() == 0 and len(posted) == 1)

# A tally with no camera identity (the older rows) is still reported.
legacy = TargetStore(tmp / "legacy.json")
old = legacy.create("Cygnus", 20.9, 44.6, collab={"project": "p", "task": "t"})
legacy.add_panel_integration(old["id"], "2026-09-21", 1, "Ha", 600.0)
legacy.add_panel_integration(old["id"], "2026-09-21", 1, "Ha", 600.0)
posted.clear()
other = CollabClient(Rigs(), config, legacy)
other._call = fake_call
case("a row that never recorded a camera is still reported",
     other.report_pending() == 1 and posted[0][2]["contributions"][0]["frames"] == 2,
     str(posted[0][2]["contributions"] if posted else None))

failed = len(results) - sum(results)
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(1 if failed else 0)
