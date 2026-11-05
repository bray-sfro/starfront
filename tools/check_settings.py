"""The settings file keeps the hub and volunteer sections, and drops what it does not know.

    python tools/check_settings.py

  * Both sections exist in the defaults, with every key the UI writes.
  * A value written to them is still there after the file is read back.
  * A key that is not in the defaults is dropped on save, not persisted.

No test framework, for the same reason as the other checks here.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol.config import DEFAULTS, Config                 # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# The per-OS catalogue paths came with the home lab (0dbefe2): the share as each
# other OS's workers reach it, which the hub hands to them (hub.py, check_hub).
HUB_KEYS = {"enabled", "binary", "configDir", "dataRoot", "catalogueDir",
            "catalogueMacos", "catalogueWindows", "catalogueLinux", "bind",
            "publicUrl", "adminToken", "autoStart", "registeredId", "registeredName"}
VOLUNTEER_KEYS = {"enabled", "hubUrl", "workerId", "idlePolicy", "pauseWhileSequencing",
                  "pauseWhileNina", "workWindow", "scratchDir", "scratchCapGb",
                  "installedVersion", "manifestUrl"}

case("the hub section has exactly the keys the plan names",
     set(DEFAULTS.get("hub", {})) == HUB_KEYS, f"{sorted(DEFAULTS.get('hub', {}))}")
case("the volunteer section has exactly the keys the plan names",
     set(DEFAULTS.get("volunteer", {})) == VOLUNTEER_KEYS,
     f"{sorted(DEFAULTS.get('volunteer', {}))}")
case("the hub defaults to off and to the plan's bind address",
     DEFAULTS["hub"]["enabled"] is False and DEFAULTS["hub"]["bind"] == "0.0.0.0:8790")
case("the volunteer defaults to off and to when-idle",
     DEFAULTS["volunteer"]["enabled"] is False
     and DEFAULTS["volunteer"]["idlePolicy"] == "when_idle")

# A fresh file reads back the defaults.
path = Path(tempfile.mkdtemp()) / "settings.json"
fresh = Config(path)
case("a new settings file starts with the hub and volunteer defaults",
     fresh.section("hub") == DEFAULTS["hub"] and fresh.section("volunteer") == DEFAULTS["volunteer"])

# Values written to both sections survive a save and a reload from disk.
hub_values = {"enabled": True, "binary": "/opt/collab-hub", "bind": "127.0.0.1:9000",
              "publicUrl": "https://hub.example.test", "autoStart": False,
              "registeredId": "h-1", "registeredName": "Observatory hub"}
volunteer_values = {"enabled": True, "hubUrl": "https://hub.example.test", "workerId": "w-7",
                    "idlePolicy": "always", "pauseWhileSequencing": False,
                    "workWindow": "22:00-07:00", "scratchCapGb": 12.5,
                    "manifestUrl": "https://starfront.example.test/download/collab-worker"}
fresh.update("hub", hub_values)
fresh.update("volunteer", volunteer_values)
reloaded = Config(path)
case("the hub values are still there after the file is read back",
     all(reloaded.get("hub", key) == value for key, value in hub_values.items()))
case("the volunteer values are still there after the file is read back",
     all(reloaded.get("volunteer", key) == value for key, value in volunteer_values.items()))
case("a key never written keeps its default after reload",
     reloaded.get("volunteer", "pauseWhileNina") is True
     and reloaded.get("hub", "adminToken") == DEFAULTS["hub"]["adminToken"])

# An unknown key is dropped on save rather than written to the file.
reloaded.update("hub", {"enabled": False, "bogusKey": "typo"})
on_disk = json.loads(path.read_text("utf-8"))
case("an unknown key is dropped on save",
     "bogusKey" not in on_disk["hub"] and "bogusKey" not in Config(path).section("hub"))
case("and the known key next to it is still saved",
     on_disk["hub"]["enabled"] is False)

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
