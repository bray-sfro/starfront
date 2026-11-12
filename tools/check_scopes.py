"""Registered scopes: the dropdown, the stored id, and what an upload sends.

    python3 tools/check_scopes.py

The collab server files a light under the scope an agent is bound to. This
checks the client side of that, against an in-memory upload endpoint:

* the dropdown lists the server's scopes, bound ones first, by displayName
* the id is stored per equipment telescope, not in the shared collab settings,
  and a rename changes only the label
* each RASA drawer (RASA-L, RASA-A, RASA-R) sends its own scopeId, including
  from a backfill
* 409 (not bound, camera miss), 403 (foreign scope) and 400 (unknown scope,
  missing scopeId) hold that file with the reason; they do not drop it and
  they do not pause the other files
* collab.registeredScopes off is the previous path: no scopeId
* the page has a dropdown and a link to the server, and no box to type a name

No test framework, for the same reason as the other checks here.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp(prefix="sf-scopes-"))

from astrocontrol import scopes                                          # noqa: E402
from astrocontrol.autoupload import AutoUploader                         # noqa: E402
from astrocontrol.config import DEFAULTS                                  # noqa: E402
from astrocontrol.equipment import EquipmentStore                         # noqa: E402
from astrocontrol.imaging.fits import write as write_fits                 # noqa: E402

import numpy as np                                                        # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(("PASS " if ok else "FAIL ") + what)


SCOPES = [
    {"id": "scope-fsq", "name": "FSQ-106", "displayName": "FSQ-106",
     "boundToThisAgent": False},
    {"id": "scope-l", "name": "RASA-L", "displayName": "RASA-L",
     "boundToThisAgent": True, "cameraSerial": "l-1"},
    {"id": "scope-r", "name": "RASA-R", "displayName": "RASA-R",
     "boundToThisAgent": True},
    {"id": "scope-a", "name": "RASA-A", "displayName": "RASA-A",
     "boundToThisAgent": True},
    {"id": "scope-other", "name": "Someone else", "displayName": "Someone else's scope",
     "boundToThisAgent": False},
]


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Conf:
    def __init__(self, **collab):
        self.data = {**DEFAULTS["collab"], **collab}

    def section(self, name):
        return dict(self.data) if name == "collab" else {}

    def get(self, section, key, default=None):
        if section != "collab":
            return default
        value = self.data.get(key, default)
        return default if value is None else value


class Listing:
    """What AutoUploader reads as the telescopes on this mount."""

    def __init__(self, rows):
        self.all = rows


class FakeUpload:
    """POST /api/v1/agent/uploads, with a per-scopeId answer when one is set."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.by_scope: dict[str, tuple] = {}
        self.default = (200, {"id": "up1", "offset": 0, "chunk": 100_000, "already": False}, {})

    def __call__(self, method, path, body):
        self.calls.append((method, path, body if isinstance(body, dict) else None))
        if method == "POST" and path == "/api/v1/agent/uploads" and isinstance(body, dict):
            answer = self.by_scope.get(str(body.get("scopeId") or ""), self.default)
            if answer[0] == 200 and not answer[1].get("already"):
                # Finish immediately: the test only cares about the start body.
                return (200, {"already": True, "name": body.get("name"), "status": "ready"}, {})
            return answer
        if method == "GET":
            return 200, {"calibrationNeeds": []}, {}
        return 200, {}, {}


def light(folder: Path, name: str, cards: dict) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {key: value for key, value in cards.items() if value not in ("", None)}
    write_fits(path, np.zeros((4, 4), dtype=np.uint16), header)
    return path


# ---------------------------------------------------------------- dropdown
hello = {"agent": "c9fbb7afa00b", "displayName": "rasa1", "scopes": SCOPES}
parsed = scopes.parse_scope_list(hello)
choices = scopes.dropdown_choices(parsed)
check(scopes.parse_scope_list({"scopes": SCOPES})[0]["id"] == "scope-fsq",
      "hello and GET /scopes are read the same way")
check([row["id"] for row in choices[:3]] == ["scope-a", "scope-l", "scope-r"],
      f"bound scopes come first ({[row['id'] for row in choices]})")
check(all(not row["bound"] for row in choices[3:]),
      "scopes that are not bound to this agent follow the bound ones")
check([row["displayName"] for row in choices[:3]] == ["RASA-A", "RASA-L", "RASA-R"],
      "the dropdown label is displayName")
check(all(row["id"] in {item["id"] for item in SCOPES} for row in choices)
      and len(choices) == len(SCOPES),
      "every choice is a scope the server listed, and none are added")
check(scopes.bound_count(SCOPES) == 3, "rasa1's three binds are the bound scopes")
check(scopes.rasa_drawer("rasa1") == "" and scopes.rasa_drawer("RASA") == "",
      "rasa1 is the agent, not a RASA-L/A/R drawer")
