"""ASCOM Alpaca (REST over HTTP) backend.

Works with any Alpaca-conformant device: ASCOM Remote, the Alpaca simulators,
ZWO/QHY Alpaca servers, INDIGO's Alpaca bridge, and hardware with it built in.
Unlike the COM backend this needs no Windows platform install and can talk to a
device on another machine, which is how most remote observatories are wired.
"""

from __future__ import annotations

import json
import random
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import numpy as np

from .base import (Camera, DeviceError, Dome, FilterWheel, FlatPanel, Focuser,
                   Mount, Rotator, SafetyMonitor, SwitchBank)

CLIENT_ID = random.randint(1, 65535)
DISCOVERY_PORT = 32227
DISCOVERY_MESSAGE = b"alpacadiscovery1"

_DEVICE_TYPES = {
    "camera": "camera",
    "mount": "telescope",
    "filterwheel": "filterwheel",
    "focuser": "focuser",
    "rotator": "rotator",
    "flatpanel": "covercalibrator",
    "piercam": "camera",
    "safetymonitor": "safetymonitor",
    "dome": "dome",
    "switch": "switch",
}

# ImageBytes element type codes from the Alpaca specification.
_ELEMENT_DTYPES = {1: "<i2", 2: "<i4", 3: "<f8", 4: "<f4", 6: "<u1", 7: "<i8", 8: "<u2"}


def available() -> bool:
    """Alpaca needs nothing installed locally, so it is always offerable."""
    return True


def discover(timeout: float = 1.5) -> list[dict[str, Any]]:
    """Broadcast an Alpaca discovery packet and ask every responder what it has."""
    servers: set[tuple[str, int]] = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.3)
    try:
        for address in ("255.255.255.255", "127.0.0.1"):
            try:
                sock.sendto(DISCOVERY_MESSAGE, (address, DISCOVERY_PORT))
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                payload, (host, _) = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                port = int(json.loads(payload.decode("utf-8")).get("AlpacaPort"))
            except Exception:
                continue
            servers.add((host, port))
    finally:
        sock.close()

    found: list[dict[str, Any]] = []
    for host, port in sorted(servers):
        found.extend(configured_devices(host, port))
    return found


# -- addressing and transport (0.2.53a) ----------------------------------------
#
# Until 0.2.53 every Alpaca address was ``host:port/type/number`` and always
# plain http.  A server on the internet - the Starfront roof API at
# https://alpaca-api.tx.starfront.space - is https only (http answers 500), and
# may sit under a path prefix.  So an address may now also be a URL:
#
#     https://host[:port][/base/path]/type/number
#
# and an address without a scheme means exactly what it always did: http.

DEFAULT_PORTS = {"http": 11111, "https": 443}


def tls_context() -> ssl.SSLContext:
    """A verifying TLS context: the system's roots plus a bundled CA file.

    Python on Windows reads the Windows certificate store, but only the roots
    already in it (it never fetches one on demand the way the OS does), so a
    frozen build also carries a CA bundle: certifi's when it is importable,
    otherwise the copy shipped next to this module (cacert.pem). Verification
    is never turned off here.
    """
    from pathlib import Path                      # noqa: PLC0415
    context = ssl.create_default_context()        # CERT_REQUIRED + hostname check
    bundle = None
    try:
        import certifi                            # noqa: PLC0415 - optional
        bundle = certifi.where()
    except Exception:                             # noqa: BLE001 - fall back to ours
        bundle = None
    for path in (bundle, str(Path(__file__).with_name("cacert.pem"))):
        if path and Path(path).is_file():
            try:
                context.load_verify_locations(cafile=path)
                break
            except Exception:                     # noqa: BLE001 - try the next
                continue
    return context


_TLS: ssl.SSLContext | None = None
_TLS_LOCK = threading.Lock()


def _shared_tls() -> ssl.SSLContext:
    global _TLS
    with _TLS_LOCK:
        if _TLS is None:
            _TLS = tls_context()
        return _TLS


def urlopen(request: Any, timeout: float, context: ssl.SSLContext | None = None):
    """urllib's urlopen, with a verifying TLS context for https URLs."""
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    if url.lower().startswith("https:"):
        return urllib.request.urlopen(request, timeout=timeout,
                                      context=context or _shared_tls())
    return urllib.request.urlopen(request, timeout=timeout)


