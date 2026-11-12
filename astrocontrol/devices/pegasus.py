"""Pegasus Astro power boxes through the Unity Platform's local web service.

Pegasus Unity (the program that drives every Pegasus box) runs a small HTTP
server on port 32000 of the PC it is on.  Every box it knows about is listed
there, and every port on a box can be read and switched through it — the same
calls Unity's own window makes.  Going that way rather than through Pegasus's
ASCOM Switch driver means no COM apartment, no driver setup window to pick the
box in, and the names typed into Unity's own port labels ("rasa1", "RASA1")
come straight through, which is what makes an outlet recognisable at 2 a.m.

The box this was written against is an Ultimate Powerbox v3; the v2, the
Pocket Powerbox Advance and Micro and the Saddle Powerbox use the same service
with slightly different routes, and are handled as far as their routes are
known.  Nothing here is executed on the box: it is plain HTTP to Unity.

Driver ids look like ``UPBv3/UPBv300091926`` — the model Unity calls the box
and its serial — with an optional ``@host:port`` for a Unity on another PC.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import DeviceError, SwitchBank

DEFAULT_HOST = "127.0.0.1:32000"

#: Models Unity reports in `name` for a connected device, with the route group
#: each kind of port uses and how many of them the box has.  "pwm" ports are
#: the 12 V outputs on a v3 (its route calls them PWM), "power" the 12 V
#: outputs on a v2 and "hub" the one switchable quad output on a Pocket box.
MODELS: dict[str, dict[str, Any]] = {
    "UPBv3": {"label": "Ultimate Powerbox v3", "power": "PWM", "usb": "USB",
              "dew": "Dew", "relay": True, "adjustable": ("Buck", "Boost")},
    "UPBv2": {"label": "Ultimate Powerbox v2", "power": "Power", "usb": "USB",
              "dew": "Dew"},
    "PPBAdvance": {"label": "Pocket Powerbox Advance", "hub": "Power/Hub",
                   "dualusb": "DualUSB2", "dew": "Dew"},
    "PPBMicro": {"label": "Pocket Powerbox Micro", "hub": "Power/Hub", "dew": "Dew"},
    "SaddlePowerBox": {"label": "Saddle Powerbox", "power": "Power", "usb": "USB",
                       "dew": "Dew"},
}

#: How long one reading of the box is trusted.  The UI asks for status several
#: times a second; the box does not change that fast and Unity would rather
#: not be asked that often either.
REPORT_TTL = 2.0


def available() -> bool:
    """Always importable: there is nothing to load, only a service to reach."""
    return True


def _split_driver_id(driver_id: str) -> tuple[str, str, str]:
    """``UPBv3/UPBv300091926@host:port`` -> (model, serial, host:port)."""
    text = str(driver_id or "").strip()
    host = DEFAULT_HOST
    if "@" in text:
        text, host = text.rsplit("@", 1)
        host = host.strip() or DEFAULT_HOST
    model, _, serial = text.partition("/")
    return model.strip(), serial.strip(), host


def _base_url(host: str) -> str:
    host = host.strip()
    if not host.startswith("http"):
        host = "http://" + host
    return host.rstrip("/")


def request(method: str, path: str, host: str = DEFAULT_HOST,
            query: dict[str, str] | None = None, timeout: float = 5.0) -> Any:
    """One call to Unity, decoded.  Raises DeviceError with a readable reason."""
    url = _base_url(host) + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("message", "")
        except Exception:                         # noqa: BLE001 - the status is enough
            pass
        raise DeviceError(f"Unity answered {exc.code} to {method} {path}"
                          + (f": {detail}" if detail else "")) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise DeviceError(f"Pegasus Unity is not answering at {_base_url(host)} "
                          f"({getattr(exc, 'reason', exc)}) — is Unity running?") from exc
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise DeviceError(f"Unity sent something that is not JSON for {path}") from exc
    if isinstance(payload, dict) and str(payload.get("status", "success")).lower() != "success":
        raise DeviceError(f"Unity refused {method} {path}: "
                          f"{payload.get('message') or payload.get('status')}")
    return payload


def connected_devices(host: str = DEFAULT_HOST, timeout: float = 2.0) -> list[dict[str, Any]]:
    """What Unity has plugged in right now: name (model), fullName, deviceID, uniqueKey."""
    payload = request("GET", "/Server/DeviceManager/Connected", host, timeout=timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    return [entry for entry in (data or []) if isinstance(entry, dict)]


def list_devices(kind: str, host: str = DEFAULT_HOST) -> list[dict[str, str]]:
    """The power boxes Unity can see, as driver entries for the Equipment page.

    Quiet when Unity is not running: a backend that is not there is not an
    error, it is an empty list — the same as ASCOM with no drivers installed.
    """
    if kind != "switch":
        return []
    try:
        devices = connected_devices(host, timeout=1.5)
    except DeviceError:
        return []
    found: list[dict[str, str]] = []
    for entry in devices:
        model = str(entry.get("name") or "")
        if model not in MODELS:
            continue
        serial = str(entry.get("deviceID") or entry.get("uniqueKey") or "")
        label = str(entry.get("fullName") or MODELS[model]["label"])
        suffix = "" if host == DEFAULT_HOST else f"@{host}"
        found.append({
            "id": f"{model}/{serial}{suffix}",
            "name": f"Pegasus {label} ({serial}) via Unity",
            "device": label,
        })
    return found


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _state_on(value: Any) -> bool | None:
    """Unity says a port's state several ways: {"state": "ON"}, "ON", a level."""
    if isinstance(value, dict):
        value = value.get("state", value.get("level"))
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return str(value).strip().upper() in ("ON", "TRUE", "1")


