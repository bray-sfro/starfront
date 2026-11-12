"""Three identical telescopes, told apart.

    python tools/check_triple.py

Covers the three things a triple rig needs that a single one never did:
naming ZWO bodies by the alias written into them (and addressing them by it),
setting every telescope up from its own N.I.N.A. profile in one go, and
finding out which focuser is on which camera by moving them.  No hardware: the
ZWO SDK is never loaded, and the pairing check runs against synthetic star
fields whose star size follows the right focuser.
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol import identify, ninaimport                      # noqa: E402
from astrocontrol.config import Config                             # noqa: E402
from astrocontrol.devices import zwo                               # noqa: E402
from astrocontrol.devices.base import Camera, DeviceError, Focuser  # noqa: E402
from astrocontrol.equipment import EquipmentStore                  # noqa: E402
from astrocontrol import collab, collabclient                       # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# ---------------------------------------------------------------------------
print("-- ZWO aliases --")

case("an eight-byte alias reads as text",
     zwo._alias_text(bytes(b"RASA-L\x00\x00")) == "RASA-L")
case("...and an unnamed body reads as nothing",
     zwo._alias_text(bytes(8)) == "" and zwo._alias_text(bytes([0xFF] * 8)) == "")
case("the EAF carries its alias in its name",
     zwo.focuser_alias("EAF(RASA-R)") == "RASA-R" and zwo.focuser_alias("EAF") == "")
case("an alias address is recognised",
     zwo.is_alias_id(zwo.alias_id("RASA-L")) and not zwo.is_alias_id("2a20560d0b020900"))

zwo._load_serials()
zwo._remember("camera", 3, "2a20560d0b020900", "RASA-L")
zwo._remember("camera", 1, "1807921309020900", "")
zwo._remember("focuser", 2, "0f2411402c91a781", "RASA-R")
case("aliases are remembered against the serial, not the index",
     zwo.known_alias("camera", "2a20560d0b020900") == "RASA-L"
     and zwo.serial_for_alias("camera", "rasa-l") == "2a20560d0b020900"
     and zwo.aliases("camera") == {"2a20560d0b020900": "RASA-L"})
case("an unnamed body is not asked again",
     zwo._alias_asked("camera", "1807921309020900") and not zwo._alias_asked("camera", "nope"))
# The USB tree came up in a different order: the body that was number 1 is
# now number 2.  The old entry must go, or two numbers claim one serial and
# whatever is really at number 1 is mislabelled.
zwo._remember("camera", 2, "1807921309020900", "RASA-A")
case("a serial read at a new device number leaves its old number",
     zwo._cached_index("camera", "1807921309020900") == 2
     and 1 not in zwo._serials["camera"])
zwo._remember("camera", 1, "082b941309020900", "")
settled = zwo._settle_duplicates("camera", [(1, "082b941309020900", True),
                                            (2, "1807921309020900", True),
                                            (3, "2a20560d0b020900", True)])
case("a listing that reads every body keeps every serial",
     settled == {1: "082b941309020900", 2: "1807921309020900", 3: "2a20560d0b020900"})
zwo._remember("camera", 1, "1807921309020900", "RASA-A")   # a stale entry at 1
zwo._serials["camera"][2] = "1807921309020900"             # ...and the body at 2
settled = zwo._settle_duplicates("camera", [(1, "1807921309020900", False),
                                            (2, "1807921309020900", True),
                                            (3, "2a20560d0b020900", True)])
case("a cached serial that was read from another body is dropped, not duplicated",
     settled == {1: "", 2: "1807921309020900", 3: "2a20560d0b020900"}
     and 1 not in zwo._serials["camera"])
settled = zwo._settle_duplicates("camera", [(1, "1807921309020900", False),
                                            (2, "1807921309020900", False)])
case("two unreadable bodies sharing a cached serial are left alone",
     settled == {1: "1807921309020900", 2: "1807921309020900"})
zwo._remember("camera", 1, "1807921309020900", "")
zwo._serials["camera"].pop(2, None)
# What a stored id promises at connect: the serial itself, the alias's
# serial, nothing for a bare index.  Connect compares the body that opened
# against this, so a USB reorder opens the right camera or none.
case("a stored id says which serial must answer",
     zwo._expected_serial("camera", "2a20560d0b020900") == "2a20560d0b020900"
     and zwo._expected_serial("camera", "alias:RASA-L") == "2a20560d0b020900"
     and zwo._expected_serial("camera", "alias:nobody") == ""
     and zwo._expected_serial("camera", "index:3") == "")
case("the name on screen leads with the alias and ends with the whole serial",
     zwo.display_name("camera", "ZWO ASI2600MM Pro", "2a20560d0b020900")
     == "ZWO ASI2600MM Pro (RASA-L) — sn 2a20560d0b020900")
case("an alias address resolves to the device holding that name",
     zwo._resolve("camera", "alias:RASA-L") == 3)
try:
    zwo._resolve("camera", "alias:RASA-Q")
    case("an unknown alias is refused with the names seen so far", False)
except DeviceError as exc:
    case("an unknown alias is refused with the names seen so far",
         "RASA-Q" in str(exc) and "RASA-L" in str(exc) and "N.I.N.A." in str(exc), str(exc)[:80])

zwo._serials_loaded = False
zwo._serials["camera"].clear(); zwo._serials["focuser"].clear()
zwo._aliases["camera"].clear(); zwo._aliases["focuser"].clear()
zwo._load_serials()
case("...and all of it survives a restart",
     zwo.known_alias("focuser", "0f2411402c91a781") == "RASA-R"
     and zwo._cached_index("camera", "2a20560d0b020900") == 3)

# ---------------------------------------------------------------------------
print("\n-- N.I.N.A.'s own ZWO driver --")

case("N.I.N.A.'s native camera id yields an alias-addressed slot",
     ninaimport.zwo_native_device("camera", "ZWOptical_ZWO ASI2600MM Pro_RASA-L", "x")
     == {"backend": "zwo", "driverId": "alias:RASA-L", "name": "ZWO ASI2600MM Pro (RASA-L)",
         "alias": "RASA-L", "model": "ZWO ASI2600MM Pro"})
case("...an id with no alias is left for a person",
     ninaimport.zwo_native_device("camera", "ZWOptical_ZWO ASI2600MM Pro_", "x") is None
     and ninaimport.zwo_native_device("camera", "ASCOM.ASICamera2.Camera", "x") is None
     and ninaimport.zwo_native_device("focuser", "ZWOptical_EAF_x", "x") is None)

P = "http://schemas.datacontract.org/2004/07/NINA.Profile"
I = "http://www.w3.org/2001/XMLSchema-instance"


def profile(name, alias, mount=True, guider=True, focal=400, gain=100, last="2026-09-15T20:29:48",
            folder=None):
    return f"""<Profile xmlns="{P}" xmlns:i="{I}">