def server_root(scheme: str, host: str, port: int | None = None,
                base_path: str = "") -> str:
    """('https', 'example.org', 443, '/alpaca') -> 'https://example.org:443/alpaca'."""
    scheme = (scheme or "http").strip().lower()
    if scheme not in DEFAULT_PORTS:
        raise DeviceError(f"Alpaca scheme must be http or https, not {scheme!r}")
    host = (host or "").strip()
    if not host or "/" in host or " " in host or ":" in host:
        raise DeviceError(f"malformed Alpaca host {host!r}")
    port = int(port or DEFAULT_PORTS[scheme])
    if not 0 < port < 65536:
        raise DeviceError(f"Alpaca port {port} is out of range")
    base = "/".join(part for part in str(base_path or "").split("/") if part)
    return f"{scheme}://{host}:{port}" + (f"/{base}" if base else "")


def parse_address(driver_id: str) -> tuple[str, str, int]:
    """An Alpaca device address -> (server root, alpaca type, device number).

    ``host:port/type/number`` (plain http, as before 0.2.53a) or
    ``http[s]://host[:port][/base]/type/number``.
    """
    text = str(driver_id or "").strip()
    try:
        if "://" in text:
            parsed = urllib.parse.urlsplit(text)
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) < 2:
                raise ValueError("no type/number")
            alpaca_type, number = parts[-2].lower(), int(parts[-1])
            root = server_root(parsed.scheme, parsed.hostname or "", parsed.port,
                               "/".join(parts[:-2]))
            return root, alpaca_type, number
        location, alpaca_type, number = text.rsplit("/", 2)
        host, port = location.split(":")
        return server_root("http", host, int(port)), alpaca_type, int(number)
    except DeviceError:
        raise
    except Exception as exc:
        raise DeviceError(
            f"malformed Alpaca address {driver_id!r}; expected host:port/type/number "
            "or https://host[:port][/path]/type/number") from exc