try:
    scopes.accept_scope_id(SCOPES, "invented-id")
    check(False, "an id the server did not list is refused")
except ValueError as exc:
    check("unknown scope" in str(exc), "an id the server did not list is refused")
check(scopes.accept_scope_id(SCOPES, "scope-l") == "scope-l",
      "an id the server listed is kept as the id")
check(scopes.accept_scope_id(SCOPES, "") == "", "a blank choice clears the selection")


# ---------------------------------------------------------------- persistence and rename
tmp = Path(tempfile.mkdtemp(prefix="sf-scope-store-"))
store = EquipmentStore(tmp / "equipment.json")
second = store.add_rig("RASA-A")
third = store.add_rig("RASA-R")
store.rename_rig("main", "RASA-L")
store.set_scope_id("main", scopes.accept_scope_id(SCOPES, "scope-l"))
store.set_scope_id(second["id"], scopes.accept_scope_id(SCOPES, "scope-a"))
store.set_scope_id(third["id"], scopes.accept_scope_id(SCOPES, "scope-r"))
on_disk = json.loads((tmp / "equipment.json").read_text())
saved_ids = {rig["name"]: rig.get("scopeId") for rig in on_disk["rigs"]}
check(saved_ids == {"RASA-L": "scope-l", "RASA-A": "scope-a", "RASA-R": "scope-r"},
      f"each telescope stores its own scope id ({saved_ids})")
check("scopeId" not in DEFAULTS["collab"] and "scope-l" not in json.dumps(DEFAULTS["collab"]),
      "the scope id is not a shared collab setting")
renamed = json.loads(json.dumps(SCOPES))
for row in renamed:
    if row["id"] == "scope-l":
        row["displayName"] = "RASA Left"
        row["name"] = "RASA Left"
check(store.scope_id("main") == "scope-l", "a rename does not change the stored id")
check(scopes.label_for(renamed, store.scope_id("main")) == "RASA Left",
      "a rename changes the label the dropdown shows")
check(scopes.label_for(SCOPES, "scope-l") == "RASA-L",
      "the label is read from the list, not from what was stored")
profile = store.save_profile("RASA night", {"site": {"observatory": "Starfront"}})
store.set_scope_id("main", "scope-fsq")
store.adopt_profile(profile["id"])
check(store.scope_id("main") == "scope-l"
      and store.scope_id(second["id"]) == "scope-a",
      "an equipment profile brings each telescope's scope id back")


# ---------------------------------------------------------------- which scope a frame gets
assignments = [
    {"id": "main", "name": "RASA-L", "scopeId": "scope-l"},
    {"id": second["id"], "name": "RASA-A", "scopeId": "scope-a"},
    {"id": third["id"], "name": "RASA-R", "scopeId": "scope-r"},
]
check(scopes.scope_id_for_frame(assignments, {"camera": "RASA-L"}, SCOPES) == "scope-l",
      "INSTRUME RASA-L sends RASA-L's scope id")
check(scopes.scope_id_for_frame(
    assignments, {"instrument": "ZWO ASI2600MM Pro", "telescope": "RASA-A"}, SCOPES) == "scope-a",
      "TELESCOP RASA-A sends RASA-A's scope id")
check(scopes.scope_id_for_frame(
    assignments, {"camera": "Camera RASA-R"}, SCOPES) == "scope-r",
      "RASA-R in the camera name sends RASA-R's scope id")
check(scopes.scope_id_for_frame(
    assignments, {"camera": "ZWO ASI2600MM Pro", "telescope": "rasa1"}, SCOPES) == "",
      "a frame that matches no drawer sends no invented scope id")
check(scopes.scope_id_for_frame(assignments, {"camera": "RASA-L"}, SCOPES, enabled=False) == "",
      "the flag off sends no scope id")
check(scopes.upload_extra("scope-l", enabled=True) == {"scopeId": "scope-l"},
      "an upload gains scopeId and nothing else")
check(scopes.upload_extra("scope-l", enabled=False) == {},
      "the flag off adds no filing field")
check("telescope" not in scopes.upload_extra("scope-l", enabled=True),
      "the equipment-profile name is not a filing field")
check(scopes.scope_id_for_frame(
    [{"id": "only", "name": "FSQ-106", "scopeId": "gone"}],
    {"rig": "only"}, SCOPES) == "",
      "a stored id the server no longer lists is not sent")