class PegasusUnitySwitch(SwitchBank):
    """One Pegasus power box, seen as a bank of named channels.

    Channels are numbered in a fixed order per model — the 12 V outputs, then
    the USB ports, then the dew heaters, then the relay and the adjustable
    outputs — so an index remembered for an outlet means the same port next
    week.  Names are the ones set in Unity.
    """

    def __init__(self, driver_id: str, name: str) -> None:
        SwitchBank.__init__(self, driver_id, name)
        self.model, self.serial, self.host = _split_driver_id(driver_id)
        if self.model not in MODELS:
            raise DeviceError(f"{driver_id!r} is not a Pegasus box Unity knows how to drive "
                              f"(one of {', '.join(MODELS)})")
        self.spec = MODELS[self.model]
        self.unique_key = ""
        self._channels: list[dict[str, Any]] = []
        self._report: dict[str, Any] = {}
        self._report_at = 0.0
        self._http = threading.RLock()

    # -- talking to Unity --------------------------------------------------
    def _call(self, method: str, path: str, timeout: float = 5.0) -> Any:
        with self._http:
            return request(method, f"/Driver/{self.model}{path}", self.host,
                           {"DriverUniqueKey": self.unique_key}, timeout)

    def connect(self) -> None:
        devices = connected_devices(self.host)
        mine = [d for d in devices if str(d.get("name")) == self.model]
        match = next((d for d in mine if str(d.get("deviceID")) == self.serial
                      or str(d.get("uniqueKey")) == self.serial), None)
        if match is None and len(mine) == 1 and not self.serial:
            match = mine[0]
        if match is None:
            seen = ", ".join(f"{d.get('name')} {d.get('deviceID')}" for d in devices) or "nothing"
            raise DeviceError(f"Unity does not list a {self.model} with serial "
                              f"{self.serial or '?'} (it lists: {seen}). Is the box "
                              "plugged in and shown as connected in Unity?")
        self.unique_key = str(match.get("uniqueKey") or "")
        label = str(match.get("fullName") or self.spec["label"])
        # Unity starts a box's driver itself as a rule; asking again is
        # harmless and covers a box that was plugged in after Unity came up.
        try:
            self._call("OPTIONS", "/Start")
        except DeviceError:
            pass
        self._report = {}
        self._report_at = 0.0
        self._connected = True
        try:
            self._channels = self._describe(self._fetch_report(force=True))
        except DeviceError:
            self._connected = False
            raise
        self.name = f"Pegasus {label} ({self.serial}) via Unity"

    def disconnect(self) -> None:
        # The box keeps running: disconnecting here only stops asking it.
        self._connected = False
        self._channels = []

    # -- reading the box ---------------------------------------------------
    def _fetch_report(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._report and now - self._report_at < REPORT_TTL:
            return self._report
        payload = self._call("GET", "/Report")
        data = payload.get("data") if isinstance(payload, dict) else None
        message = (data or {}).get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict):
            raise DeviceError("Unity's report for the box had no readings in it")
        self._report = message
        self._report_at = now
        return message

    def _forget_report(self) -> None:
        self._report_at = 0.0

    @staticmethod
    def _ports(report: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
        for key in keys:
            block = report.get(key)
            if isinstance(block, dict) and isinstance(block.get("hub"), list):
                return [p for p in block["hub"] if isinstance(p, dict)]
            if isinstance(block, list):
                return [p for p in block if isinstance(p, dict)]
        return []

    def _describe(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        """The channel list, from the shape of the box's report."""
        found: list[dict[str, Any]] = []

        def add(group: str, port: int | None, name: str, description: str,
                boolean: bool = True, maximum: float = 1.0) -> None:
            found.append({
                "index": len(found), "group": group, "port": port,
                "name": name, "description": description,
                "min": 0.0, "max": maximum, "step": 1.0,
                "boolean": boolean, "writable": True,
            })

        if self.spec.get("power"):
            for entry in self._ports(report, "pwmHubStatus", "powerHubStatus", "powerPorts"):
                number = int(entry.get("portNumber") or len(found) + 1)
                add("power", number, str(entry.get("name") or f"12V {number}"),
                    f"12 V output {number}")
        if self.spec.get("hub"):
            add("hub", None, "12V outputs", "the quad 12 V output, switched together")
        if self.spec.get("usb"):
            for entry in self._ports(report, "usbHubStatus", "usbPorts"):
                number = int(entry.get("portNumber") or len(found) + 1)
                add("usb", number, str(entry.get("name") or f"USB {number}"),
                    f"USB port {number}")
        if self.spec.get("dualusb"):
            add("dualusb", None, "USB2 pair", "the two switched USB 2 ports")
        if self.spec.get("dew"):
            for entry in self._ports(report, "dewHubStatus", "dewPorts"):
                number = int(entry.get("portNumber") or len(found) + 1)
                add("dew", number, str(entry.get("name") or f"Dew {number}"),
                    f"dew heater {number} (0–100 %)", boolean=False, maximum=100.0)
        if self.spec.get("relay") and isinstance(report.get("relay"), dict):
            add("relay", None, "Relay", "the relay output")
        for which in self.spec.get("adjustable", ()):
            block = report.get(which.lower())
            if isinstance(block, dict):
                volts = block.get("voltage")
                add(which.lower(), None, f"{which} output",
                    f"adjustable {which.lower()} output"
                    + (f" at {volts} V" if volts is not None else ""))
        return found

    def _value_of(self, channel: dict[str, Any], report: dict[str, Any]) -> float | None:
        group, port = channel["group"], channel["port"]
        if group == "power":
            for entry in self._ports(report, "pwmHubStatus", "powerHubStatus", "powerPorts"):
                if int(entry.get("portNumber") or 0) == port:
                    on = _state_on(entry.get("state", entry.get("level")))
                    return None if on is None else float(on)
        elif group == "usb":
            for entry in self._ports(report, "usbHubStatus", "usbPorts"):
                if int(entry.get("portNumber") or 0) == port:
                    on = _state_on(entry.get("switch", entry.get("state")))
                    return None if on is None else float(on)
        elif group == "dew":
            for entry in self._ports(report, "dewHubStatus", "dewPorts"):
                if int(entry.get("portNumber") or 0) == port:
                    level = entry.get("power", entry.get("level"))
                    return None if level is None else float(level)
        elif group == "hub":
            on = _state_on(report.get("powerHubStatus", report.get("quadPort")))
            return None if on is None else float(on)
        elif group == "dualusb":
            on = _state_on(report.get("dualUSB2", report.get("usb2Status")))
            return None if on is None else float(on)
        elif group in ("relay", "buck", "boost"):
            block = report.get(group)
            on = _state_on(block.get("state") if isinstance(block, dict) else block)
            return None if on is None else float(on)
        return None

    @property
    def channels(self) -> list[dict[str, Any]]:
        if not self._connected:
            return []
        try:
            report = self._fetch_report()
        except DeviceError:
            report = {}
        out = []
        for channel in self._channels:
            entry = dict(channel)
            entry["value"] = self._value_of(channel, report) if report else None
            out.append(entry)
        return out

    def describe_channels(self) -> list[dict[str, Any]]:
        """The channel list without values: no call to the box."""
        return [dict(channel) for channel in self._channels]

    def get_value(self, index: int) -> float:
        self._require()
        channel = self._channel(index)
        value = self._value_of(channel, self._fetch_report())
        if value is None:
            raise DeviceError(f"the box did not report {channel['name']}")
        return value

    def _channel(self, index: int) -> dict[str, Any]:
        channel = next((c for c in self._channels if c["index"] == int(index)), None)
        if channel is None:
            raise DeviceError(f"this box has no channel {index}")
        return channel

    # -- switching ---------------------------------------------------------
    def set_value(self, index: int, value: float) -> None:
        self._require()
        channel = self._channel(index)
        group, port = channel["group"], channel["port"]
        on = float(value) > 0
        if group == "power":
            self._call("PUT", f"/{self.spec['power']}/{port}/{'On' if on else 'Off'}")
        elif group == "usb":
            self._call("PUT", f"/{self.spec['usb']}/{port}/{'On' if on else 'Off'}")
        elif group == "dew":
            level = max(0, min(100, int(round(float(value)))))
            if level <= 0:
                self._call("PUT", f"/{self.spec['dew']}/{port}/Off")
            else:
                self._call("PUT", f"/{self.spec['dew']}/{port}/{level}")
        elif group == "hub":
            self._call("PUT", f"/{self.spec['hub']}/{'On' if on else 'Off'}")
        elif group == "dualusb":
            self._call("PUT", f"/{self.spec['dualusb']}/{'On' if on else 'Off'}")
        elif group == "relay":
            self._call("PUT", f"/Relay/{'On' if on else 'Off'}")
        elif group in ("buck", "boost"):
            self._call("PUT", f"/Adjustable/{group.capitalize()}/{'On' if on else 'Off'}")
        else:
            raise DeviceError(f"{channel['name']} cannot be switched from here")
        self._forget_report()

    # -- reporting ---------------------------------------------------------
    def readings(self) -> dict[str, Any]:
        """The box's own meters: input volts and amps, and its weather."""
        if not self._connected:
            return {}
        try:
            report = self._fetch_report()
        except DeviceError:
            return {}
        out: dict[str, Any] = {}
        for key in ("voltage", "current", "power", "temperature", "humidity", "dewPoint"):
            value = report.get(key)
            if isinstance(value, (int, float)):
                out[key] = value
        return out

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        return {"connected": True, "model": self.spec["label"], "serial": self.serial,
                "channels": self.channels, "readings": self.readings()}


FACTORIES = {"switch": PegasusUnitySwitch}


def create(kind: str, driver_id: str, name: str):
    factory = FACTORIES.get(kind)
    if factory is None:
        raise DeviceError(f"the Pegasus Unity backend only provides a power box, not a {kind}")
    return factory(driver_id, name or driver_id)