<AstrometrySettings><Elevation>300</Elevation><Latitude>30.1</Latitude><Longitude>-97.7</Longitude></AstrometrySettings>
<CameraSettings><BayerPattern>Auto</BayerPattern><BitDepth>16</BitDepth><Gain>{gain}</Gain>
<Id>ZWOptical_ZWO ASI2600MM Pro_{alias}</Id><LastDeviceName>ZWO ASI2600MM Pro ({alias})</LastDeviceName>
<Offset>50</Offset><PixelSize>3.76</PixelSize><Temperature>-10</Temperature></CameraSettings>
<FilterWheelSettings><Id>No_Device</Id></FilterWheelSettings>
<FocuserSettings><AutoFocusExposureTime>6</AutoFocusExposureTime><AutoFocusStepSize>120</AutoFocusStepSize>
<AutoFocusInitialOffsetSteps>4</AutoFocusInitialOffsetSteps><Id>ASCOM.EAF.Focuser</Id><LastDeviceName>ZWO Focuser (ASCOM)</LastDeviceName></FocuserSettings>
<FramingAssistantSettings><CameraHeight>4176</CameraHeight><CameraWidth>6248</CameraWidth></FramingAssistantSettings>
<GuiderSettings><GuiderName>{'PHD2_Single' if guider else 'No_Guider'}</GuiderName><PHD2ServerPort>4400</PHD2ServerPort><PHD2ServerUrl>localhost</PHD2ServerUrl></GuiderSettings>
<Id>{name}</Id>
<ImageFileSettings><FilePath>{folder or 'C:\\\\frames\\\\' + name}</FilePath></ImageFileSettings>
<LastUsed>{last}</LastUsed><Name>{name}</Name>
<TelescopeSettings><FocalLength>{focal}</FocalLength><Id>{'ASCOM.OnStep.Telescope' if mount else 'No_Device'}</Id><LastDeviceName>On-Step</LastDeviceName></TelescopeSettings>
</Profile>"""


folder = Path(tempfile.mkdtemp())
(folder / "rasa1.profile").write_text(profile("rasa1", "RASA-L"), encoding="utf-8")
(folder / "rasa2.profile").write_text(profile("rasa2", "RASA-A", guider=False, gain=120), encoding="utf-8")
(folder / "rasa3.profile").write_text(profile("rasa3", "RASA-R", mount=False, guider=False), encoding="utf-8")
plans = [ninaimport.read(folder / f"{n}.profile") for n in ("rasa3", "rasa2", "rasa1")]

case("a profile read carries the camera by alias",
     plans[2]["devices"]["camera"]["driverId"] == "alias:RASA-L"
     and ninaimport.camera_alias(plans[2]) == "RASA-L"
     and not any("ZWOptical" in line for line in plans[2]["leftOut"]))

print("\n-- one plan for all three --")
rigs_now = [{"id": "main", "name": "Telescope 1", "role": "master", "devices": {}},
            {"id": "b", "name": "Telescope 2", "role": "slave", "devices": {}},
            {"id": "c", "name": "Telescope 3", "role": "slave", "devices": {}}]
cameras_seen = [{"id": "2a20560d0b020900", "alias": "RASA-L", "name": "ZWO ASI2600MM Pro (RASA-L) — sn 2a20560d0b020900"}]
focusers_seen = [{"id": "0f2411402c91a781", "alias": "RASA-R", "name": "ZWO EAF (RASA-R) — sn 0f2411402c91a781"},
                 {"id": "0f24118039bd6281", "alias": "RASA-A", "name": "ZWO EAF (RASA-A) — sn 0f24118039bd6281"},
                 {"id": "0f241190048b0e51", "alias": "RASA3", "name": "ZWO EAF (RASA3) — sn 0f241190048b0e51"}]
plan = ninaimport.setup_plan(plans, rigs_now, cameras_seen, focusers_seen,
                             {"RASA-A": "082b941309020900"})
t = plan["telescopes"]
case("the profile with the mount and the guider comes first, as the master",
     [x["name"] for x in t] == ["rasa1", "rasa2", "rasa3"] and t[0]["master"] and not t[1]["master"],
     str([x["name"] for x in t]))
case("each profile is laid onto the telescope in the same position",
     [x["rig"] for x in t] == ["main", "b", "c"])
case("a camera that can be seen is addressed by its serial",
     t[0]["devices"]["camera"] == {"backend": "zwo", "driverId": "2a20560d0b020900",
                                   "name": "ZWO ASI2600MM Pro (RASA-L)", "alias": "RASA-L",
                                   "status": "found",
                                   "note": "RASA-L is plugged in — serial 2a20560d0b020900"},
     str(t[0]["devices"]["camera"]))
case("...one learned on an earlier day too",
     t[1]["devices"]["camera"]["driverId"] == "082b941309020900"
     and t[1]["devices"]["camera"]["status"] == "found")
case("...and one never yet opened is kept by its name for later",
     t[2]["devices"]["camera"]["driverId"] == "alias:RASA-R"
     and t[2]["devices"]["camera"]["status"] == "byAlias")
case("focusers named after the cameras are paired with them",
     t[1]["devices"]["focuser"]["driverId"] == "0f24118039bd6281"
     and t[1]["devices"]["focuser"]["status"] == "matched"
     and t[2]["devices"]["focuser"]["driverId"] == "0f2411402c91a781"
     and t[2]["devices"]["focuser"]["status"] == "matched")
case("...and the one left over is offered, marked as a guess",
     t[0]["devices"]["focuser"]["driverId"] == "0f241190048b0e51"
     and t[0]["devices"]["focuser"]["status"] == "leftover"
     and "Which focuser" in t[0]["devices"]["focuser"]["note"])
case("the mount and the guider stay with the master",
     "mount" in t[0]["devices"] and "guider" in t[0]["devices"]
     and "mount" not in t[1]["devices"] and any("mount left" in n for n in t[1]["notes"]))
case("the summary says what a person checks a profile by",
     "400 mm" in t[1]["summary"] and "gain 120" in t[1]["summary"], t[1]["summary"])

plan2 = ninaimport.setup_plan(plans, rigs_now, [], [], {})
case("with no ZWO focuser in sight the shared ASCOM driver is kept and flagged",
     all(x["devices"]["focuser"]["status"] == "shared" for x in plan2["telescopes"]))
plan3 = ninaimport.setup_plan(plans[:1], rigs_now, [], [], {})
case("spare telescopes are mentioned, not touched",
     any("Telescope 2, Telescope 3" in n for n in plan3["notes"]), str(plan3["notes"]))

print("\n-- carrying it out --")


class FakeRig:
    def __init__(self, definition, config, store):
        import types
        from astrocontrol.rigs import RigConfig
        self.id = definition["id"]
        self.name = definition["name"]
        self.role = definition["role"]
        self.config = RigConfig(config, store, self.id, primary=self.role == "master")
        # No devices at all: the case a drawer telescope is in all afternoon.
        self.manager = types.SimpleNamespace(get=lambda kind: None, log=lambda *a, **k: None)


class FakeRigSet:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.sync()

    def sync(self):
        self.all = [FakeRig(d, self.config, self.store) for d in self.store.rigs()]

    def get(self, rig_id):
        return next(r for r in self.all if r.id == rig_id)


config = Config(Path(tempfile.mkdtemp()) / "settings.json")
store = EquipmentStore(Path(tempfile.mkdtemp()) / "equipment.json")
fake_rigs = FakeRigSet(config, store)
# Only one telescope exists; the plan was made against three, and the person
# has moved the master to rasa2 and renamed rasa3.
plan_one = ninaimport.setup_plan(plans, store.rigs(), cameras_seen, focusers_seen, {})
edited = plan_one["telescopes"]
edited[0]["master"], edited[1]["master"] = False, True
edited[2]["name"] = "Right RASA"
done = ninaimport.apply_setup(edited, ninaimport.read, config, store, fake_rigs)
names = [(r["name"], r["role"]) for r in store.rigs()]
case("telescopes are made and named, the chosen one master first",
     names == [("rasa2", "master"), ("rasa1", "slave"), ("Right RASA", "slave")], str(names))
case("the existing telescope was reused rather than duplicated",
     any(r["id"] == "main" for r in store.rigs()) and len(store.rigs()) == 3)
master = next(r for r in store.rigs() if r["role"] == "master")
slave = next(r for r in store.rigs() if r["name"] == "rasa1")
case("the master's profile writes the shared settings; gain is per telescope",
     config.get("site", "latitude") == 30.1 and config.get("camera", "gain") == 120
     and store.rig_settings(slave["id"], "camera").get("gain") == 100)
case("devices are remembered where the plan put them",
     master["devices"]["camera"]["driverId"] == "082b941309020900" or
     master["devices"]["camera"]["driverId"] == "alias:RASA-A")
case("...the mount only on the master",
     "mount" in master["devices"] and "mount" not in slave["devices"])
case("each telescope says what it was given",
     sorted(d["name"] for d in done["telescopes"]) == ["Right RASA", "rasa1", "rasa2"]
     and all(d["devices"] for d in done["telescopes"]))

print("\n-- what the collaboration is told --")
# Three drawer telescopes, no wheel anywhere, nothing typed into any slot
# list: the collaboration profile used to say "no filters" and the server's
# pre-flight check refused the rig. Now it carries what is in the drawers.
fake_rigs.sync()
for rig_obj, fitted in zip(fake_rigs.all, ["L", "Ha", "OIII"]):
    rig_obj.config.update("camera", {"filterNames": [], "fixedFilter": fitted})
fake_rigs.master = next(r for r in fake_rigs.all if r.role == "master")
client = collabclient.CollabClient(fake_rigs, config)
carried, _bands = client._carried_filters()
# Stored in the program's one spelling: Ha is H, OIII is O.
case("the profile carries every telescope's drawer filter",
     sorted(carried) == ["H", "L", "O"], str(carried))
fake_rigs.all[1].config.update("camera", {"fixedFilter": "L"})
carried, _bands = client._carried_filters()
case("two telescopes with the same filter count it once",
     sorted(carried) == ["L", "O"], str(carried))
wants = collab.Requirements.read({"filters": {"Ha": None}})
empty = collab.RigProfile.read({"name": "rig", "focalLength": 400, "pixelSize": 3.76,
                                "sensorWidth": 6248, "sensorHeight": 4176, "filters": {}})
verdict = collab.compatibility(empty, wants)
text = " ".join(c["detail"] for c in verdict.get("checks", []) if c["check"] == "filters")
case("the refusal for an empty filter list now points at the drawer setting",
     "light path" in text and "no wheel" in text, text[:160])
# And with the drawers declared, the same project takes the rig.
full_profile = collab.RigProfile.read(
    {**{"name": "rig", "focalLength": 400, "pixelSize": 3.76, "sensorWidth": 6248,
        "sensorHeight": 4176}, "filters": {name: None for name in ["L", "Ha", "OIII"]}})
verdict = collab.compatibility(full_profile, wants)
ok = [c for c in verdict.get("checks", []) if c["check"] == "filters"]
case("...and a declared drawer filter satisfies a project that asks for it",
     bool(ok) and ok[0]["ok"] is True, str(ok))
# Three RASAs carrying R, G and B on a project that wants LRGB and Ha/OIII:
# it can shoot three of the six, so it can contribute, and the verdict says
# which. It used to be refused for the three it lacked.
rgb = collab.RigProfile.read(
    {"name": "rgb", "focalLength": 400, "pixelSize": 3.76, "sensorWidth": 6248,
     "sensorHeight": 4176, "filters": {"R": None, "G": None, "B": None}})
mixed = collab.Requirements.read({"filters": {"L": None, "R": None, "G": None,
                                              "B": None, "Ha": 5.0, "OIII": 5.0}})
verdict = collab.compatibility(rgb, mixed)
row = next((c for c in verdict["checks"] if c["check"] == "filters"), {})
case("a rig carrying some of the wanted filters can contribute",
     verdict["ok"] is True and row.get("ok") is True
     and "can shoot R, G, B" in row.get("detail", "")
     and "no L, H, O" in row.get("detail", ""),
     row.get("detail"))
nothing = collab.compatibility(rgb, collab.Requirements.read({"filters": {"Ha": None}}))
case("...but a rig carrying none of them is still refused", nothing["ok"] is False,
     nothing["summary"])

print("\n-- saving survives overlapping borrowers --")
# The night of 2026-10-08: a focus sweep from a stopped run was still
# restoring saving when the next run's sweep began; each put back the other's
# answer and rasa1's lights for a whole target went unsaved.
import types                                                               # noqa: E402
from astrocontrol.capture import CaptureService                            # noqa: E402
cs = CaptureService(types.SimpleNamespace(log=lambda *a, **k: None, get=lambda k: None,
                                          require=lambda k: None))
cs.save_enabled = True
cs.suspend_saving()                      # sweep A starts
cs.suspend_saving()                      # sweep B starts while A is running
case("saving is off while any borrower holds it", cs.save_enabled is False)
cs.resume_saving()                       # A finishes
case("...and stays off until the last one is done", cs.save_enabled is False)
cs.resume_saving()                       # B finishes
case("...then comes back to what it was", cs.save_enabled is True)
cs.suspend_saving()
cs.set_output(save=False)                # the person turns saving off mid-sweep
cs.resume_saving()
case("a choice made during a sweep is what comes back afterwards", cs.save_enabled is False)
cs.set_output(save=True)
cs.resume_saving()                       # one resume too many
case("a stray resume does nothing", cs.save_enabled is True and cs._save_suspended == 0)

print("\n-- a dropped frame gets the camera back --")
# Capture, Loop, a focus frame or a centring frame: when the camera fails
# the frame, it is disconnected, brought back on its remembered driver and
# the frame tried once more. Off by a setting; nothing happens without a
# way to reconnect.
camera_stub = types.SimpleNamespace(connected=True, binning=1, gain=0, offset=0,
                                    abort_exposure=lambda: None)
events = []
mgr = types.SimpleNamespace(log=lambda m, *a, **k: events.append(m),
                            get=lambda k: camera_stub if k == "camera" else None,
                            require=lambda k: camera_stub,
                            disconnect=lambda k: events.append(f"disconnect {k}"))
cs = CaptureService(mgr)
attempts = {"n": 0}
def flaky(exposure, frame_type):
    attempts["n"] += 1
    if attempts["n"] == 1:
        raise DeviceError("the camera reported the exposure failed")
    cs._latest_id = "frame-1"
cs._capture_one = flaky
cs.record = lambda image_id: {"id": image_id}
cs.reconnect_camera = lambda: events.append("reconnect")
cs.capture_blocking(1.0)
case("a failed frame reconnects the camera and tries once more",
     attempts["n"] == 2 and "disconnect camera" in events and "reconnect" in events,
     str(events[-4:]))
attempts["n"] = 0; events.clear()
cs.config.update("recovery", {"reconnectManualCaptures": False})
try:
    cs.capture_blocking(1.0)
    case("...and with the setting off the failure stands", False)
except DeviceError as exc:
    case("...and with the setting off the failure stands",
         attempts["n"] == 1 and "reconnect" not in events, str(exc))
cs.config.update("recovery", {"reconnectManualCaptures": True})
cs.reconnect_camera = None
attempts["n"] = 0
try:
    cs.capture_blocking(1.0)
    case("...and with nothing to reconnect with, likewise", False)
except DeviceError:
    case("...and with nothing to reconnect with, likewise", attempts["n"] == 1)

print("\n-- what each telescope shoots for a target --")
# One allocation typed for the master (R), R, G and B fitted across the three
# drawers: the followers shoot the master's frames through their own filter.
from astrocontrol import plans as _plans                                   # noqa: E402
fake_rigs.sync()
master_rig = next(r for r in fake_rigs.all if r.role == "master")
followers = [r for r in fake_rigs.all if r.role != "master"]
master_rig.config.update("camera", {"fixedFilter": "R", "filterNames": []})
followers[0].config.update("camera", {"fixedFilter": "G", "filterNames": []})
followers[1].config.update("camera", {"fixedFilter": "B", "filterNames": []})
fitted_now = _plans.fitted_filters(fake_rigs.all)
case("every drawer telescope reports its fitted filter",
     fitted_now == {master_rig.id: "R", followers[0].id: "G", followers[1].id: "B"}, str(fitted_now))
entry = {"id": "e1", "filters": [{"name": "R", "exposure": 120, "count": 20}]}
case("the master shoots what was typed",
     _plans.allocation_for(entry, master_rig.id, master_rig.id, fitted_now)
     == [{"name": "R", "exposure": 120, "count": 20}])
case("a follower shoots the same frames through its own filter",
     _plans.allocation_for(entry, followers[0].id, master_rig.id, fitted_now)
     == [{"name": "G", "exposure": 120, "count": 20}]
     and _plans.allocation_for(entry, followers[1].id, master_rig.id, fitted_now)
     == [{"name": "B", "exposure": 120, "count": 20}])
two = {"id": "e2", "filters": [{"name": "R", "exposure": 120, "count": 10},
                               {"name": "L", "exposure": 120, "count": 5},
                               {"name": "R", "exposure": 60, "count": 4}]}
case("several rows fold into one per exposure length, counts added",
     _plans.allocation_for(two, followers[0].id, master_rig.id, fitted_now)
     == [{"name": "G", "exposure": 120, "count": 15}, {"name": "G", "exposure": 60, "count": 4}])
own_entry = {"id": "e3", "filters": [{"name": "R", "exposure": 120, "count": 20}],
             "rigFilters": {followers[0].id: [{"name": "G", "exposure": 300, "count": 3}]}}
case("a follower with its own allocation keeps it",
     _plans.allocation_for(own_entry, followers[0].id, master_rig.id, fitted_now)
     == [{"name": "G", "exposure": 300, "count": 3}])
case("without the fitted table nothing changes",
     _plans.allocation_for(entry, followers[0].id, master_rig.id)
     == [{"name": "R", "exposure": 120, "count": 20}])

# ---------------------------------------------------------------------------
print("\n-- which focuser is which --")

rng = np.random.default_rng(7)
STARS = [(rng.uniform(40, 760), rng.uniform(40, 1160), rng.uniform(3000, 20000)) for _ in range(40)]


def star_field(sigma):
    yy, xx = np.mgrid[0:800, 0:1200]
    frame = np.full((800, 1200), 800.0)
    for y, x, flux in STARS:
        frame += flux / (2 * np.pi * sigma ** 2) * np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * sigma ** 2)) * 8
    frame += rng.normal(0, 12, frame.shape)
    return np.clip(frame, 0, 65535).astype(np.uint16)


sharp, soft = star_field(1.6), star_field(3.4)
m_sharp, m_soft = identify.measure_frame(sharp), identify.measure_frame(soft)
case("stars are measured and the softer frame has the larger HFR",
     m_sharp["metric"] == "hfr" and m_soft["metric"] == "hfr" and m_soft["hfr"] > m_sharp["hfr"] * 1.5,
     f"{m_sharp['hfr']} -> {m_soft['hfr']} from {m_sharp['stars']} stars")
case("sharpness falls as focus goes",
     identify.sharpness(sharp) > identify.sharpness(soft) * 1.5)
change, metric = identify.change_between(m_sharp, m_soft)
case("the change between them is large and by HFR", metric == "hfr" and change > 0.4, f"{change:.2f}")
flat = (np.full((300, 300), 500) + rng.normal(0, 5, (300, 300))).astype(np.uint16)
case("a starless frame falls back to sharpness",
     identify.measure_frame(flat)["metric"] == "sharpness")

verdicts = identify.decide({"a": {"a": 0.8, "b": 0.05, "c": 0.02},
                            "b": {"a": 0.03, "b": 0.04, "c": 0.9},
                            "c": {"a": 0.1, "b": 0.08, "c": 0.09}})
case("a focuser is given the camera whose stars moved",
     verdicts["a"]["camera"] == "a" and verdicts["a"]["clear"]
     and verdicts["b"]["camera"] == "c" and verdicts["b"]["clear"])
case("...and one that moved nobody says so",
     verdicts["c"]["camera"] is None and "no camera" in verdicts["c"]["why"])
case("two cameras responding is called, but not called clear",
     not identify.decide({"a": {"a": 0.5, "b": 0.4}})["a"]["clear"])
case("the moves are only the ones that cross telescopes",
     identify.swaps_needed(verdicts) == {"c": "b"})
try:
    identify.swaps_needed({"a": {"camera": "x"}, "b": {"camera": "x"}})
    case("two focusers on one camera is refused", False)
except DeviceError:
    case("two focusers on one camera is refused", True)


class FakeFocuser(Focuser):
    def __init__(self, name):
        super().__init__(name, name)
        self._connected = True
        self._position = 20000
        self.max_step = 60000

    @property
    def position(self):
        return self._position

    @property
    def moving(self):
        return False

    def move_to(self, position):
        self._position = int(position)

    def halt(self):
        pass


class FakeCamera(Camera):
    """A camera whose star size follows one particular focuser."""

    def __init__(self, name, follows: FakeFocuser, best=20000):
        super().__init__(name, name)
        self._connected = True
        self.follows, self.best = follows, best

    def frame(self):
        sigma = 1.6 + 0.004 * abs(self.follows.position - self.best)
        return star_field(sigma)


class FakeCapture:
    def __init__(self, camera):
        self.camera = camera
        self.save_enabled = True
        self.busy = False
        self._frames = {}
        self._suspended = 0

    def suspend_saving(self):
        if self._suspended == 0:
            self._save_before = self.save_enabled
            self.save_enabled = False
        self._suspended += 1

    def resume_saving(self):
        if self._suspended == 0:
            return
        self._suspended -= 1
        if self._suspended == 0:
            self.save_enabled = self._save_before

    def capture_blocking(self, exposure, frame_type="light"):
        self.busy = True
        try:
            time.sleep(0.01)
            frame = self.camera.frame()
            key = f"f{len(self._frames)}"
            self._frames[key] = frame

            class Record:
                id = key
            return Record()
        finally:
            self.busy = False

    def frame(self, image_id):
        return self._frames[image_id]


class FakeManager:
    def __init__(self, camera, focuser):
        self.devices = {"camera": camera, "focuser": focuser}
        self.lines = []

    def get(self, kind):
        return self.devices.get(kind)

    def require(self, kind):
        device = self.devices.get(kind)
        if device is None:
            raise DeviceError(f"{kind} is not connected")
        return device

    def disconnect(self, kind):
        self.devices[kind] = None

    def log(self, text, level="info"):
        self.lines.append(text)


class FakeAutoFocuser:
    running = False

    def settings(self):
        return {"focusStepSize": 100, "focusExposure": 1}


class CheckRig:
    def __init__(self, rig_id, name, camera, focuser):
        self.id, self.name = rig_id, name
        self.manager = FakeManager(camera, focuser)
        self.capture = FakeCapture(camera)
        self.focuser = FakeAutoFocuser()


class CheckRigSet:
    def __init__(self, rigs, store):
        self.all, self.store = rigs, store
        self.reconnected = []

    def get(self, rig_id):
        return next(r for r in self.all if r.id == rig_id)

    def connect_remembered(self, rig, kinds=None):
        spec = self.store.rig(rig.id)["devices"].get("focuser")
        if spec is None:
            return {"connected": [], "failed": []}
        rig.manager.devices["focuser"] = FakeFocuser(spec["driverId"])
        self.reconnected.append((rig.id, spec["driverId"]))
        return {"connected": ["focuser"], "failed": []}


# Three focusers; the one in telescope 2's slot really drives telescope 3's
# camera and vice versa.  Telescope 1 is fine.
f1, f2, f3 = FakeFocuser("EAF-1"), FakeFocuser("EAF-2"), FakeFocuser("EAF-3")
rig1 = CheckRig("t1", "RASA 1", FakeCamera("cam1", f1), f1)
rig2 = CheckRig("t2", "RASA 2", FakeCamera("cam2", f3), f2)
rig3 = CheckRig("t3", "RASA 3", FakeCamera("cam3", f2), f3)
store2 = EquipmentStore(Path(tempfile.mkdtemp()) / "equipment.json")
store2.add_rig("RASA 2"); store2.add_rig("RASA 3")
ids = [r["id"] for r in store2.rigs()]
for rig, rid in zip((rig1, rig2, rig3), ids):
    rig.id = rid
for rig, foc in zip((rig1, rig2, rig3), (f1, f2, f3)):
    store2.set_device(rig.id, "focuser", {"backend": "zwo", "driverId": foc.name, "name": foc.name})
rigset = CheckRigSet([rig1, rig2, rig3], store2)
check = identify.PairingCheck(rigset)
check.start(steps=400, exposure=1)
deadline = time.monotonic() + 60
while check.running and time.monotonic() < deadline:
    time.sleep(0.05)
status = check.status()
result = status["result"] or {}
rows = {row["name"]: row for row in result.get("rows", [])}
case("the check finishes and reports", not status["running"] and status["error"] is None and rows,
     status["message"])
case("the focuser on its own telescope is recognised",
     rows.get("RASA 1", {}).get("ownCamera") is True and rows["RASA 1"]["clear"])
case("the crossed pair is found",
     rows.get("RASA 2", {}).get("cameraName") == "RASA 3"
     and rows.get("RASA 3", {}).get("cameraName") == "RASA 2",
     str({k: (v["cameraName"], v["changePercent"]) for k, v in rows.items()}))
case("every focuser went back where it started",
     f1.position == 20000 and f2.position == 20000 and f3.position == 20000)
case("the fix is described as two moves",
     sorted((m["fromName"], m["toName"]) for m in result.get("moves", []))
     == [("RASA 2", "RASA 3"), ("RASA 3", "RASA 2")], str(result.get("moves")))
case("the log tells the story in order",
     status["log"] and "Measuring" in status["log"][0] and "Done" in status["log"][-1])
fixed = check.fix(store2)
case("applying it swaps what the two telescopes remember and reconnects them",
     store2.rig(rig2.id)["devices"]["focuser"]["driverId"] == "EAF-3"
     and store2.rig(rig3.id)["devices"]["focuser"]["driverId"] == "EAF-2"
     and store2.rig(rig1.id)["devices"]["focuser"]["driverId"] == "EAF-1"
     and sorted(rigset.reconnected) == sorted([(rig2.id, "EAF-3"), (rig3.id, "EAF-2")]),
     str(fixed))
case("...and says so", sorted(fixed["changed"]) == sorted([rig2.id, rig3.id]) and not fixed["failed"])

try:
    identify.PairingCheck(CheckRigSet([rig1], store2)).start()
    case("one telescope alone is refused", False)
except DeviceError as exc:
    case("one telescope alone is refused", "nothing to tell apart" in str(exc))

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
