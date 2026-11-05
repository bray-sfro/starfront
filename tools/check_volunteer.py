"""The volunteer installer trusts a signature and a hash, and runs nothing else.

    python tools/check_volunteer.py

The real `collab-worker` and its signed manifest are built in another
repository, so this runs against a fake manifest server on loopback that is
signed with a key made here, and a fake `collab-worker` script that records
its argv.

  * The pure-Python Ed25519 verifier passes RFC 8032's vectors, rejects
    tampering, and agrees with `cryptography` on random keys.
  * With the placeholder key in place every install is refused and the
    server is never even asked for the archive.
  * A good signature installs: the hub's manifest is tried first, the archive
    is checked, `enrol` and `service install` are run with the right argv,
    and the worker's TOML carries the busy file, the idle policy and
    `pause_while_nina` as a TOML bool, both on install and when saved.
  * A bad signature, a wrong SHA-256, a size mismatch (larger and smaller),
    a downgrade, and path-traversal / link archives are each refused, nothing
    is installed, and the fake worker is never run.
  * A hub mirror with a bad signature falls back to the server's copy.
  * An update stops the service, swaps the program and starts it; an equal
    version is refused.  `collab-hub` installs at the supervisor's path.
  * The routes exist, never block, and report progress.
"""
import base64
import hashlib
import io
import json
import os
import random
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WORK = Path(tempfile.mkdtemp(prefix="check_volunteer_"))
os.environ["ASTRO_DATA_DIR"] = str(WORK / "data")
LOG = WORK / "worker-argv.log"
os.environ["FAKE_WORKER_LOG"] = str(LOG)

from cryptography.hazmat.primitives import serialization                     # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (                 # noqa: E402
    Ed25519PrivateKey, Ed25519PublicKey)

from astrocontrol import ed25519_verify, volunteer                           # noqa: E402
from astrocontrol.config import Config                                        # noqa: E402


class _WindowsFakes:
    """Windows cannot execute a script with a shebang (WinError 193), so there the
    fakes are handed to the interpreter. `volunteer` sees this in place of `subprocess`;
    everything but the fakes' own argv goes through untouched."""

    def __init__(self):
        self._real = subprocess

    def __getattr__(self, name):
        return getattr(self._real, name)

    @staticmethod
    def _argv(args):
        if os.name == "nt" and isinstance(args, (list, tuple)) and args \
                and Path(str(args[0])).name in {"collab-worker"}:
            return [sys.executable, *map(str, args)]
        return args

    def run(self, args, *a, **kw):
        return self._real.run(self._argv(args), *a, **kw)

    def Popen(self, args, *a, **kw):
        return self._real.Popen(self._argv(args), *a, **kw)


volunteer.subprocess = _WindowsFakes()

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


