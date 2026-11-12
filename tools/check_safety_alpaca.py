"""The network safety monitor (0.2.53a): https Alpaca, failing closed.

    python tools/check_safety_alpaca.py            # simulated servers + live read
    python tools/check_safety_alpaca.py --offline  # simulated servers only

2026-11-04, the RASA: its safety monitor was the ASCOM Generic File driver on
Z:\\roof\\building-2\\RoofStatusFile.txt, and Z: is disconnected. 0.2.53's
Alpaca client only built http://host:port URLs, and the Starfront roof server
https://alpaca-api.tx.starfront.space answers plain http with a 500. So the
rig had no monitor, and no monitor reads *safe*.

Here a local https server with a throwaway test CA plays the roof server
through every answer it can give - open, closed, an error, a 500, a hang, a
certificate the PC does not trust, a stale report, a clock 200 s out - and
the rule checked throughout is that the configured source only ever reads
safe on a fresh, complete, consistent "safe". Plain http still works, and a
rig that does not configure the source behaves exactly as before. Then one
read-only GET against the real server (no PUT).

No test framework, like the other checks here. Needs the openssl command.
"""

from __future__ import annotations

import email.utils
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol import netsafety, safety                      # noqa: E402
from astrocontrol.config import DEFAULTS, Config                # noqa: E402
from astrocontrol.devices import alpaca                         # noqa: E402
from astrocontrol.devices.base import DeviceError               # noqa: E402

results: list[bool] = []


def case(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return bool(ok)


# ---------------------------------------------------------------------------
# A throwaway CA and a certificate for localhost / 127.0.0.1
# ---------------------------------------------------------------------------

def make_pki(folder: Path) -> dict[str, Path]:
    def run(*args: str) -> None:
        subprocess.run(["openssl", *args], cwd=folder, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for ca in ("ca", "other"):
        run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-keyout", f"{ca}.key", "-out", f"{ca}.pem", "-subj", f"/CN=Starfront test {ca}",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    (folder / "ext.cnf").write_text(
        "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key",
        "-out", "server.csr", "-subj", "/CN=localhost")
    run("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
        "-CAcreateserial", "-days", "2", "-out", "server.pem", "-extfile", "ext.cnf")
    # A second certificate for a name the client will not ask for.
    (folder / "ext2.cnf").write_text(
        "basicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:roof.example.invalid\n")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "wrong.key",
        "-out", "wrong.csr", "-subj", "/CN=roof.example.invalid")
    run("x509", "-req", "-in", "wrong.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
        "-CAcreateserial", "-days", "2", "-out", "wrong.pem", "-extfile", "ext2.cnf")
    return {n: folder / n for n in ("ca.pem", "other.pem", "server.pem", "server.key",
                                    "wrong.pem", "wrong.key")}


# ---------------------------------------------------------------------------
# The simulated roof server
# ---------------------------------------------------------------------------