def configured_devices(host: str, port: int | None, timeout: float = 4.0,
                       scheme: str | None = None, base_path: str = "") -> list[dict[str, Any]]:
    """Ask one Alpaca server for its device list.

    `host` may also be a URL (``https://alpaca-api.tx.starfront.space``), in
    which case its scheme, port and path are used.
    """
    if "://" in str(host or ""):
        parsed = urllib.parse.urlsplit(str(host).strip())
        scheme = parsed.scheme
        base_path = parsed.path
        port = parsed.port or (None if port in (None, 11111) else port)
        host = parsed.hostname or ""
    root = server_root(scheme or "http", host, port, base_path)
    url = f"{root}/management/v1/configureddevices"
    try:
        with urlopen(url, timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise DeviceError(f"no Alpaca server at {root} ({exc})") from exc

    # Plain http with no path keeps the old host:port/... id, so the choices
    # remembered before 0.2.53a still match.
    plain = (scheme or "http").lower() == "http" and not root.split("://", 1)[1].count("/")
    location = f"{host}:{int(port or DEFAULT_PORTS['http'])}" if plain else root
    devices = []
    for entry in body.get("Value", []):
        alpaca_type = str(entry.get("DeviceType", "")).lower()
        kind = next((k for k, v in _DEVICE_TYPES.items() if v == alpaca_type), None)
        if kind is None:
            continue
        number = int(entry.get("DeviceNumber", 0))
        devices.append({
            "kind": kind,
            "id": f"{location}/{alpaca_type}/{number}",
            "name": f"{entry.get('DeviceName', alpaca_type)} ({location})",
        })
    return devices


class _Client:
    """HTTP plumbing for one Alpaca device, with a short read cache."""

    def __init__(self, driver_id: str, kind: str) -> None:
        root, alpaca_type, number = parse_address(driver_id)
        self.base = f"{root}/api/v1/{alpaca_type}/{number}"
        self.kind = kind
        self._transaction = 0
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}

    def _next_transaction(self) -> int:
        with self._lock:
            self._transaction += 1
            return self._transaction

    def _unwrap(self, body: dict[str, Any]) -> Any:
        error = int(body.get("ErrorNumber", 0) or 0)
        if error:
            raise DeviceError(body.get("ErrorMessage") or f"Alpaca error {error}")
        return body.get("Value")

    def get(self, name: str, timeout: float = 15.0, **params: Any) -> Any:
        query = {"ClientID": CLIENT_ID, "ClientTransactionID": self._next_transaction()}
        query.update(params)
        url = f"{self.base}/{name.lower()}?{urllib.parse.urlencode(query)}"
        try:
            with urlopen(url, timeout) as response:
                return self._unwrap(json.loads(response.read().decode("utf-8")))
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceError(f"{name}: {exc}") from exc

    def get_cached(self, name: str, ttl: float = 0.4, default: Any = None) -> Any:
        """Used by status polling so a fast UI refresh does not flood the device."""
        now = time.monotonic()
        hit = self._cache.get(name)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        try:
            value = self.get(name)
        except DeviceError:
            return default
        self._cache[name] = (now, value)
        return value

    def put(self, name: str, timeout: float = 30.0, **params: Any) -> Any:
        payload = {"ClientID": CLIENT_ID, "ClientTransactionID": self._next_transaction()}
        payload.update(params)
        data = urllib.parse.urlencode(payload).encode("ascii")
        request = urllib.request.Request(
            f"{self.base}/{name.lower()}", data=data, method="PUT",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen(request, timeout) as response:
                result = self._unwrap(json.loads(response.read().decode("utf-8")))
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceError(f"{name}: {exc}") from exc
        self._cache.pop(name, None)
        return result

    # -- image download ----------------------------------------------------
    def image_array(self, timeout: float = 180.0) -> np.ndarray:
        """Fetch ImageArray, preferring the binary ImageBytes transfer."""
        query = {"ClientID": CLIENT_ID, "ClientTransactionID": self._next_transaction()}
        url = f"{self.base}/imagearray?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url, headers={"Accept": "application/imagebytes, application/json"})
        with urlopen(request, timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            payload = response.read()

        if "imagebytes" in content_type:
            return self._decode_image_bytes(payload)
        return self._decode_image_json(payload)

    @staticmethod
    def _decode_image_bytes(payload: bytes) -> np.ndarray:
        if len(payload) < 44:
            raise DeviceError("truncated ImageBytes response")
        (_version, error, _client_tx, _server_tx, data_start, element_type,
         transmission_type, rank, dim1, dim2, _dim3) = struct.unpack_from("<11i", payload, 0)
        if error:
            message = payload[data_start:].decode("utf-8", "replace")
            raise DeviceError(message or f"Alpaca error {error}")
        if rank != 2:
            raise DeviceError(f"expected a 2-D image, got rank {rank}")
        dtype = _ELEMENT_DTYPES.get(transmission_type or element_type)
        if dtype is None:
            raise DeviceError(f"unsupported ImageBytes element type {transmission_type}")

        flat = np.frombuffer(payload, dtype=dtype, count=dim1 * dim2, offset=data_start)
        # Alpaca transmits in [x][y] order, matching ASCOM's ImageArray.
        image = flat.reshape(dim1, dim2).T
        return np.clip(image.astype(np.int64), 0, 65535).astype(np.uint16)

    def _decode_image_json(self, payload: bytes) -> np.ndarray:
        body = json.loads(payload.decode("utf-8"))
        value = self._unwrap(body)
        image = np.array(value, dtype=np.int64).T
        if image.ndim != 2:
            raise DeviceError(f"expected a 2-D image, got shape {image.shape}")
        return np.clip(image, 0, 65535).astype(np.uint16)


class _AlpacaDevice:
    def _setup(self, driver_id: str, kind: str) -> None:
        self._client = _Client(driver_id, kind)

    def connect(self) -> None:
        self._client.put("connected", Connected=True)
        self._connected = True

    def disconnect(self) -> None:
        try:
            self._client.put("connected", Connected=False)
        except DeviceError:
            pass
        finally:
            self._connected = False


class AlpacaCamera(_AlpacaDevice, Camera):
    def __init__(self, driver_id: str, name: str) -> None:
        Camera.__init__(self, driver_id, name)
        self._setup(driver_id, "camera")

    def connect(self) -> None:
        super().connect()
        client = self._client
        self.sensor_width = int(client.get("cameraxsize") or 0)
        self.sensor_height = int(client.get("cameraysize") or 0)
        self.pixel_size_um = float(client.get("pixelsizex") or 0.0)
        self.max_bin = int(client.get("maxbinx") or 1)
        self.can_cool = bool(client.get("cansetccdtemperature"))
        self.can_abort = bool(client.get("canabortexposure"))
        for attribute, name, default in (("gain_min", "gainmin", 0), ("gain_max", "gainmax", 0),
                                         ("offset_min", "offsetmin", 0),
                                         ("offset_max", "offsetmax", 0),
                                         ("gain", "gain", 0), ("offset", "offset", 0),
                                         ("binning", "binx", 1)):
            try:
                setattr(self, attribute, int(client.get(name) or default))
            except DeviceError:
                setattr(self, attribute, default)

    def read_settings(self) -> dict[str, int | None]:
        """Gain and offset read from the device now (0.2.53); reads only."""
        self._require()
        found: dict[str, int | None] = {"gain": None, "offset": None}
        for key in ("gain", "offset"):
            try:
                value = self._client.get(key, timeout=10.0)
            except Exception:                   # noqa: BLE001 - unreadable is None
                continue
            if value is not None:
                found[key] = int(value)
                setattr(self, key, int(value))
        return found

    def start_exposure(self, seconds: float, light: bool = True) -> None:
        self._require()
        self._client.put("startexposure", Duration=float(seconds), Light=bool(light))

    def abort_exposure(self) -> None:
        if self._connected and self.can_abort:
            self._client.put("abortexposure")

    @property
    def image_ready(self) -> bool:
        return bool(self._client.get_cached("imageready", 0.3, False))

    def get_image(self) -> np.ndarray:
        return self._client.image_array()

    @property
    def temperature(self) -> float | None:
        value = self._client.get_cached("ccdtemperature", 1.0)
        return None if value is None else round(float(value), 2)

    @property
    def cooler_on(self) -> bool:
        return bool(self._client.get_cached("cooleron", 1.0, False))

    def set_cooler(self, on: bool) -> None:
        self._require()
        self._client.put("cooleron", CoolerOn=bool(on))

    @property
    def setpoint(self) -> float | None:
        value = self._client.get_cached("setccdtemperature", 2.0)
        return None if value is None else round(float(value), 2)

    def set_setpoint(self, celsius: float) -> None:
        self._require()
        self._client.put("setccdtemperature", SetCCDTemperature=float(celsius))

    @property
    def cooler_power(self) -> float | None:
        value = self._client.get_cached("coolerpower", 1.0)
        return None if value is None else round(float(value), 1)

    def set_settings(self, binning: int | None = None, gain: int | None = None,
                     offset: int | None = None) -> None:
        self._require()
        if binning is not None:
            self._client.put("binx", BinX=int(binning))
            self._client.put("biny", BinY=int(binning))
            self.binning = int(binning)
        if gain is not None:
            self._client.put("gain", Gain=int(gain))
            self.gain = int(gain)
        if offset is not None:
            self._client.put("offset", Offset=int(offset))
            self.offset = int(offset)

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        state = int(self._client.get_cached("camerastate", 0.3, 0) or 0)
        percent = float(self._client.get_cached("percentcompleted", 0.3, 0) or 0)
        duration = float(self._client.get_cached("lastexposureduration", 2.0, 0) or 0)
        return {
            "connected": True,
            "exposing": state in (1, 2, 3),
            "imageReady": self.image_ready,
            "elapsed": round(duration * percent / 100.0, 2),
            "duration": round(duration, 2),
            "temperature": self.temperature,
            "setpoint": self.setpoint,
            "coolerOn": self.cooler_on,
            "coolerPower": self.cooler_power,
            "canCool": self.can_cool,
            "gain": self.gain,
            "offset": self.offset,
            "binning": self.binning,
            "gainRange": [self.gain_min, self.gain_max],
            "offsetRange": [self.offset_min, self.offset_max],
            "maxBin": self.max_bin,
            "sensor": [self.sensor_width, self.sensor_height],
            "pixelSizeUm": self.pixel_size_um,
        }


class AlpacaMount(_AlpacaDevice, Mount):
    def __init__(self, driver_id: str, name: str) -> None:
        Mount.__init__(self, driver_id, name)
        self._setup(driver_id, "mount")
        self._jogging = False

    def connect(self) -> None:
        super().connect()
        self.can_park = bool(self._client.get("canpark"))
        self.can_sync = bool(self._client.get("cansync"))
        self.can_set_tracking = bool(self._client.get("cansettracking"))
        self.can_slew = bool(self._client.get("canslewasync"))
        self.can_find_home = bool(self._client.get("canfindhome"))

    @property
    def ra(self) -> float:
        return float(self._client.get_cached("rightascension", 0.3, 0.0) or 0.0)

    @property
    def dec(self) -> float:
        return float(self._client.get_cached("declination", 0.3, 0.0) or 0.0)

    @property
    def altitude(self) -> float | None:
        value = self._client.get_cached("altitude", 0.5)
        return None if value is None else round(float(value), 2)

    @property
    def azimuth(self) -> float | None:
        value = self._client.get_cached("azimuth", 0.5)
        return None if value is None else round(float(value), 2)

    @property
    def slewing(self) -> bool:
        return bool(self._client.get_cached("slewing", 0.3, False)) or self._jogging

    @property
    def pulse_guiding(self) -> bool:
        try:
            return bool(self._client.get_cached("ispulseguiding", 0.3, False))
        except Exception:                          # noqa: BLE001 - a driver without it
            return False

    @property
    def tracking(self) -> bool:
        return bool(self._client.get_cached("tracking", 0.5, False))

    @property
    def at_park(self) -> bool:
        return bool(self._client.get_cached("atpark", 1.0, False))

    @property
    def at_home(self) -> bool:
        return bool(self._client.get_cached("athome", 1.0, False))

    @property
    def side_of_pier(self) -> str | None:
        value = self._client.get_cached("sideofpier", 1.0)
        if value is None or int(value) < 0:
            return None
        return "east" if int(value) == 0 else "west"

    @property
    def site(self) -> dict[str, float] | None:
        latitude = self._client.get_cached("sitelatitude", 30.0)
        longitude = self._client.get_cached("sitelongitude", 30.0)
        if latitude is None or longitude is None:
            return None
        return {"latitude": float(latitude), "longitude": float(longitude),
                "elevation": float(self._client.get_cached("siteelevation", 30.0, 0.0) or 0.0)}

    def reported_clock(self) -> dict[str, Any]:
        """``utcdate`` and ``siderealtime``. GET only; the clock is not set."""
        utc = lst = None
        try:
            utc = self._client.get("utcdate", timeout=8.0)
        except DeviceError:
            utc = None
        try:
            lst = self._client.get("siderealtime", timeout=8.0)
        except DeviceError:
            lst = None
        return {"utc": utc, "lst": lst}

    def slew_to(self, ra_hours: float, dec_deg: float) -> None:
        self._require()
        self._client.put("slewtocoordinatesasync",
                         RightAscension=float(ra_hours), Declination=float(dec_deg))

    def sync_to(self, ra_hours: float, dec_deg: float) -> None:
        self._require()
        self._client.put("synctocoordinates",
                         RightAscension=float(ra_hours), Declination=float(dec_deg))

    def abort_slew(self) -> None:
        self._require()
        self.stop_jog()
        self._client.put("abortslew")

    def set_tracking(self, on: bool) -> None:
        self._require()
        self._client.put("tracking", Tracking=bool(on))

    def park(self) -> None:
        self._require()
        self._client.put("park")

    def unpark(self) -> None:
        self._require()
        self._client.put("unpark")

    def find_home(self) -> None:
        self._require()
        self._client.put("findhome")

    def jog(self, direction: str, rate_deg_s: float) -> None:
        self._require()
        axis = 0 if direction in ("east", "west") else 1
        sign = 1.0 if direction in ("north", "east") else -1.0
        self._client.put("moveaxis", Axis=axis, Rate=sign * float(rate_deg_s))
        self._jogging = True

    def stop_jog(self) -> None:
        if not self._connected:
            return
        for axis in (0, 1):
            try:
                self._client.put("moveaxis", Axis=axis, Rate=0.0)
            except DeviceError:
                pass
        self._jogging = False

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        return {
            "connected": True,
            "ra": round(self.ra, 5),
            "dec": round(self.dec, 4),
            "altitude": self.altitude,
            "azimuth": self.azimuth,
            "lst": round(float(self._client.get_cached("siderealtime", 1.0, 0.0) or 0.0), 4),
            "slewing": self.slewing,
            "tracking": self.tracking,
            "atPark": self.at_park,
            "atHome": self.at_home,
            "canFindHome": self.can_find_home,
            "sideOfPier": self.side_of_pier,
        }


class AlpacaFilterWheel(_AlpacaDevice, FilterWheel):
    def __init__(self, driver_id: str, name: str) -> None:
        FilterWheel.__init__(self, driver_id, name)
        self._setup(driver_id, "filterwheel")
        self._names: list[str] = []

    def connect(self) -> None:
        super().connect()
        self._names = [str(n) for n in (self._client.get("names") or [])]

    @property
    def names(self) -> list[str]:
        return self._named(self._names)

    @property
    def position(self) -> int:
        value = self._client.get_cached("position", 0.3)
        return -1 if value is None else int(value)

    def set_position(self, index: int) -> None:
        self._require()
        if not 0 <= index < len(self._names):
            raise DeviceError(f"filter slot must be 0..{len(self._names) - 1}")
        self._client.put("position", Position=int(index))

    # `status` comes from FilterWheel: it is the same for every backend.


class AlpacaRotator(_AlpacaDevice, Rotator):
    def __init__(self, driver_id: str, name: str) -> None:
        Rotator.__init__(self, driver_id, name)
        self._setup(driver_id, "rotator")

    def connect(self) -> None:
        super().connect()
        self.can_reverse = bool(self._client.get("canreverse"))
        try:
            self.step_size = float(self._client.get("stepsize") or 0) or None
        except DeviceError:
            self.step_size = None

    @property
    def position(self) -> float:
        return float(self._client.get_cached("position", 0.3, 0.0) or 0.0) % 360.0

    @property
    def mechanical_position(self) -> float | None:
        value = self._client.get_cached("mechanicalposition", 0.3)
        return None if value is None else round(float(value) % 360.0, 3)

    @property
    def target_position(self) -> float | None:
        value = self._client.get_cached("targetposition", 0.3)
        return None if value is None else round(float(value) % 360.0, 3)

    @property
    def moving(self) -> bool:
        return bool(self._client.get_cached("ismoving", 0.3, False))

    @property
    def reversed(self) -> bool:
        return bool(self._client.get_cached("reverse", 2.0, False))

    def move_absolute(self, position_angle: float) -> None:
        self._require()
        self._client.put("moveabsolute", Position=float(position_angle) % 360.0)

    def move_relative(self, delta: float) -> None:
        self._require()
        self._client.put("move", Position=float(delta))

    def halt(self) -> None:
        self._require()
        self._client.put("halt")

    def sync(self, position_angle: float) -> None:
        self._require()
        self._client.put("sync", Position=float(position_angle) % 360.0)

    def set_reversed(self, reverse: bool) -> None:
        self._require()
        if not self.can_reverse:
            raise DeviceError("this rotator cannot be reversed")
        self._client.put("reverse", Reverse=bool(reverse))

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        return {
            "connected": True,
            "position": round(self.position, 3),
            "mechanical": self.mechanical_position,
            "target": self.target_position,
            "moving": self.moving,
            "canReverse": self.can_reverse,
            "reversed": self.reversed,
            "stepSize": self.step_size,
        }


class AlpacaFocuser(_AlpacaDevice, Focuser):
    def __init__(self, driver_id: str, name: str) -> None:
        Focuser.__init__(self, driver_id, name)
        self._setup(driver_id, "focuser")

    def connect(self) -> None:
        super().connect()
        self.max_step = int(self._client.get("maxstep") or 0)
        self.is_absolute = bool(self._client.get("absolute"))
        try:
            self.step_size_um = float(self._client.get("stepsize") or 0) or None
        except DeviceError:
            self.step_size_um = None

    @property
    def position(self) -> int:
        return int(self._client.get_cached("position", 0.3, 0) or 0)

    @property
    def moving(self) -> bool:
        return bool(self._client.get_cached("ismoving", 0.3, False))

    @property
    def temperature(self) -> float | None:
        value = self._client.get_cached("temperature", 5.0)
        return None if value is None else round(float(value), 2)

    def move_to(self, position: int) -> None:
        self._require()
        target = int(position)
        if self.max_step and not 0 <= target <= self.max_step:
            raise DeviceError(f"position must be 0..{self.max_step}")
        self._client.put("move", Position=target if self.is_absolute else target - self.position)

    def move_relative(self, delta: int) -> None:
        self._require()
        if self.is_absolute:
            self.move_to(self.position + int(delta))
        else:
            self._client.put("move", Position=int(delta))

    def halt(self) -> None:
        self._require()
        self._client.put("halt")

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        position = self.position
        return {
            "connected": True,
            "position": position,
            "target": position,
            "moving": self.moving,
            "maxStep": self.max_step,
            "stepSizeUm": self.step_size_um,
            "temperature": self.temperature,
        }


class AlpacaFlatPanel(_AlpacaDevice, FlatPanel):
    _COVER_STATES = {0: "notpresent", 1: "closed", 2: "moving", 3: "open", 4: "unknown",
                     5: "error"}

    def __init__(self, driver_id: str, name: str) -> None:
        FlatPanel.__init__(self, driver_id, name)
        self._setup(driver_id, "flatpanel")

    def connect(self) -> None:
        super().connect()
        self.max_brightness = int(self._client.get("maxbrightness") or 100)

    @property
    def brightness(self) -> int:
        return int(self._client.get_cached("brightness", 1.0, 0) or 0)

    @property
    def light_on(self) -> bool:
        return int(self._client.get_cached("calibratorstate", 1.0, 1) or 1) == 3

    @property
    def cover_state(self) -> str:
        missing = object()
        value = self._client.get_cached("coverstate", 1.0, missing)
        if value is missing or value is None:
            return "unread"
        try:
            number = int(value)
        except (TypeError, ValueError):
            return "unread"
        return self._COVER_STATES.get(number, "unread")

    def turn_on(self, brightness: int) -> None:
        self._require()
        self._client.put("calibratoron", Brightness=int(brightness))

    def turn_off(self) -> None:
        self._require()
        self._client.put("calibratoroff")

    def open_cover(self) -> None:
        self._require()
        self._client.put("opencover")

    def close_cover(self) -> None:
        self._require()
        self._client.put("closecover")

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        return {
            "connected": True,
            "brightness": self.brightness,
            "maxBrightness": self.max_brightness,
            "lightOn": self.light_on,
            "hasCover": self.has_cover,
            "coverState": self.cover_state,
            "coverPrepare": self.cover_prepare(),
        }


class AlpacaSafetyMonitor(_AlpacaDevice, SafetyMonitor):
    def __init__(self, driver_id: str, name: str) -> None:
        SafetyMonitor.__init__(self, driver_id, name)
        self._setup(driver_id, "safetymonitor")

    @property
    def is_safe(self) -> bool:
        # False when it cannot be asked: a monitor that has stopped answering is
        # not evidence that the weather is fine.
        return bool(self._client.get_cached("issafe", 2.0, False))

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        return {"connected": True, "safe": self.is_safe}


class AlpacaDome(_AlpacaDevice, Dome):
    _SHUTTER_STATES = {0: "open", 1: "closed", 2: "opening", 3: "closing", 4: "error"}

    def __init__(self, driver_id: str, name: str) -> None:
        Dome.__init__(self, driver_id, name)
        self._setup(driver_id, "dome")

    def connect(self) -> None:
        super().connect()
        self.can_park = bool(self._client.get("canpark"))
        self.can_shutter = bool(self._client.get("cansetshutter"))
        self.can_slave = bool(self._client.get("canslave"))

    @property
    def shutter_state(self) -> str:
        if not self.can_shutter:
            return "notpresent"
        value = self._client.get_cached("shutterstatus", 1.0, 4)
        return self._SHUTTER_STATES.get(int(value if value is not None else 4), "error")

    @property
    def at_park(self) -> bool:
        return bool(self._client.get_cached("atpark", 1.0, False))

    @property
    def slewing(self) -> bool:
        return bool(self._client.get_cached("slewing", 0.5, False))

    @property
    def slaved(self) -> bool:
        return bool(self._client.get_cached("slaved", 1.0, False))

    @property
    def azimuth(self) -> float | None:
        value = self._client.get_cached("azimuth", 1.0)
        return None if value is None else float(value)

    def open_shutter(self) -> None:
        self._require()
        from ..roofguard import blocked
        why = blocked()
        if why:
            raise DeviceError(f"{why}; OpenShutter was not sent")
        self.begin_shutter_command("open")
        try:
            self._client.put("openshutter")
        except Exception as exc:
            self.fail_shutter_command(str(exc))
            raise

    def close_shutter(self) -> None:
        self._require()
        self.begin_shutter_command("close")
        try:
            self._client.put("closeshutter")
        except Exception as exc:
            self.fail_shutter_command(str(exc))
            raise

    def park(self) -> None:
        self._require()
        self._client.put("park")

    def set_slaved(self, on: bool) -> None:
        self._require()
        self._client.put("slaved", Slaved=bool(on))

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {
                "connected": False,
                "shutterState": "unknown",
                "shutterVerified": False,
                "shutterQuality": "unknown",
                "shutterNote": self.SHUTTER_NOTE,
            }
        # A missing shutterstatus stays error (the cache default), never open.
        report = self.shutter_report(self.shutter_state)
        return {
            "connected": True,
            "canShutter": self.can_shutter,
            "canPark": self.can_park,
            "canSlave": self.can_slave,
            "slaved": self.slaved,
            "atPark": self.at_park,
            "slewing": self.slewing,
            "azimuth": self.azimuth,
            **report,
        }


class AlpacaSwitch(_AlpacaDevice, SwitchBank):
    def __init__(self, driver_id: str, name: str) -> None:
        SwitchBank.__init__(self, driver_id, name)
        self._setup(driver_id, "switch")
        self._channels: list[dict[str, Any]] = []

    def connect(self) -> None:
        super().connect()
        self._channels = self._describe()

    def _describe(self) -> list[dict[str, Any]]:
        count = int(self._client.get("maxswitch") or 0)
        found: list[dict[str, Any]] = []
        for index in range(count):
            try:
                minimum = float(self._client.get("minswitchvalue", Id=index) or 0.0)
                maximum = float(self._client.get("maxswitchvalue", Id=index) or 1.0)
                step = float(self._client.get("switchstep", Id=index) or 1.0)
                found.append({
                    "index": index,
                    "name": str(self._client.get("getswitchname", Id=index)
                                or f"Switch {index}"),
                    "description": str(self._client.get("getswitchdescription",
                                                        Id=index) or ""),
                    "min": minimum,
                    "max": maximum,
                    "step": step,
                    "boolean": minimum == 0.0 and maximum == 1.0 and step >= 1.0,
                    "writable": bool(self._client.get("canwrite", Id=index)),
                })
            except Exception:                     # noqa: BLE001 - skip a bad channel
                continue
        return found

    @property
    def channels(self) -> list[dict[str, Any]]:
        out = []
        for channel in self._channels:
            entry = dict(channel)
            try:
                entry["value"] = float(
                    self._client.get("getswitchvalue", Id=channel["index"]))
            except Exception:                     # noqa: BLE001 - reported as unknown
                entry["value"] = None
            out.append(entry)
        return out

    def describe_channels(self) -> list[dict[str, Any]]:
        return [dict(channel) for channel in self._channels]

    def get_value(self, index: int) -> float:
        self._require()
        return float(self._client.get("getswitchvalue", Id=int(index)))

    def set_value(self, index: int, value: float) -> None:
        self._require()
        channel = next((c for c in self._channels if c["index"] == int(index)), None)
        if channel is None:
            raise DeviceError(f"this switch has no channel {index}")
        if not channel["writable"]:
            raise DeviceError(f"{channel['name']} is read-only")
        if channel["boolean"]:
            self._client.put("setswitch", Id=int(index), State=bool(value))
        else:
            self._client.put("setswitchvalue", Id=int(index), Value=float(value))

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        return {"connected": True, "channels": self.channels}


FACTORIES = {
    "camera": AlpacaCamera,
    "mount": AlpacaMount,
    "filterwheel": AlpacaFilterWheel,
    "focuser": AlpacaFocuser,
    "rotator": AlpacaRotator,
    "flatpanel": AlpacaFlatPanel,
    "piercam": AlpacaCamera,
    "safetymonitor": AlpacaSafetyMonitor,
    "dome": AlpacaDome,
    "switch": AlpacaSwitch,
}


def create(kind: str, driver_id: str, name: str):
    factory = FACTORIES.get(kind)
    if factory is None:
        raise DeviceError(f"no Alpaca driver class for {kind}")
    return factory(driver_id, name)
