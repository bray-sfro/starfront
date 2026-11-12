"""Exercise the power box: Unity's routes, outlet matching, and the power cycle.

    python tools/check_power.py

Three things that only ever matter at three in the morning, with a camera
that has dropped off the USB bus:

  * the Pegasus Unity backend reads a box's report into named channels and
    sends the right route when one is switched — checked against a captured
    Ultimate Powerbox v3 report, with the HTTP layer stubbed;
  * each device is matched to its own outlets — assigned on the Power page,
    named after the telescope in Unity, or (for a camera) ticked on the
    0.2.46 Camera page — and never to another telescope's;
  * `RigSet.reconnect_camera` tries a plain reconnect first, goes to the mains
    when that fails or when the same camera is back again inside the window,
    puts every outlet back even when stopped half way, and stays out of it
    when switched off in the settings.

No test framework, as with the other checks here.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol import rigs as rigs_module
from astrocontrol.config import Config
from astrocontrol.devices import pegasus
from astrocontrol.devices.base import DeviceError
from astrocontrol.equipment import EquipmentStore

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# ---------------------------------------------------------------- Unity ----
# A report captured from the user's box on 2026-10-20: six 12 V outputs, eight
# USB ports (named after the telescopes), three dew heaters, a relay and the
# two adjustable outputs.  Only the parts the backend reads are kept.
REPORT = {
    "voltage": 12.8, "current": 0.37, "power": 4.736, "temperature": 25.6,
    "humidity": 52, "dewPoint": 15,
    "pwmHubStatus": {"hub": [
        {"portNumber": 1, "level": 100, "current": 0, "name": "Output1"},
        {"portNumber": 2, "level": 100, "current": 0, "name": "Output2"},
        {"portNumber": 3, "level": 100, "current": 0, "name": "Output3"},
        {"portNumber": 4, "level": 100, "current": 3, "name": "RASA1"},
        {"portNumber": 5, "level": 100, "current": 4, "name": "RASA2"},
        {"portNumber": 6, "level": 0, "current": 0, "name": "RASA3"},
    ]},
    "buck": {"type": "Buck", "state": {"state": "ON"}, "voltage": 3},
    "boost": {"type": "Boost", "state": {"state": "ON"}, "voltage": 12},
    "relay": {"state": {"state": "OFF"}},
    "dewHubStatus": {"hub": [
        {"portNumber": 1, "power": 40, "name": "Dew1"},
        {"portNumber": 2, "power": 0, "name": "Dew2"},
        {"portNumber": 3, "power": 0, "name": "Dew3"},
    ]},
    "usbHubStatus": {"hub": [
        {"portNumber": 1, "switch": {"state": "ON"}, "name": "rasa3"},
        {"portNumber": 2, "switch": {"state": "ON"}, "name": "eaf3"},
        {"portNumber": 3, "switch": {"state": "ON"}, "name": "rasa2"},
        {"portNumber": 4, "switch": {"state": "ON"}, "name": "eaf2"},
        {"portNumber": 5, "switch": {"state": "ON"}, "name": "rasa1"},
        {"portNumber": 6, "switch": {"state": "ON"}, "name": "eaf1"},
        {"portNumber": 7, "switch": {"state": "OFF"}, "name": "USB7"},
        {"portNumber": 8, "switch": {"state": "ON"}, "name": "USB8"},
    ]},
}
CONNECTED = [{"uniqueKey": "a1ac4ec6-0000-0000-0000-000000000000", "name": "UPBv3",
              "fullName": "Ultimate PowerBox v3", "deviceID": "UPBv300091926",
              "firmware": "1.4"}]

calls = []


def fake_request(method, path, host=pegasus.DEFAULT_HOST, query=None, timeout=5.0):
    calls.append((method, path, dict(query or {})))
    if path == "/Server/DeviceManager/Connected":
        return {"status": "success", "data": CONNECTED}
    if path.endswith("/Report"):
        return {"status": "success", "data": {"message": REPORT}}
    if path.endswith("/Start"):
        return {"status": "success"}
    return {"status": "success", "data": {}}


pegasus.request = fake_request

print("-- the Pegasus Unity backend --")
listed = pegasus.list_devices("switch")
case("Unity's connected list becomes one driver entry per box",
     listed == [{"id": "UPBv3/UPBv300091926",
                 "name": "Pegasus Ultimate PowerBox v3 (UPBv300091926) via Unity",
                 "device": "Ultimate PowerBox v3"}], str(listed))
case("nothing but a switch is offered", pegasus.list_devices("camera") == [])

box = pegasus.create("switch", "UPBv3/UPBv300091926", "")
box.connect()
case("connecting finds the box by serial and takes its unique key",
     box.connected and box.unique_key == CONNECTED[0]["uniqueKey"]
     and all(q.get("DriverUniqueKey") == box.unique_key
             for m, p, q in calls if p.startswith("/Driver/")),
     f"key={box.unique_key!r}")
case("...and starts its driver first", ("OPTIONS", "/Driver/UPBv3/Start") in
     [(m, p) for m, p, q in calls])
channels = box.channels
names = [c["name"] for c in channels]
case("channels are the 12 V outputs, USB ports, dew heaters, relay and adjustables, in order",
     names == ["Output1", "Output2", "Output3", "RASA1", "RASA2", "RASA3",
               "rasa3", "eaf3", "rasa2", "eaf2", "rasa1", "eaf1", "USB7", "USB8",
               "Dew1", "Dew2", "Dew3", "Relay", "Buck output", "Boost output"], str(names))
case("indices are stable and sequential",
     [c["index"] for c in channels] == list(range(20)))
by_name = {c["name"]: c for c in channels}
case("a 12 V output's state comes from its level",
     by_name["RASA1"]["value"] == 1.0 and by_name["RASA3"]["value"] == 0.0)
case("a USB port's state comes from its switch",
     by_name["rasa1"]["value"] == 1.0 and by_name["USB7"]["value"] == 0.0)
case("a dew heater is a 0-100 value, not a relay",
     not by_name["Dew1"]["boolean"] and by_name["Dew1"]["max"] == 100.0
     and by_name["Dew1"]["value"] == 40.0)
case("the relay and the adjustable outputs are read",
     by_name["Relay"]["value"] == 0.0 and by_name["Buck output"]["value"] == 1.0
     and "3 V" in by_name["Buck output"]["description"])
case("describe_channels costs no call to the box",
     (lambda n: (box.describe_channels(), len(calls) == n)[1])(len(calls)))
case("the box's meters are reported",
     box.readings() == {"voltage": 12.8, "current": 0.37, "power": 4.736,
                        "temperature": 25.6, "humidity": 52, "dewPoint": 15})

del calls[:]
box.set_value(by_name["rasa1"]["index"], 0)
box.set_value(by_name["RASA1"]["index"], 0)
box.set_value(by_name["rasa1"]["index"], 1)
box.set_value(by_name["RASA1"]["index"], 1)
box.set_value(by_name["Dew1"]["index"], 55)
box.set_value(by_name["Dew1"]["index"], 0)
box.set_value(by_name["Relay"]["index"], 1)
box.set_value(by_name["Boost output"]["index"], 0)
sent = [(m, p) for m, p, q in calls if not p.endswith("/Report")]
case("switching sends Unity's own routes with PUT",
     sent == [("PUT", "/Driver/UPBv3/USB/5/Off"), ("PUT", "/Driver/UPBv3/PWM/4/Off"),
              ("PUT", "/Driver/UPBv3/USB/5/On"), ("PUT", "/Driver/UPBv3/PWM/4/On"),
              ("PUT", "/Driver/UPBv3/Dew/1/55"), ("PUT", "/Driver/UPBv3/Dew/1/Off"),
              ("PUT", "/Driver/UPBv3/Relay/On"),
              ("PUT", "/Driver/UPBv3/Adjustable/Boost/Off")], str(sent))
try:
    box.set_value(99, 1)
    case("an unknown channel is refused", False)
except DeviceError as exc:
    case("an unknown channel is refused", "no channel 99" in str(exc))

model, serial, host = pegasus._split_driver_id("UPBv2/UPB2ABC@10.0.0.5:32000")
case("a driver id can name a Unity on another PC",
     (model, serial, host) == ("UPBv2", "UPB2ABC", "10.0.0.5:32000"))
try:
    pegasus.create("switch", "Toaster/1", "")
    case("an unknown model is refused", False)
except DeviceError as exc:
    case("an unknown model is refused", "Toaster" in str(exc))

# ------------------------------------------------------ matching outlets ----
print("-- which outlets feed which camera --")


class StubSwitch:
    """A power box that remembers what it was asked, in order."""

    def __init__(self, channels):
        self.connected = True
        self._channels = channels
        self.values = {c["index"]: c.get("value", 1.0) for c in channels}
        self.log = []
        self.refuse = set()

    def describe_channels(self):
        return [{k: v for k, v in c.items() if k != "value"} for c in self._channels]

    @property
    def channels(self):
        return [{**c, "value": self.values[c["index"]]} for c in self._channels]

    def set_value(self, index, value):
        if index in self.refuse:
            raise DeviceError("outlet stuck")
        self.values[index] = float(value)
        self.log.append((index, float(value), time.monotonic()))


class StubCamera:
    def __init__(self):
        self.connected = True
        self.can_cool = False


class StubCapture:
    busy = False
    aborted = 0

    def abort(self):
        self.aborted += 1


class StubManager:
    def __init__(self, switch=None):
        self.devices = {"switch": switch}
        self.lines = []

    def get(self, kind):
        return self.devices.get(kind)

    def disconnect(self, kind):
        self.devices[kind] = None
        self.lines.append(("info", f"disconnect {kind}"))

    def log(self, text, level="info"):
        self.lines.append((level, text))


def make_switch():
    return StubSwitch([
        {"index": 0, "name": "Output1", "group": "power", "boolean": True, "max": 1.0, "value": 1.0},
        {"index": 1, "name": "RASA1", "group": "power", "boolean": True, "max": 1.0, "value": 1.0},
        {"index": 2, "name": "RASA2", "group": "power", "boolean": True, "max": 1.0, "value": 1.0},
        {"index": 3, "name": "rasa1", "group": "usb", "boolean": True, "max": 1.0, "value": 1.0},
        {"index": 4, "name": "rasa2", "group": "usb", "boolean": True, "max": 1.0, "value": 1.0},
        {"index": 5, "name": "eaf1", "group": "usb", "boolean": True, "max": 1.0, "value": 1.0},
        {"index": 6, "name": "Dew1", "group": "dew", "boolean": False, "max": 100.0, "value": 40.0},
    ])


config = Config(Path(tempfile.mkdtemp()) / "settings.json")
store = EquipmentStore(Path(tempfile.mkdtemp()) / "equipment.json")
store.rename_rig(store.rigs()[0]["id"], "rasa1")
store.add_rig("rasa2")
store.add_rig("rasa3")


class RigSetUnderTest(rigs_module.RigSet):
    """The real policy with stub telescopes: no devices are opened."""

    def __init__(self, config, store, switch):
        self.config, self.store = config, store
        self._rigs = []
        self._reconnects, self._cycled_at, self._cycling = {}, {}, {}
        self.switch = switch
        self.master_manager = StubManager(switch)
        self.plain_outcomes = []        # what the next plain reconnects return
        self.plain_calls = []

    @property
    def master(self):
        return self.rigs_by_name["rasa1"]

    @property
    def all(self):
        return list(self.rigs_by_name.values())

    def connect_remembered(self, rig, kinds=None):
        kinds = tuple(kinds or ("camera",))
        self.plain_calls.append(rig.name if kinds == ("camera",) else f"{rig.name}:{kinds[0]}")
        if self.plain_outcomes:
            outcome = self.plain_outcomes.pop(0)
        else:
            outcome = {"connected": list(kinds), "failed": []}
        for kind in outcome["connected"]:
            rig.manager.devices[kind] = StubCamera()
        return outcome


def make_rig(rigset, name, master):
    definition = next(r for r in store.rigs() if r["name"] == name)
    rig = rigs_module.Rig.__new__(rigs_module.Rig)
    rig.id, rig.name, rig.role = definition["id"], name, "master" if master else "slave"
    rig.store = store
    rig.config = rigs_module.RigConfig(config, store, rig.id, primary=master)
    rig.manager = rigset.master_manager if master else StubManager()
    rig.manager.devices["camera"] = StubCamera()
    rig.capture = StubCapture()
    rig.cooler_wanted = None
    # The real status() reads every service; the stub has none.
    rig.status = lambda rig=rig: {"id": rig.id, "name": rig.name}
    return rig


def build(switch=None):
    switch = switch or make_switch()
    rigset = RigSetUnderTest(config, store, switch)
    rigset.rigs_by_name = {}
    for name in ("rasa1", "rasa2", "rasa3"):
        rigset.rigs_by_name[name] = make_rig(rigset, name, master=(name == "rasa1"))
    return rigset, switch


rigset, switch = build()
r1, r2, r3 = (rigset.rigs_by_name[n] for n in ("rasa1", "rasa2", "rasa3"))
chosen, source = r1.power_channels(switch)
case("the master is matched to the outlets carrying its name, whatever the case",
     source == "byName" and sorted(c["name"] for c in chosen) == ["RASA1", "rasa1"],
     f"{source} {[c['name'] for c in chosen]}")
chosen, source = r2.power_channels(switch)
case("a follower is matched to its own outlets",
     source == "byName" and sorted(c["name"] for c in chosen) == ["RASA2", "rasa2"])
chosen, source = r3.power_channels(switch)
case("a telescope nothing is named after gets nothing", source == "none" and chosen == [])
case("eaf1 is not taken for rasa1", not any(c["name"] == "eaf1" for c in r1.power_channels(switch)[0]))

config.update("camera", {"powerChannels": ["Output1", "index:6"]})
chosen, source = r1.power_channels(switch)
case("outlets ticked on the master's page win over the name match",
     source == "configured" and [c["name"] for c in chosen] == ["Output1", "Dew1"],
     f"{source} {[c['name'] for c in chosen]}")
chosen, source = r2.power_channels(switch)
case("a follower never inherits the master's ticked outlets",
     source == "byName" and sorted(c["name"] for c in chosen) == ["RASA2", "rasa2"],
     f"{source} {[c['name'] for c in chosen]}")
store.update_rig_settings(r3.id, "camera", {"powerChannels": ["RASA2"]})
chosen, source = r3.power_channels(switch)
case("a follower's own ticked outlet is taken exactly, not its lower-case twin",
     source == "configured" and [c["name"] for c in chosen] == ["RASA2"],
     f"{source} {[c['name'] for c in chosen]}")
store.update_rig_settings(r3.id, "camera", {"powerChannels": ["RASA-2"]})
chosen, source = r3.power_channels(switch)
case("a name typed loosely matches every port spelt like it",
     source == "configured" and [c["name"] for c in chosen] == ["RASA2", "rasa2"],
     f"{source} {[c['name'] for c in chosen]}")
config.update("camera", {"powerChannels": []})
store.update_rig_settings(r3.id, "camera", {"powerChannels": []})
mapping = rigset.power_mapping(r1)
case("the mapping for the UI says what would be switched and why",
     mapping["available"] and mapping["source"] == "byName"
     and sorted(c["name"] for c in mapping["channels"]) == ["RASA1", "rasa1"]
     and mapping["configured"] == [] and mapping["lastCycled"] is None, str(mapping))
case("...and carries every slot the telescope has, the mount included on the master",
     set(mapping["devices"]) >= {"camera", "focuser", "filterwheel", "mount", "guider"}
     and "switch" not in mapping["devices"]
     and mapping["devices"]["focuser"] == {"source": "none", "channels": [], "lastCycled": None},
     str(sorted(mapping["devices"])))
status = rigset.status()
case("every telescope's status carries its power mapping",
     [s["power"]["source"] for s in status] == ["byName", "byName", "none"])

# The Power page: an outlet is given to one piece of gear by its device key.
print("-- the Power page's assignments --")
config.update("power", {"outlets": {"eaf1": f"{r1.id}:focuser", "Output1": "mount",
                                    "RASA-2": f"{r3.id}:camera", "rasa2": f"{r3.id}:camera"}})
chosen, source = r1.power_channels(switch, "focuser")
case("an outlet assigned to a focuser is that focuser's",
     source == "assigned" and [c["name"] for c in chosen] == ["eaf1"], f"{source} {chosen}")
chosen, source = r1.power_channels(switch, "mount")
case("the mount is named bare, and found on the master",
     source == "assigned" and [c["name"] for c in chosen] == ["Output1"], f"{source} {chosen}")
chosen, source = r3.power_channels(switch, "camera")
case("an assignment typed loosely finds the port, and two outlets can feed one device",
     source == "assigned" and [c["name"] for c in chosen] == ["RASA2", "rasa2"],
     f"{source} {[c['name'] for c in chosen]}")
chosen, source = r2.power_channels(switch, "camera")
case("an outlet given to another telescope's camera is no longer taken by name",
     source == "none" and chosen == [], f"{source} {[c['name'] for c in chosen]}")
chosen, source = r1.power_channels(switch, "camera")
case("the camera still matches by name where nothing was assigned",
     source == "byName" and sorted(c["name"] for c in chosen) == ["RASA1", "rasa1"])
store.update_rig_settings(r3.id, "camera", {"powerChannels": ["Dew1"]})
chosen, source = r3.power_channels(switch, "camera")
case("an assignment on the Power page beats the 0.2.46 tick list",
     source == "assigned" and [c["name"] for c in chosen] == ["RASA2", "rasa2"])
store.update_rig_settings(r3.id, "camera", {"powerChannels": []})
chosen, source = r2.power_channels(switch, "focuser")
case("a slot nothing is assigned to gets nothing, by name or otherwise",
     source == "none" and chosen == [])
case("device keys: a telescope's own gear, the mount bare",
     r2.device_key("camera") == f"{r2.id}:camera" and r1.device_key("mount") == "mount")
mapping = rigset.power_mapping(r1)
case("the mapping lists the focuser's and the mount's outlets",
     mapping["devices"]["focuser"]["source"] == "assigned"
     and [c["name"] for c in mapping["devices"]["focuser"]["channels"]] == ["eaf1"]
     and [c["name"] for c in mapping["devices"]["mount"]["channels"]] == ["Output1"])
config.update("power", {"outlets": {}})
no_box = build(StubSwitch([]))[0]
no_box.switch.connected = False
case("no box connected: nothing matched, nothing available",
     no_box.power_mapping(no_box.rigs_by_name["rasa1"]) ["available"] is False)

# ------------------------------------------------------- the power cycle ----
print("-- the power cycle --")
config.update("recovery", {"powerCycleOffSeconds": 1.0, "powerCycleBootSeconds": 0.5,
                           "powerCycleAfterReconnects": 1, "powerCycleCameras": True})

# 1. First failure of the night: a plain reconnect, no power touched.
rigset, switch = build()
r1 = rigset.rigs_by_name["rasa1"]
outcome = rigset.reconnect_camera(r1)
case("the first failure gets a plain reconnect and the power is left alone",
     outcome["connected"] == ["camera"] and switch.log == [] and rigset.plain_calls == ["rasa1"],
     str(switch.log))

# 2. The same camera again inside the window: straight to the mains.
said = []
started = time.monotonic()
outcome = rigset.reconnect_camera(r1, progress=said.append)
elapsed = time.monotonic() - started
off = [(i, v) for i, v, t in switch.log if v == 0.0]
on = [(i, v) for i, v, t in switch.log if v == 1.0]
case("the second rescue in a quarter of an hour power-cycles the camera's outlets",
     sorted(i for i, v in off) == [1, 3] and sorted(i for i, v in on) == [1, 3],
     str(switch.log))
case("...off for the configured time, then the boot wait",
     1.4 <= elapsed <= 3.0 and switch.log[-1][2] - switch.log[0][2] >= 1.0, f"{elapsed:.2f}s")
case("...the camera is closed before the power goes and reconnected after",
     ("info", "disconnect camera") in r1.manager.lines and outcome["connected"] == ["camera"]
     and rigset.plain_calls == ["rasa1", "rasa1"])
case("...the other outlets are untouched",
     all(i in (1, 3) for i, v, t in switch.log) and switch.values[6] == 40.0)
case("...progress names what is being waited for",
     any("off" in s for s in said) and any("boot" in s for s in said), str(said))
case("...and the log says why",
     any("Power-cycling the camera because" in text for level, text in r1.manager.lines))
case("the cycle is remembered for the UI",
     rigset.power_mapping(r1)["lastCycled"] is not None)

# 3. Straight after a cycle, another failure is not cycled again (too soon).
del switch.log[:]
outcome = rigset.reconnect_camera(r1)
case("a third failure right after a cycle gets a plain reconnect, not another cycle",
     switch.log == [] and outcome["connected"] == ["camera"])

# 4. A plain reconnect that fails outright goes to the mains at once.
rigset, switch = build()
r2 = rigset.rigs_by_name["rasa2"]
rigset.plain_outcomes = [{"connected": [], "failed": ["camera: ASIOpenCamera failed (invalid id)"]}]
outcome = rigset.reconnect_camera(r2)
case("a reconnect that fails is followed by a power cycle and a second reconnect",
     rigset.plain_calls == ["rasa2", "rasa2"] and outcome["connected"] == ["camera"]
     and sorted(i for i, v, t in switch.log if v == 0.0) == [2, 4], str(switch.log))
case("...and nobody else's outlets", all(i in (2, 4) for i, v, t in switch.log))

# 5. Switched off in the settings: never touched, however often it fails.
config.update("recovery", {"powerCycleCameras": False})
rigset, switch = build()
r1 = rigset.rigs_by_name["rasa1"]
rigset.plain_outcomes = [{"connected": [], "failed": ["camera: gone"]}]
outcome = rigset.reconnect_camera(r1)
rigset.reconnect_camera(r1)
case("with power cycling off the outlets are never touched",
     switch.log == [] and outcome["failed"] == ["camera: gone"] and rigset.plain_calls == ["rasa1"] * 2)
config.update("recovery", {"powerCycleCameras": True})

# 6. Nothing mapped: a plain reconnect only, and the hand version says why.
rigset, switch = build()
r3 = rigset.rigs_by_name["rasa3"]
rigset.plain_outcomes = [{"connected": [], "failed": ["camera: gone"]}]
outcome = rigset.reconnect_camera(r3)
case("a camera with no outlets mapped is only ever reconnected",
     switch.log == [] and outcome["failed"] == ["camera: gone"])
try:
    rigset.power_cycle_camera(r3)
    case("a hand power cycle with nothing mapped is refused", False)
except DeviceError as exc:
    case("a hand power cycle with nothing mapped is refused", "no outlet" in str(exc), str(exc))

# 7. Stopped during the off wait: the power is put back at once.
rigset, switch = build()
r1 = rigset.rigs_by_name["rasa1"]
rigset._reconnects[r1.id] = [time.time()]
config.update("recovery", {"powerCycleOffSeconds": 30.0, "powerCycleBootSeconds": 30.0})
stop_at = time.monotonic() + 0.6
started = time.monotonic()
rigset.reconnect_camera(r1, stop=lambda: time.monotonic() > stop_at)
elapsed = time.monotonic() - started
case("a Stop during the off wait puts the power back at once",
     elapsed < 3.0 and switch.values[1] == 1.0 and switch.values[3] == 1.0, f"{elapsed:.2f}s")
case("...and says the boot wait was skipped",
     any("boot wait was skipped" in text for level, text in r1.manager.lines))
config.update("recovery", {"powerCycleOffSeconds": 1.0, "powerCycleBootSeconds": 0.5})

# 8. An outlet that refuses to come back is shouted about, after the rest are on.
rigset, switch = build()
r1 = rigset.rigs_by_name["rasa1"]
rigset._reconnects[r1.id] = [time.time()]


def set_value(index, value):
    # The 12 V port goes off fine and then refuses to come back on.
    if index == 1 and value == 1.0:
        raise DeviceError("outlet stuck")
    switch.values[index] = float(value)
    switch.log.append((index, float(value), time.monotonic()))


switch.set_value = set_value
try:
    rigset.reconnect_camera(r1)
    case("an outlet that will not come back on raises", False)
except DeviceError as exc:
    case("an outlet that will not come back on raises", "could not restore power" in str(exc)
         and switch.values[3] == 1.0, str(exc))
case("...and the failure is in the log as an error",
     any(level == "error" and "restore power" in text for level, text in r1.manager.lines))

# 9. The hand version: a cycle plus a reconnect, remembered like the others.
rigset, switch = build()
r2 = rigset.rigs_by_name["rasa2"]
outcome = rigset.power_cycle_camera(r2, "asked for by hand")
case("a hand power cycle switches the outlets and reconnects",
     outcome["connected"] == ["camera"] and sorted(i for i, v, t in switch.log if v == 0.0) == [2, 4])
# A heater ticked as one of a camera's outlets goes back to its own level.
rigset, switch = build()
r1 = rigset.rigs_by_name["rasa1"]
config.update("camera", {"powerChannels": ["rasa1", "Dew1"]})
rigset.power_cycle_camera(r1, "asked for by hand")
config.update("camera", {"powerChannels": []})
case("a dew heater that was cycled goes back to its level, not to full",
     switch.values[6] == 40.0 and (6, 0.0) in [(i, v) for i, v, t in switch.log], str(switch.log))

# 10. Two cycles of one camera cannot overlap.
rigset, switch = build()
r1 = rigset.rigs_by_name["rasa1"]
rigset._cycling[f"{r1.id}:camera"] = __import__("threading").Lock()
rigset._cycling[f"{r1.id}:camera"].acquire()
try:
    rigset.power_cycle_camera(r1)
    case("a second cycle of a camera already being cycled is refused", False)
except DeviceError as exc:
    case("a second cycle of a camera already being cycled is refused",
         "already being power-cycled" in str(exc))
finally:
    rigset._cycling[f"{r1.id}:camera"].release()

# 11. Any other device: its own outlets, closed first, reconnected after.
print("-- power-cycling other gear --")
rigset, switch = build()
r1 = rigset.rigs_by_name["rasa1"]
config.update("power", {"outlets": {"eaf1": f"{r1.id}:focuser", "Output1": "mount"}})
r1.manager.devices["focuser"] = StubCamera()
outcome = rigset.power_cycle_device(r1, "focuser", "asked for by hand")
case("a focuser is cycled on its own outlet and reconnected",
     [(i, v) for i, v, t in switch.log] == [(5, 0.0), (5, 1.0)]
     and outcome["connected"] == ["focuser"] and rigset.plain_calls == ["rasa1:focuser"],
     f"{switch.log} {outcome} {rigset.plain_calls}")
case("...the focuser was closed before the power went",
     ("info", "disconnect focuser") in r1.manager.lines)
case("...and the log names the device",
     any("Power-cycling the focuser because" in text for level, text in r1.manager.lines))
case("...the camera's outlets were not touched",
     switch.values[1] == 1.0 and switch.values[3] == 1.0 and not any(i in (1, 3) for i, v, t in switch.log))
case("the cycle is remembered against the focuser, not the camera",
     rigset.power_mapping(r1)["devices"]["focuser"]["lastCycled"] is not None
     and rigset.power_mapping(r1)["lastCycled"] is None)
del switch.log[:]
outcome = rigset.power_cycle_device(r1, "mount")
case("the mount is cycled on the master",
     [(i, v) for i, v, t in switch.log] == [(0, 0.0), (0, 1.0)] and outcome["connected"] == ["mount"])
r2 = rigset.rigs_by_name["rasa2"]
try:
    rigset.power_cycle_device(r2, "mount")
    case("a follower has no mount to cycle", False)
except DeviceError as exc:
    case("a follower has no mount to cycle", "no mount" in str(exc), str(exc))
try:
    rigset.power_cycle_device(r2, "focuser")
    case("a device with nothing assigned is refused", False)
except DeviceError as exc:
    case("a device with nothing assigned is refused",
         "no outlet" in str(exc) and "Equipment > Power" in str(exc), str(exc))
try:
    rigset.power_cycle_device(r1, "switch")
    case("the box cannot cycle itself", False)
except DeviceError as exc:
    case("the box cannot cycle itself", "no power / switches slot" in str(exc), str(exc))
config.update("power", {"outlets": {}})

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