def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Roof:
    """What the server says. Changed between cases."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.safe = True             # issafe Value
        self.is_open = True          # roof/state is_open for our building
        self.update_offset = -20.0   # state_update relative to now
        self.clock_offset = 0.0      # server Date header relative to now
        self.status = 200
        self.delay = 0.0
        self.body: bytes | None = None       # raw issafe body override
        self.roof_body: bytes | None = None
        self.error_number = 0
        self.no_roof_state = False
        self.requests: list[tuple[str, str]] = []


ROOF = Roof()
DEVICE = 2


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:          # quiet
        pass

    def date_time_string(self, timestamp=None) -> str:
        return email.utils.formatdate(time.time() + ROOF.clock_offset, usegmt=True)

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self) -> None:                        # noqa: N802
        ROOF.requests.append(("PUT", self.path))
        self._send(405, b"{}")

    def do_GET(self) -> None:                        # noqa: N802
        ROOF.requests.append(("GET", self.path))
        if ROOF.delay:
            time.sleep(ROOF.delay)
        path = self.path.split("?", 1)[0]
        if path.endswith(f"/api/v1/safetymonitor/{DEVICE}/issafe"):
            if ROOF.status != 200:
                return self._send(ROOF.status, b"Internal Server Error")
            body = ROOF.body if ROOF.body is not None else json.dumps({
                "Value": ROOF.safe, "ErrorNumber": ROOF.error_number,
                "ErrorMessage": "boom" if ROOF.error_number else "",
                "ClientTransactionID": 1, "ServerTransactionID": 9}).encode()
            return self._send(200, body)
        if path.endswith("/api/v1/roof/state"):
            if ROOF.no_roof_state:
                return self._send(404, b"not found")
            now = time.time() + ROOF.clock_offset
            body = ROOF.roof_body if ROOF.roof_body is not None else json.dumps([
                {"device_number": 1, "name": "Building 1", "is_open": False,
                 "state_update": iso(now - 5)},
                {"device_number": DEVICE, "name": "Building 2", "is_open": ROOF.is_open,
                 "state_update": iso(now + ROOF.update_offset)},
            ]).encode()
            return self._send(200, body)
        if path.endswith("/management/v1/configureddevices"):
            return self._send(200, json.dumps({"Value": [
                {"DeviceName": "Building 2", "DeviceType": "safetymonitor",
                 "DeviceNumber": DEVICE}]}).encode())
        if path.endswith(f"/api/v1/safetymonitor/{DEVICE}/connected"):
            return self._send(200, json.dumps({"Value": True, "ErrorNumber": 0}).encode())
        self._send(404, b"{}")


def serve(cert: Path | None = None, key: Path | None = None) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.handle_error = lambda *_a: None          # a client that gave up is the point
    if cert is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self) -> None:
        self.offset = 0.0

    def mono(self) -> float:
        return time.monotonic() + self.offset


def source(port: int, scheme: str = "https", clock: FakeClock | None = None, **extra):
    settings = {"source": "alpaca", "alpacaScheme": scheme, "alpacaHost": "localhost",
                "alpacaPort": port, "alpacaDevice": DEVICE, "alpacaTimeoutSeconds": 1.5,
                "alpacaPollSeconds": 30.0, "alpacaRetrySeconds": 0.0}
    settings.update(extra)
    logs: list[str] = []
    src = netsafety.AlpacaSafetySource(lambda: settings,
                                       monotonic=(clock.mono if clock else time.monotonic),
                                       log=lambda m, level: logs.append(m))
    return src, settings, logs


def fresh(src) -> tuple[bool, str | None]:
    src.poll(force=True)
    return src.read()


class Rigs:
    def __init__(self, devices: dict) -> None:
        self.devices = devices

    @property
    def master(self):
        return self

    @property
    def manager(self):
        return self

    def get(self, kind):
        return self.devices.get(kind)


def make_watcher(devices: dict, **safety_settings):
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    config.update("safety", safety_settings)
    calls = {"unsafe": 0, "detail": None}

    def unsafe(detail):
        calls["unsafe"] += 1
        calls["detail"] = detail

    return safety.SafetyWatcher(Rigs(devices), config, on_unsafe=unsafe,
                                on_safe=lambda: None), calls


def main() -> int:
    offline = "--offline" in sys.argv
    if shutil.which("openssl") is None:
        print("SKIP  openssl not found; cannot make a test CA")
        return 0
    tmp = Path(tempfile.mkdtemp())
    pki = make_pki(tmp)
    trusted = ssl.create_default_context(cafile=str(pki["ca.pem"]))
    untrusted = ssl.create_default_context(cafile=str(pki["other.pem"]))
    https, hport = serve(pki["server.pem"], pki["server.key"])
    wrong, wport = serve(pki["wrong.pem"], pki["wrong.key"])
    plain, pport = serve()

    # -- addressing ----------------------------------------------------------
    case("legacy host:port/type/n is still plain http",
         alpaca.parse_address("192.168.1.5:11111/safetymonitor/0")
         == ("http://192.168.1.5:11111", "safetymonitor", 0))
    case("https URL with default port",
         alpaca.parse_address("https://alpaca-api.tx.starfront.space/safetymonitor/2")
         == ("https://alpaca-api.tx.starfront.space:443", "safetymonitor", 2))
    case("https URL with port and base path",
         alpaca.parse_address("https://h.example:8443/a/b/safetymonitor/7")
         == ("https://h.example:8443/a/b", "safetymonitor", 7))
    try:
        alpaca.parse_address("ftp://h/safetymonitor/1")
        case("a scheme other than http/https is refused", False)
    except DeviceError:
        case("a scheme other than http/https is refused", True)
    ctx = alpaca.tls_context()
    case("TLS verification is on (CERT_REQUIRED + hostname check)",
         ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname)
    case("a CA bundle ships next to the Alpaca client",
         (ROOT / "astrocontrol" / "devices" / "cacert.pem").stat().st_size > 100_000)
    spec = (ROOT / "packaging" / "starfront.spec").read_text()
    case("the frozen build packs the CA bundle", "cacert.pem" in spec and '"certifi"' in spec)

    # Everything below trusts the test CA, as the PC trusts Let's Encrypt.
    alpaca._TLS = trusted

    # -- the Alpaca device over https -----------------------------------------
    ROOF.reset()
    devices = alpaca.configured_devices(f"https://localhost:{hport}", 11111)
    case("configured devices over https keep the https id",
         devices and devices[0]["id"] == f"https://localhost:{hport}/safetymonitor/{DEVICE}",
         devices[0]["id"] if devices else "none")
    monitor = alpaca.AlpacaSafetyMonitor(f"https://localhost:{hport}/safetymonitor/{DEVICE}", "b2")
    monitor._connected = True
    case("AlpacaSafetyMonitor reads issafe over https", monitor.is_safe is True)
    monitor_http = alpaca.AlpacaSafetyMonitor(f"127.0.0.1:{pport}/safetymonitor/{DEVICE}", "b2")
    monitor_http._connected = True
    case("AlpacaSafetyMonitor still reads plain http (legacy id)", monitor_http.is_safe is True)

    # -- the network source ---------------------------------------------------
    ROOF.reset()
    src, settings, logs = source(hport)
    case("https open: safe", fresh(src) == (True, None), str(src.status()))
    st = src.status()
    case("status line has source, age and skew",
         st["name"].startswith("Alpaca https://localhost:") and st["lastUpdateAge"] is not None
         and st["skewSeconds"] is not None and abs(st["skewSeconds"]) < 5
         and "(Building 2)" in st["name"], str(st))
    case("no PUT is ever sent by the source", not any(m == "PUT" for m, _ in ROOF.requests))

    ROOF.reset(); ROOF.safe = False; ROOF.is_open = False
    ok, why = fresh(src)
    case("https closed: unsafe, confirmed by roof/state", ok is False
         and "closed" in (why or "") and src.confirmed_reads() >= 1, str(why))
    first = src.confirmed_reads()
    fresh(src)
    case("each further confirmed read counts up", src.confirmed_reads() > first)
    ROOF.reset()
    fresh(src)
    case("a safe read clears the confirmed count", src.confirmed_reads() == 0)
    ROOF.reset(); ROOF.safe = False
    ok, why = fresh(src)
    case("issafe false but roof/state open: unsafe, unconfirmed (disagreement)",
         ok is False and "disagrees" in (why or "") and src.confirmed_reads() == 0, str(why))

    ROOF.reset(); ROOF.status = 500
    ok, why = fresh(src)
    case("HTTP 500: unsafe", ok is False and "500" in (why or ""), why or "")
    case("the failure is logged once as a warning",
         any("reading UNSAFE" in m for m in logs))

    ROOF.reset(); ROOF.delay = 2.5
    t0 = time.monotonic()
    ok, why = fresh(src)
    took = time.monotonic() - t0
    case("timeout: unsafe, bounded by the timeout", ok is False
         and took < 2 * 1.5 + 0.8, f"{why} after {took:.1f} s")
    ROOF.reset()
    time.sleep(2.0)                       # let the slow handler finish

    ROOF.reset(); ROOF.body = b"<html>oops"
    ok, why = fresh(src)
    case("malformed JSON: unsafe", ok is False and "JSON" in (why or ""), why or "")
    ROOF.reset(); ROOF.body = b'{"Value": "true", "ErrorNumber": 0}'
    case("Value that is not a boolean: unsafe", fresh(src)[0] is False)
    ROOF.reset(); ROOF.error_number = 1031
    case("Alpaca ErrorNumber: unsafe", fresh(src)[0] is False)
    ROOF.reset(); ROOF.is_open = False
    ok, why = fresh(src)
    case("issafe true but roof/state closed: unsafe, an explicit close (confirmed)",
         ok is False and "roof/state says closed" in (why or "")
         and src.confirmed_reads() >= 1, why or "")
    ROOF.reset(); ROOF.roof_body = b'[{"device_number": 9, "is_open": true}]'
    case("building missing from roof/state: unsafe", fresh(src)[0] is False)
    ROOF.reset(); ROOF.no_roof_state = True
    case("roof/state 404 with the check on: unsafe", fresh(src)[0] is False)
    settings["alpacaCheckRoofState"] = False
    case("roof/state check off: issafe alone decides", fresh(src) == (True, None))
    settings["alpacaCheckRoofState"] = True

    ROOF.reset(); ROOF.update_offset = -600.0
    ok, why = fresh(src)
    case("stale building report (10 min): unsafe", ok is False and "old" in (why or ""), why or "")

    ROOF.reset(); ROOF.clock_offset = 200.0
    ok, why = fresh(src)
    case("server clock 200 s ahead of the PC: unsafe", ok is False and "clock" in (why or ""),
         why or "")
    ROOF.reset(); ROOF.clock_offset = -200.0
    case("server clock 200 s behind the PC: unsafe", fresh(src)[0] is False)
    ROOF.reset(); ROOF.clock_offset = 100.0
    case("100 s skew is inside the 120 s limit: safe", fresh(src) == (True, None))
    ROOF.reset(); ROOF.update_offset = 200.0
    case("a roof report 200 s in the PC's future: unsafe", fresh(src)[0] is False)

    # No answer for longer than staleSeconds, on a clock we control.
    ROOF.reset()
    clock = FakeClock()
    src2, settings2, _ = source(hport, clock=clock)
    case("fresh before going quiet", fresh(src2) == (True, None))
    https_down = {"down": True}
    real_fetch = src2._fetch
    src2._fetch = lambda url, t: (_ for _ in ()).throw(OSError("network unreachable")) \
        if https_down["down"] else real_fetch(url, t)
    settings2["alpacaPollSeconds"] = 1000.0     # pretend no poll is due
    clock.offset += 181.0
    ok, why = src2.read()
    case("no good answer for > 180 s: unsafe even with no poll due", ok is False
         and "no good answer" in (why or ""), why or "")
    settings2["alpacaPollSeconds"] = 30.0
    ok, why = src2.read()
    case("network down: unsafe", ok is False and "unreachable" in (why or ""), why or "")
    https_down["down"] = False
    settings2["alpacaRetrySeconds"] = 5.0
    case("retries quickly after a failure (5 s, not the 30 s poll)",
         (lambda: (clock.__setattr__("offset", clock.offset + 6.0), src2.read())[1])()
         == (True, None))

    # TLS failures.
    src3, _, _ = source(hport)
    alpaca._TLS = untrusted
    ok, why = fresh(src3)
    case("certificate from a CA the PC does not trust: unsafe", ok is False
         and "CERTIFICATE" in (why or "").upper(), why or "")
    alpaca._TLS = trusted
    src4, _, _ = source(wport)
    ok, why = fresh(src4)
    case("certificate for another host name: unsafe", ok is False
         and ("hostname" in (why or "").lower() or "match" in (why or "").lower()), why or "")
    src5, _, _ = source(hport, scheme="http")
    case("speaking http to an https server: unsafe", fresh(src5)[0] is False)

    # Plain http still works for the source.
    ROOF.reset()
    src6, _, _ = source(pport, scheme="http")
    case("plain http source: safe", fresh(src6) == (True, None))

    # Nobody listening.
    sock_port = plain.server_address[1]
    plain.shutdown(); plain.server_close()
    case("connection refused: unsafe", fresh(src6)[0] is False)
    del sock_port

    # -- the watcher ------------------------------------------------------------
    ROOF.reset()
    w, calls = make_watcher({})
    case("no source configured, no monitor: unchanged (None, reads safe)",
         w.read() is None and w.safe and w.status()["source"] is None
         and not w.status()["connected"])

    base = {"source": "alpaca", "alpacaScheme": "https", "alpacaHost": "localhost",
            "alpacaPort": hport, "alpacaDevice": DEVICE, "alpacaRetrySeconds": 0.0,
            "graceSeconds": 0.0, "alpacaTimeoutSeconds": 1.5}
    w, calls = make_watcher({}, **base)
    case("source configured, server safe: watcher reads safe", w.read() is True)
    st = w.status()
    case("watcher status: connected, named, with the source line",
         st["connected"] and st["watching"] and st["source"]["source"] == "alpaca"
         and "Alpaca https://localhost" in (st["name"] or ""), str(st["name"]))

    ROOF.status = 500
    w, calls = make_watcher({}, unreachableIsUnsafe=False, debounceSeconds=0.0, **base)
    case("source failing reads unsafe even with unreachableIsUnsafe off",
         w.read() is False)
    w._poll()
    case("and the watcher acts on it (grace 0, debounce 0)", calls["unsafe"] == 1 and not w.safe,
         str(calls["detail"]))
    ROOF.reset()

    class SlotMonitor:
        connected = True
        name = "slot"

        def __init__(self, safe):
            self._safe = safe

        @property
        def is_safe(self):
            return self._safe

    w, _ = make_watcher({"safetymonitor": SlotMonitor(False)}, **base)
    case("source safe but an Equipment monitor says unsafe: unsafe", w.read() is False)
    w, _ = make_watcher({"safetymonitor": SlotMonitor(True)}, **base)
    case("source safe and the Equipment monitor safe: safe", w.read() is True)

    class Gone:
        connected = False
        name = "gone"

    w, _ = make_watcher({"safetymonitor": Gone()}, **base)
    ROOF.status = 503
    case("source configured, slot monitor disconnected, server failing: unsafe",
         w.read() is False)
    ROOF.reset()
    w, _ = make_watcher({}, enabled=False, **base)
    case("watching switched off: as before (None)", w.read() is None)

    # -- settings ----------------------------------------------------------------
    keys = ("source", "alpacaScheme", "alpacaHost", "alpacaPort", "alpacaBasePath",
            "alpacaDevice", "alpacaCheckRoofState", "staleSeconds", "maxClockSkewSeconds")
    case("settings defaults exist and default to off/http",
         all(k in DEFAULTS["safety"] for k in keys) and DEFAULTS["safety"]["source"] == ""
         and DEFAULTS["safety"]["alpacaScheme"] == "http"
         and DEFAULTS["safety"]["staleSeconds"] == 180.0
         and DEFAULTS["safety"]["maxClockSkewSeconds"] == 120.0)
    page = (ROOT / "astrocontrol" / "web" / "index.html").read_text("utf-8")
    js = (ROOT / "astrocontrol" / "web" / "app.js").read_text("utf-8")
    case("the Safety settings have scheme/host/port/path/device fields",
         all(f'id="{i}"' in page for i in ("safSource", "safAlpacaScheme", "safAlpacaHost",
                                           "safAlpacaPort", "safAlpacaPath", "safAlpacaDevice",
                                           "safSourceStatus", "safetySourceLine")))
    case("the page saves them", all(f"'{k}'" in js for k in keys))
    main_src = (ROOT / "astrocontrol" / "main.py").read_text("utf-8")
    case("the settings API accepts them",
         all(f"    {k}:" in main_src for k in keys))

    # -- live, read-only ------------------------------------------------------
    if offline:
        print("SKIP  live check (--offline)")
    else:
        alpaca._TLS = None                           # the real roots
        live = {"source": "alpaca", "alpacaScheme": "https",
                "alpacaHost": "alpaca-api.tx.starfront.space", "alpacaPort": 443,
                "alpacaDevice": 2}
        real = netsafety.AlpacaSafetySource(lambda: live)
        try:
            listing = alpaca.configured_devices("https://alpaca-api.tx.starfront.space", None,
                                                timeout=10)
        except DeviceError as exc:
            listing = []
            print(f"SKIP  live server unreachable from here: {exc}")
        if listing:
            names = {d["id"].rsplit("/", 1)[-1]: d["name"] for d in listing
                     if d["kind"] == "safetymonitor"}
            case("live: safetymonitor 2 is Building 2", names.get("2", "").startswith("Building 2"),
                 names.get("2", ""))
            real.poll(force=True)
            ok, why = real.read()
            st = real.status()
            case("live: a complete answer (no error), TLS verified",
                 st["error"] is None and isinstance(st["serverSafe"], bool), str(st))
            case("live: safe only when the server says so", ok is (st["serverSafe"] is True))
            print(f"INFO  live Building 2: {'OPEN/safe' if st['serverSafe'] else 'CLOSED/unsafe'}"
                  f", skew {st['skewSeconds']} s, report age {st['dataAge']} s")

    https.shutdown(); wrong.shutdown()
    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