# ---------------------------------------------------------------- the uploader
def rigs():
    return Listing([
        {"id": "main", "name": "RASA-L", "role": "master", "scopeId": "scope-l",
         "camera": "RASA-L", "upload": True},
        {"id": "cam-a", "name": "RASA-A", "role": "slave", "scopeId": "scope-a",
         "camera": "RASA-A", "upload": True},
        {"id": "cam-r", "name": "RASA-R", "role": "slave", "scopeId": "scope-r",
         "camera": "RASA-R", "upload": True},
    ])


class Client:
    def __init__(self, known):
        self._known = known

    def known_scopes(self):
        return list(self._known)

    def _ready(self):
        return ("https://collab.starfront.space", "token")


def uploader(flag=True, known=SCOPES):
    folder = Path(tempfile.mkdtemp(prefix="sf-scope-up-"))
    conf = Conf(registeredScopes=flag, autoUpload=True)
    client = Client(known)
    made = AutoUploader(conf, client, None, folder=folder, clock=Clock(), rigs=rigs())
    server = FakeUpload()
    made.http = server
    return made, server


def queue_and_send(made, server, path, ident, rig):
    made.add(path, "p1", "light", "t1", rig, identity=ident)
    item = next(row for row in made.items if row["path"] == str(path))
    os.utime(path, (made.clock() - 30, made.clock() - 30))
    made.send(item)
    return item


frames = Path(tempfile.mkdtemp(prefix="sf-scope-frames-"))
made, server = uploader()
sent = {}
for drawer, scope_id, rig_id in (
        ("RASA-L", "scope-l", "main"),
        ("RASA-A", "scope-a", "cam-a"),
        ("RASA-R", "scope-r", "cam-r")):
    path = light(frames, f"{drawer}.fits", {"IMAGETYP": "Light", "INSTRUME": drawer, "EXPTIME": 30})
    item = queue_and_send(made, server, path, {"camera": drawer, "telescope": drawer, "role": "master"}, rig_id)
    sent[drawer] = item
bodies = [body for method, path, body in server.calls
          if method == "POST" and path == "/api/v1/agent/uploads"]
check([body.get("scopeId") for body in bodies] == ["scope-l", "scope-a", "scope-r"],
      f"each RASA camera's upload names its own scope ({[body.get('scopeId') for body in bodies]})")
check(all(body.get("state") == "already" for body in (sent["RASA-L"], sent["RASA-A"], sent["RASA-R"])),
      "a scope the server accepts is sent")
check(all("telescope" not in body and "Nightly RASA" not in json.dumps(body) for body in bodies),
      "the upload does not carry an equipment-profile name")

# Flag off: the same cameras, the old body.
made_off, server_off = uploader(flag=False)
path = light(frames, "off.fits", {"IMAGETYP": "Light", "INSTRUME": "RASA-L", "EXPTIME": 30})
item = queue_and_send(made_off, server_off, path, {"camera": "RASA-L", "telescope": "RASA-L"}, "main")
off_body = next(body for method, _path, body in server_off.calls if method == "POST")
check("scopeId" not in off_body and item["state"] == "already",
      "registeredScopes off does not send a scope id")


def hold(detail_code, body, name):
    made_h, server_h = uploader()
    server_h.default = (detail_code, body, {})
    path_h = light(frames, name, {"IMAGETYP": "Light", "INSTRUME": "RASA-L", "EXPTIME": 30})
    # A second file for another camera must not be paused by the first one's answer.
    other = light(frames, "other-" + name, {"IMAGETYP": "Light", "INSTRUME": "RASA-A", "EXPTIME": 30})
    held = queue_and_send(made_h, server_h, path_h, {"camera": "RASA-L"}, "main")
    server_h.default = (200, {"already": True, "status": "ready"}, {})
    kept = queue_and_send(made_h, server_h, other, {"camera": "RASA-A"}, "cam-a")
    return made_h, held, kept


made_h, held, kept = hold(409, {"detail": "this agent is not bound to that scope"}, "not-bound.fits")
check(held["state"] == "held" and "not bound" in held["error"]
      and any(row["path"] == held["path"] for row in made_h.items),
      "409 not bound holds the file with the reason and does not drop it")
check(not made_h.project_paused and kept["state"] == "already",
      "409 not bound does not pause the other camera's upload")
check("held" in made_h.status()["line"] and made_h.status()["held"],
      "the status line says the file is held")

made_h, held, kept = hold(409, {"detail": "no scope matches this camera (RASA)"}, "camera-miss.fits")
check(held["state"] == "held" and "camera" in held["error"].lower() and kept["state"] == "already",
      "409 when the RASA camera matches no scope holds that file only")

made_h, held, kept = hold(403, {"detail": "that scope belongs to another agent", "error": "foreign_scope"},
                          "foreign.fits")
check(held["state"] == "held" and held["state"] != "refused" and "another agent" in held["error"],
      "403 foreign scope holds the file instead of refusing it")