def until(predicate, timeout=20.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def b64(raw):
    return base64.b64encode(raw).decode()


def raw_public(priv):
    return priv.public_key().public_bytes(serialization.Encoding.Raw,
                                          serialization.PublicFormat.Raw)


# ------------------------------------------------------- the verifier alone
print("-- ed25519")
RFC = [
    ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]
for n, (pk, msg, sig) in enumerate(RFC, 1):
    pk, msg, sig = bytes.fromhex(pk), bytes.fromhex(msg), bytes.fromhex(sig)
    case(f"RFC 8032 vector {n} verifies", ed25519_verify.verify(pk, msg, sig))
    case(f"RFC 8032 vector {n} fails on a changed message",
         not ed25519_verify.verify(pk, msg + b"x", sig))
    flipped = bytearray(sig)
    flipped[10] ^= 1
    case(f"RFC 8032 vector {n} fails on a changed signature",
         not ed25519_verify.verify(pk, msg, bytes(flipped)))
    big_s = sig[:32] + (int.from_bytes(sig[32:], "little") + ed25519_verify._L).to_bytes(32, "little")
    case(f"RFC 8032 vector {n} fails with S + L (malleated)",
         not ed25519_verify.verify(pk, msg, big_s))
case("wrong lengths are refused", not ed25519_verify.verify(b"x" * 31, b"", b"y" * 64)
     and not ed25519_verify.verify(b"x" * 32, b"", b"y" * 63))
rng = random.Random(7)
agree = True
for _ in range(4):
    priv = Ed25519PrivateKey.generate()
    msg = rng.randbytes(rng.randint(0, 200))
    sig = priv.sign(msg)
    agree &= ed25519_verify.verify(raw_public(priv), msg, sig)
    agree &= not ed25519_verify.verify(raw_public(priv), msg + b"!", sig)
case("it agrees with cryptography on random keys", agree)
KEY = Ed25519PrivateKey.generate()
OTHER = Ed25519PrivateKey.generate()
PUB = b64(raw_public(KEY))
sig_ok = b64(KEY.sign(b"hello"))
case("verify_signature: both back ends accept a good one and refuse a bad one",
     volunteer.verify_signature(PUB, b"hello", sig_ok)
     and volunteer.verify_signature(PUB, b"hello", sig_ok, force_pure=True)
     and not volunteer.verify_signature(PUB, b"hellO", sig_ok)
     and not volunteer.verify_signature(PUB, b"hellO", sig_ok, force_pure=True)
     and not volunteer.verify_signature(PUB, b"hello", "not base64!!"))
case("the shipped key is a real one, and the placeholder counts as unconfigured",
     volunteer.MANIFEST_PUBLIC_KEY != volunteer.PLACEHOLDER_KEY
     and volunteer.key_configured(volunteer.MANIFEST_PUBLIC_KEY)
     and not volunteer.key_configured(volunteer.PLACEHOLDER_KEY)
     and volunteer.key_configured(PUB))
try:
    Ed25519PublicKey.from_public_bytes(base64.b64decode(volunteer.MANIFEST_PUBLIC_KEY))
    shipped_is_a_point = True
except ValueError:
    shipped_is_a_point = False
case("... and it is a valid Ed25519 public key, not just 32 bytes", shipped_is_a_point)

# ---------------------------------------------------------------- the fakes
FAKE_WORKER = textwrap.dedent('''\
    #!%(python)s
    import json, os, sys, tomllib
    from pathlib import Path
    with open(os.environ["FAKE_WORKER_LOG"], "a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\\n")
    argv = sys.argv[1:]
    if argv[:1] == ["enrol"]:
        # Like the real one: keeps a scratch folder and cap already in the file,
        # and otherwise writes its own (a folder beside the config, a cap it picks).
        d = Path(argv[argv.index("--dir") + 1]); d.mkdir(parents=True, exist_ok=True)
        f = d / "collab-worker.toml"
        old = tomllib.loads(f.read_text()) if f.exists() else {}
        scratch = old.get("scratch_dir") or str(d.parent / "scratch")
        cap = old.get("scratch_cap_gb") or 40
        f.write_text("hub_url = '%%s'\\nscratch_dir = '%%s'\\nscratch_cap_gb = %%d\\n\\n[x]\\nk = 1\\n"
                     %% (argv[1], scratch, cap))
    if argv[:2] == ["service", "install"]:
        # The real one runs `config check` first, which requires both scratch keys.
        f = Path(argv[argv.index("--dir") + 1]) / "collab-worker.toml"
        missing = [k for k in ("scratch_dir", "scratch_cap_gb") if k not in tomllib.loads(f.read_text())]
        if missing:
            print("collab-worker config check failed: MISSING " + " ".join(missing))
            sys.exit(1)
    if argv[:2] == ["service", "status"]:
        print("running (fake)")
    if os.environ.get("FAKE_FAIL_START") and argv[:2] == ["service", "start"]:
        sys.exit(1)
''') % {"python": sys.executable}


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members:
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o755 << 16
            zf.writestr(info, data)
    return buf.getvalue()


def tar_bytes(members, links=()):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
        for name, target in links:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return buf.getvalue()


class Fake:
    """A manifest server: files by path, a request log, signing on demand."""

    def __init__(self):
        self.files = {}
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                data = fake.files.get(self.path)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def publish(self, manifest_path, version, archive, product="collab-worker", key=KEY,
                os_key="linux-x64", sha=None, size=None, sign_body=None, contract="1.27.0",
                relative=True):
        name = f"{product}-{version}-{os_key}.tar.gz"
        file_path = f"/files/{name}"
        self.files[file_path] = archive
        manifest = {"version": version, "contract": contract, "assets": {os_key: {"primary": {
            "url": (file_path if relative else self.url + file_path),
            "size": len(archive) if size is None else size,
            "sha256": sha or hashlib.sha256(archive).hexdigest(), "filename": name}}}}
        body = json.dumps(manifest).encode()
        self.files[manifest_path] = body
        self.files[manifest_path + ".sig"] = b64(key.sign(sign_body if sign_body is not None else body)).encode()

    def clear(self):
        self.files.clear()
        self.requests.clear()

    def stop(self):
        self.httpd.shutdown()


fake = Fake()
# The public releases are the last sources tried (Collab's downloads, then the
# GitHub mirror); the checks never leave this machine, so they are the fake
# too, each under its own path.
SHIPPED_RELEASES_URLS = volunteer.RELEASES_URLS
volunteer.RELEASES_URLS = (fake.url + "/releases", fake.url + "/mirror")
HUB_PATH = "/hub/v1/worker-manifest"
SRV_PATH = "/download/collab-worker/latest.json"
HUB_BIN_PATH = "/download/collab-hub/latest.json"


def good_archive(extra=()):
    return tar_bytes([("collab-worker", FAKE_WORKER.encode()), ("README.txt", b"hi")] + list(extra))


def argv_log():
    return [json.loads(x) for x in LOG.read_text().splitlines()] if LOG.exists() else []


def fresh(name, key=PUB):
    """A Volunteer over its own data folder, pretending to be Linux."""
    os.environ["ASTRO_DATA_DIR"] = str(WORK / name)
    (WORK / name).mkdir(parents=True, exist_ok=True)
    cfg = Config(WORK / name / "settings.json")
    cfg.update("collab", {"serverUrl": fake.url})
    LOG.unlink(missing_ok=True)
    fake.clear()
    return cfg, volunteer.Volunteer(cfg, public_key=key, os_key="linux-x64")


def refused(vol, text=None, **kw):
    try:
        vol.install_blocking(code="ABCD-1234", hub_url=fake.url, **kw)
    except volunteer.VolunteerError as exc:
        return text is None or text in str(exc), str(exc)
    return False, "no error"


def untouched(vol):
    return not vol.bin_path().exists() and not argv_log()


# ------------------------------------------------------- the placeholder key
print("-- placeholder key")
cfg, vol = fresh("placeholder", key=volunteer.PLACEHOLDER_KEY)
fake.publish(HUB_PATH, "1.0.0", good_archive())
ok, msg = refused(vol, "no release signing key configured")
case("a build carrying the placeholder key refuses to install", ok, msg)
case("... without asking the server for anything", not fake.requests, str(fake.requests))
case("... and nothing was installed or run", untouched(vol))

# ------------------------------------------------------------ good and bad
print("-- install")
cfg, vol = fresh("good")
fake.publish(SRV_PATH, "1.0.0", good_archive())
fake.publish(HUB_PATH, "1.0.0", good_archive())
vol.config.update("volunteer", {"idlePolicy": "when_idle", "pauseWhileSequencing": True,
                                "workWindow": "08:00-18:00", "scratchCapGb": 50})
vol.install_blocking(code="ABCD-1234", hub_url=fake.url, worker_id="mac-mini")
log = argv_log()
cfgdir = str(vol.config_dir())
case("the hub's manifest was tried before the server's",
     [r for r in fake.requests if r.startswith(("/hub", "/download"))][:2] == [HUB_PATH, HUB_PATH + ".sig"]
     and SRV_PATH not in fake.requests, str(fake.requests))
case("the program is installed under bin/ and is executable",
     vol.bin_path().is_file() and os.access(vol.bin_path(), os.X_OK))
case("the archive was unpacked into <root>/<version>/",
     (vol.root() / "1.0.0" / "collab-worker").is_file())
case("enrol ran with the hub, code, dir and worker id",
     log[:1] == [["enrol", fake.url, "--code", "ABCD-1234", "--dir", cfgdir, "--worker-id", "mac-mini"]], str(log[:1]))
case("service install ran next with --dir", log[1:2] == [["service", "install", "--dir", cfgdir]], str(log))
case("then status was read", log[-1] == ["service", "status", "--dir", cfgdir])
toml = vol.toml_path().read_text()
import tomllib                                                       # noqa: E402
data = tomllib.loads(toml)
case("the worker's TOML carries the busy file and the idle policy",
     data.get("busy_file") == str(vol.busy_file()) and data.get("idle_policy") == "when_idle"
     and data.get("work_window") == "08:00-18:00" and data.get("scratch_cap_gb") == 50, toml)
case("... and the file's own keys and tables survive", data.get("hub_url") == fake.url and data["x"]["k"] == 1)
case("busy file path is <data_root>/collab-worker/busy",
     vol.busy_file() == Path(os.environ["ASTRO_DATA_DIR"]) / "collab-worker" / "busy"
     or vol.busy_file().name == "busy" and vol.busy_file().parent.name == "collab-worker")
case("settings record the version, the hub and the worker",
     cfg.get("volunteer", "installedVersion") == "1.0.0" and cfg.get("volunteer", "hubUrl") == fake.url
     and cfg.get("volunteer", "workerId") == "mac-mini" and cfg.get("volunteer", "enabled") is True)
cfg.update("volunteer", {"pauseWhileSequencing": False})
vol.apply_settings()
case("turning off pause-while-sequencing removes busy_file",
     "busy_file" not in tomllib.loads(vol.toml_path().read_text()))

# pause while N.I.N.A. runs: the worker reads a TOML bool (T206), never a string
cfg.update("volunteer", {"pauseWhileNina": True})
vol.install_blocking(code="ABCD-1234", hub_url=fake.url, worker_id="mac-mini")
data = tomllib.loads(vol.toml_path().read_text())
case("an install with pauseWhileNina on writes pause_while_nina = true",
     "pause_while_nina = true" in vol.toml_path().read_text().splitlines()
     and data.get("pause_while_nina") is True, vol.toml_path().read_text())
cfg.update("volunteer", {"pauseWhileNina": False})
vol.apply_settings()                                    # what the settings save does
data = tomllib.loads(vol.toml_path().read_text())
case("saving with pauseWhileNina off writes pause_while_nina = false",
     "pause_while_nina = false" in vol.toml_path().read_text().splitlines()
     and data.get("pause_while_nina") is False, vol.toml_path().read_text())
cfg.update("volunteer", {"pauseWhileNina": True})
vol.apply_settings()
data = tomllib.loads(vol.toml_path().read_text())
case("saving it back on updates the same key, not a second copy",
     data.get("pause_while_nina") is True and vol.toml_path().read_text().count("pause_while_nina") == 1)

# The panel's defaults are a blank scratch folder and a cap of 0, meaning "the
# worker's own". The desktop used to turn them into removals, deleting the
# scratch_dir and scratch_cap_gb that `enrol` had just written, and the worker's
# config check then refused `service install`: the first real install on the
# home lab's Windows PC (2026-10-28) failed exactly so, after spending its code.
cfg, vol = fresh("scratch-defaults")
fake.publish(SRV_PATH, "1.0.0", good_archive())
cfg.update("volunteer", {"scratchDir": "", "scratchCapGb": 0})
try:
    vol.install_blocking(code="ABCD-1234", hub_url=fake.url)
    installed, why = True, ""
except volunteer.VolunteerError as exc:
    installed, why = False, str(exc)
data = tomllib.loads(vol.toml_path().read_text()) if vol.toml_path().exists() else {}
case("an install with a blank scratch folder and cap keeps the ones enrol wrote, and installs",
     installed and data.get("scratch_dir") == str(vol.config_dir().parent / "scratch")
     and data.get("scratch_cap_gb") == 40
     and ["service", "install", "--dir", str(vol.config_dir())] in argv_log(), why or str(data))
cfg.update("volunteer", {"scratchCapGb": 25})
vol.apply_settings()
case("...and a cap set later replaces the worker's own",
     tomllib.loads(vol.toml_path().read_text()).get("scratch_cap_gb") == 25)

print("-- refusals")
def attempt(name, text, publish, key=PUB, hub=True):
    cfg, v = fresh(name, key)
    publish()
    ok, msg = refused(v, text)
    case(name + " is refused", ok, msg)
    case(name + ": nothing installed, nothing run", untouched(v))
    return cfg, v

good = good_archive()
attempt("a bad signature", "no usable release manifest",
        lambda: fake.publish(HUB_PATH, "1.0.0", good, key=OTHER))
attempt("a signature over other bytes", "signature",
        lambda: fake.publish(HUB_PATH, "1.0.0", good, sign_body=b"{}"))
attempt("a wrong SHA-256", "SHA-256",
        lambda: fake.publish(HUB_PATH, "1.0.0", good, sha="0" * 64))
attempt("a download larger than the manifest says", "larger",
        lambda: fake.publish(HUB_PATH, "1.0.0", good, size=len(good) - 5))
attempt("a download smaller than the manifest says", "manifest says",
        lambda: fake.publish(HUB_PATH, "1.0.0", good, size=len(good) + 5))

cfg, v = fresh("downgrade")
fake.publish(HUB_PATH, "1.0.0", good)
cfg.update("volunteer", {"installedVersion": "2.0.0"})
v.bin_path().parent.mkdir(parents=True, exist_ok=True)
v.bin_path().write_text("old")
ok, msg = refused(v, "downgrade")
case("a downgrade is refused", ok, msg)
case("... and the installed program is untouched", v.bin_path().read_text() == "old" and not argv_log())

for label, archive in [
    ("a zip with ../ in a path", None),
    ("a tar with ../ in a path", tar_bytes([("collab-worker", FAKE_WORKER.encode()), ("../evil", b"x")])),
    ("a tar with an absolute path", tar_bytes([("collab-worker", FAKE_WORKER.encode()), ("/tmp/evil-abs", b"x")])),
    ("a tar with a symlink", tar_bytes([("collab-worker", FAKE_WORKER.encode())], links=[("l", "/etc")])),
]:
    cfg, v = fresh("trav-" + label.split()[1] + str(len(label)))
    if archive is None:
        archive = zip_bytes([("collab-worker", FAKE_WORKER.encode()), ("../evil", b"x")])
        fake.publish(HUB_PATH, "1.0.0", archive, os_key="windows-x64")
        v = volunteer.Volunteer(cfg, public_key=PUB, os_key="windows-x64")
        # a zip on a Linux-shaped test: the suffix decides, so rename the asset
        manifest = json.loads(fake.files[HUB_PATH])
        manifest["assets"]["windows-x64"]["primary"]["filename"] = "collab-worker-1.0.0-windows-x64.zip"
        body = json.dumps(manifest).encode()
        fake.files[HUB_PATH] = body
        fake.files[HUB_PATH + ".sig"] = b64(KEY.sign(body)).encode()
    else:
        fake.publish(HUB_PATH, "1.0.0", archive)
    ok, msg = refused(v, "unsafe path" if "link" not in label else "link or device")
    case(label + " is refused", ok, msg)
    case(label + ": nothing unpacked outside, nothing installed or run",
         untouched(v) and not (Path(v.root()).parent / "evil").exists()
         and not (v.root() / "evil").exists() and not Path("/tmp/evil-abs").exists()
         and not (v.root() / "1.0.0").exists())

cfg, v = fresh("fallback")
fake.publish(HUB_PATH, "1.0.0", good, key=OTHER)
fake.publish(SRV_PATH, "1.0.0", good)
v.install_blocking(code="ABCD-1234", hub_url=fake.url)
case("a hub mirror with a bad signature falls back to the server's copy",
     v.bin_path().is_file() and SRV_PATH in fake.requests)

cfg, v = fresh("nocode")
fake.publish(HUB_PATH, "1.0.0", good)
try:
    v.install_blocking(hub_url=fake.url)
    ok = False
except volunteer.VolunteerError as exc:
    ok = "code" in str(exc)
case("no code and no local hub is refused before any download", ok and not fake.requests)

cfg, v = fresh("absolute")
fake.publish(SRV_PATH, "1.0.0", good, relative=False)
v.install_blocking(code="ABCD-1234", hub_url=fake.url)
case("a hub with no mirror: the server's manifest installs, absolute asset URL too", v.bin_path().is_file())

# ------------------------------------------------------------------- update
print("-- update")
cfg, v = fresh("update")
fake.publish(HUB_PATH, "1.0.0", good)
v.install_blocking(code="ABCD-1234", hub_url=fake.url)
LOG.unlink()
try:
    v.update_blocking()
    ok = False
except volunteer.VolunteerError as exc:
    ok = "already the newest" in str(exc)
case("an equal version is not an update", ok and not argv_log())
new = tar_bytes([("collab-worker", (FAKE_WORKER + "# v2\n").encode())])
fake.publish(HUB_PATH, "1.1.0", new)
v.update_blocking()
verbs = [a[:2] for a in argv_log()]
case("update stops the service, swaps, then starts it",
     verbs[:2] == [["service", "stop"], ["service", "start"]], str(verbs))
case("the new program is in place, the old one kept as .previous, version recorded",
     "# v2" in v.bin_path().read_text() and v.bin_path().with_name("collab-worker.previous").is_file()
     and cfg.get("volunteer", "installedVersion") == "1.1.0")
fake.publish(HUB_PATH, "1.2.0", tar_bytes([("collab-worker", (FAKE_WORKER + "# v3\n").encode())]))
os.environ["FAKE_FAIL_START"] = "1"
try:
    v.update_blocking()
    ok = False
except volunteer.VolunteerError as exc:
    ok = "previous one was put back" in str(exc)
del os.environ["FAKE_FAIL_START"]
case("an update that will not start puts the previous program back",
     ok and "# v3" not in v.bin_path().read_text() and cfg.get("volunteer", "installedVersion") == "1.1.0")
v.uninstall_blocking()
case("uninstall stops and uninstalls the service and forgets the version",
     [a[:2] for a in argv_log()][-2:] == [["service", "stop"], ["service", "uninstall"]]
     and cfg.get("volunteer", "installedVersion") == "" and not v.bin_path().exists())

# ---------------------------------------------------------------------- hub
print("-- hub binary")
cfg, v = fresh("hubinst")
hub_target = WORK / "hubinst" / "hubroot" / "bin" / "collab-hub"
fake.publish(HUB_BIN_PATH, "0.9.0", tar_bytes([("collab-hub", b"#!/bin/sh\nexit 0\n")]), product="collab-hub")
v.install_hub_blocking(hub_target)
case("collab-hub installs at the path given, with its version beside it",
     hub_target.is_file() and os.access(hub_target, os.X_OK)
     and hub_target.with_name("collab-hub.version").read_text().strip() == "0.9.0")
case("the worker was not run for it", not argv_log())
fake.publish(HUB_BIN_PATH, "0.9.0", tar_bytes([("collab-hub", b"x")]), product="collab-hub", key=OTHER)
try:
    v.install_hub_blocking(hub_target)
    ok = False
except volunteer.VolunteerError:
    ok = True
case("a bad signature on the hub manifest is refused and the old hub stays",
     ok and hub_target.read_bytes().startswith(b"#!/bin/sh"))

# ------------------------------------------------------ the public releases
# The signed release is also published for anyone: in Rapid Stacker Collab's
# own downloads, its canonical home, and as a GitHub release per product on
# Starfront's repository, the mirror that carried 0.1.0 first. So a server
# that does not carry it (a home lab's, or a new one's) still lets its
# volunteers install. They are tried last, after the server, in that order,
# under the same signature check as every other source.
print("-- public release")
case("the shipped release addresses are Collab's downloads, then Starfront's GitHub releases",
     SHIPPED_RELEASES_URLS == ("https://collab.astro.ekeservices.com/downloads",
                               "https://github.com/EKEServices/starfront/releases/download"),
     str(SHIPPED_RELEASES_URLS))
cfg, v = fresh("from-release")
fake.publish("/releases/collab-worker/latest.json", "1.0.0", good_archive())
v.install_blocking(code="ABCD-1234", hub_url=fake.url)
tried = [r for r in fake.requests if r.endswith("latest.json")]
case("with nothing on the hub or the server, the worker installs from the public release",
     cfg.get("volunteer", "installedVersion") == "1.0.0" and v.bin_path().is_file(), str(tried))
case("...which is tried after the server, not before it, and the mirror is not needed",
     tried[-2:] == [SRV_PATH, "/releases/collab-worker/latest.json"], str(tried))
fake.clear()
cfg, v = fresh("from-mirror")
fake.publish("/mirror/collab-worker/latest.json", "1.0.0", good_archive())
v.install_blocking(code="ABCD-1234", hub_url=fake.url)
tried = [r for r in fake.requests if r.endswith("latest.json")]
case("with Collab's downloads empty, the GitHub mirror serves the release",
     cfg.get("volunteer", "installedVersion") == "1.0.0"
     and tried[-3:] == [SRV_PATH, "/releases/collab-worker/latest.json", "/mirror/collab-worker/latest.json"],
     str(tried))
cfg, v = fresh("hub-from-release")
hub_target = WORK / "hub-from-release" / "hubroot" / "bin" / "collab-hub"
fake.publish("/releases/collab-hub/latest.json", "0.9.0", tar_bytes([("collab-hub", b"#!/bin/sh\nexit 0\n")]),
             product="collab-hub")
v.install_hub_blocking(hub_target)
case("the hub installs from its own public release when the server has none",
     hub_target.is_file() and hub_target.with_name("collab-hub.version").read_text().strip() == "0.9.0"
     and [r for r in fake.requests if r.endswith("latest.json")] == [HUB_BIN_PATH, "/releases/collab-hub/latest.json"],
     str(fake.requests))
fake.publish("/releases/collab-hub/latest.json", "0.9.1", tar_bytes([("collab-hub", b"x")]), product="collab-hub",
             key=OTHER)
try:
    v.install_hub_blocking(hub_target)
    ok = False
except volunteer.VolunteerError:
    ok = True
case("...and a release signed with another key is refused there too",
     ok and hub_target.read_bytes().startswith(b"#!/bin/sh"))

# ------------------------------------------------------------------- routes
print("-- routes")
os.environ["ASTRO_DATA_DIR"] = str(WORK / "routes")
LOG.unlink(missing_ok=True)
fake.clear()
try:
    from astrocontrol import desktop, main
except Exception as exc:                                                # noqa: BLE001
    case("main.py imports for the route checks", False, repr(exc))
    main = None

if main is not None:
    import urllib.error
    import urllib.request
    main.config.update("collab", {"serverUrl": fake.url})
    main.volunteer_service.worker._public_key = PUB
    main.volunteer_service.hub_installer._public_key = PUB
    main.volunteer_service.worker._os_key = "linux-x64"
    main.volunteer_service.hub_installer._os_key = "linux-x64"
    main.config.update("hub", {"configDir": str(WORK / "routes" / "hubcfg"),
                               "dataRoot": str(WORK / "routes" / "hubdata"), "binary": ""})
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = desktop.ServerThread("127.0.0.1", port)
    server.start()

    class Reply:
        def __init__(self, status, text):
            self.status_code, self.text = status, text

        def json(self):
            return json.loads(self.text)

    def call(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(server.url.rstrip("/") + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return Reply(resp.status, resp.read().decode())
        except urllib.error.HTTPError as exc:
            return Reply(exc.code, exc.read().decode())

    r = call("GET", "/api/volunteer")
    case("GET /api/volunteer works with nothing installed",
         r.status_code == 200 and r.json()["installed"] is False and r.json()["signingKeyConfigured"] is True, r.text[:200])
    snap = main._snapshot()
    case("the live snapshot carries a volunteer summary",
         set(snap["volunteer"]) == {"enabled", "installed", "version", "service", "busy", "phase", "lastError"},
         str(snap.get("volunteer")))
    r = call("POST", "/api/settings/volunteer", {"idlePolicy": "always", "workWindow": "22:00-07:00"})
    case("POST /api/settings/volunteer saves", r.status_code == 200
         and main.config.get("volunteer", "idlePolicy") == "always")
    case("... and refuses an empty or bad body",
         call("POST", "/api/settings/volunteer", {}).status_code == 400
         and call("POST", "/api/settings/volunteer", {"idlePolicy": "never"}).status_code == 422
         and call("POST", "/api/settings/volunteer", {"workerId": "Bad Name"}).status_code == 422)
    fake.publish(SRV_PATH, "1.0.0", good_archive())
    t0 = time.monotonic()
    r = call("POST", "/api/volunteer/install", {"code": "ABCD-1234", "hubUrl": fake.url + "/nohub", "workerId": "route-pc"})
    case("POST /api/volunteer/install answers at once", r.status_code == 200 and time.monotonic() - t0 < 1.5, r.text[:200])
    main.volunteer_service.wait(30)
    r = call("GET", "/api/volunteer")
    case("the install finished and is reported",
         r.json()["progress"]["phase"] == "done" and r.json()["installed"] and r.json()["version"] == "1.0.0",
         r.text[:300])
    case("the server's manifest was used when the hub has none", SRV_PATH in fake.requests)
    r = call("GET", "/api/volunteer/status")
    main.volunteer_service.wait(30)
    case("GET /api/volunteer/status returns and reads the service",
         r.status_code == 200 and call("GET", "/api/volunteer").json()["serviceStatus"]["status"] == "running (fake)")
    for verb in ("stop", "start"):
        r = call("POST", f"/api/volunteer/{verb}")
        main.volunteer_service.wait(30)
        case(f"POST /api/volunteer/{verb} runs service {verb}",
             r.status_code == 200 and [a[:2] for a in argv_log() if a[1:2] == [verb]] == [["service", verb]])
    r = call("POST", "/api/volunteer/update")
    main.volunteer_service.wait(30)
    case("POST /api/volunteer/update with nothing newer ends in a clear error",
         "already the newest" in call("GET", "/api/volunteer").json()["lastError"], r.text[:100])
    fake.publish(HUB_BIN_PATH, "0.9.0", tar_bytes([("collab-hub", b"#!/bin/sh\nexit 0\n")]), product="collab-hub")
    r = call("POST", "/api/hub/install")
    case("POST /api/hub/install answers at once", r.status_code == 200, r.text[:200])
    main.volunteer_service.wait(30)
    target = main.hub_supervisor.binary()
    case("... and installs where the supervisor looks",
         target.is_file() and call("GET", "/api/hub").json()["binaryFound"]
         and call("GET", "/api/hub").json()["install"]["phase"] == "done")
    r = call("POST", "/api/volunteer/uninstall")
    main.volunteer_service.wait(30)
    case("POST /api/volunteer/uninstall removes it",
         r.status_code == 200 and call("GET", "/api/volunteer").json()["installed"] is False)
    server.stop()

fake.stop()
passed = sum(results)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