made_h, held, _kept = hold(400, {"detail": "unknown scope"}, "unknown.fits")
check(held["state"] == "held" and "unknown scope" in held["error"],
      "400 unknown scope holds the file")

made_h, held, _kept = hold(400, {"detail": "missing scopeId"}, "missing.fits")
check(held["state"] == "held" and "missing scopeId" in held["error"],
      "400 missing scopeId holds the file")

# The collaboration-not-taking-uploads 409 is still a project pause.
made_h, server_h = uploader()
server_h.default = (409, {"detail": "auto-upload is off on that collaboration"}, {})
path_h = light(frames, "off-project.fits", {"IMAGETYP": "Light", "INSTRUME": "RASA-L", "EXPTIME": 30})
paused = queue_and_send(made_h, server_h, path_h, {"camera": "RASA-L"}, "main")
check(paused["state"] == "queued" and "p1" in made_h.project_paused,
      "409 when the collaboration is not taking uploads still pauses that project")

made_h, server_h = uploader()
server_h.default = (403, {"detail": "not a light of this collaboration: wrong field"}, {})
path_h = light(frames, "wrong-field.fits", {"IMAGETYP": "Light", "INSTRUME": "RASA-L", "EXPTIME": 30})
refused = queue_and_send(made_h, server_h, path_h, {"camera": "RASA-L"}, "main")
check(refused["state"] == "refused" and "wrong field" in refused["error"],
      "403 for the frame itself still takes that frame off with the reason")


# ---------------------------------------------------------------- backfill
root = frames / "captures"
target = "m42 horse test mosaic"
cards = {
    "L": {"IMAGETYP": "Light", "INSTRUME": "RASA-L", "TELESCOP": "rasa1", "EXPTIME": 60},
    "A": {"IMAGETYP": "Light", "INSTRUME": "ZWO ASI2600MM Pro", "TELESCOP": "RASA-A", "EXPTIME": 60},
    "R": {"IMAGETYP": "Light", "INSTRUME": "Camera RASA-R", "TELESCOP": "mount", "EXPTIME": 60},
}
for letter, header in cards.items():
    light(root / target / "2026-11-11", f"rasa_{letter}.fits", header)
made_b, server_b = uploader()
report = made_b.backfill(root, [{
    "name": target,
    "collab": {"project": "p1", "task": "t1", "projectName": target},
}], rigs().all)
check(set(report["queuedNames"]) == {"rasa_L.fits", "rasa_A.fits", "rasa_R.fits"},
      f"backfill queues each RASA camera ({report['queuedNames']})")
by_name = {}
for item in list(made_b.items):
    os.utime(item["path"], (made_b.clock() - 30, made_b.clock() - 30))
    made_b.send(item)
    by_name[Path(item["path"]).name] = item
posted = {body.get("name"): body.get("scopeId")
          for method, path, body in server_b.calls
          if method == "POST" and isinstance(body, dict)}
check(posted.get("rasa_L.fits") == "scope-l"
      and posted.get("rasa_A.fits") == "scope-a"
      and posted.get("rasa_R.fits") == "scope-r",
      f"backfill sends the scope id for the camera that took the frame ({posted})")


# ---------------------------------------------------------------- the page
html = (ROOT / "astrocontrol/web/index.html").read_text()
script = (ROOT / "astrocontrol/web/collab.js").read_text()
start = html.find('id="colScopes"')
end = html.find('id="colStartFold"')
block = html[start:end] if start != -1 and end > start else ""
check('id="colScopeRows"' in block and 'id="colRegisteredScopes"' in block,
      "the collab tab has a scope dropdown and the fallback flag")
check('id="colScopeRegister"' in block and "https://collab.starfront.space" in block,
      "registering a scope is a link to the server")
check('type="text"' not in block and "<textarea" not in block.lower(),
      "the scope block has no free-text telescope name")
check("displayName" in script and "data-col-scope" in script
      and "Let the server use this telescope" in script,
      "the dropdown is filled with the server's display names")
page = (ROOT / "astrocontrol/main.py").read_text()
check('"/api/collab/scope"' in page and "accept_scope_id" in page,
      "saving a scope goes through the server's list and never invents an id")
check(DEFAULTS["collab"]["registeredScopes"] is True,
      "registered scopes default on, with the old path behind the flag")
check("registeredScopes" in (ROOT / "astrocontrol/autoupload.py").read_text()
      or "registered_scopes_enabled" in (ROOT / "astrocontrol/autoupload.py").read_text(),
      "auto-upload consults the flag before it sends a scope id")


# ---------------------------------------------------------------- summary
failed = [what for ok, what in results if not ok]
print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:")
    for what in failed:
        print(" -", what)
    raise SystemExit(1)
