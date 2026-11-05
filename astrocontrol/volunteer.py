"""Volunteering this machine for live stacking: install a verified worker.

`collab-worker` (and `collab-hub`) are separate programs built in the Rapid
Stacker Collab repository.  This module is the desktop's half of putting one
on this machine: it fetches the signed release manifest, checks it, downloads
the right archive, checks that, unpacks it safely, enrols the worker with a
hub and installs it as a background service.  The point of the whole module
is one rule: **a binary that has not passed every check is never run.**

Why each piece exists:

  * **The manifest is signed, not just served over HTTP.**  `latest.json` is
    rapid-stacker's shape `{version, contract, assets: {<os>: {primary:
    {url, size, sha256, filename}}}}`; `latest.json.sig` holds the base64 of
    a raw 64-byte Ed25519 signature over the *exact bytes* of `latest.json`.
    The public key is embedded below (`MANIFEST_PUBLIC_KEY`).  A hub on the
    LAN or a mirror on the internet can therefore serve it without being
    trusted.  The SHA-256 and size in the signed manifest then bind the
    archive.  A build carrying the placeholder key instead refuses every
    install ("no release signing key configured").
  * **Where it is fetched from, in order:** the `volunteer.manifestUrl`
    setting if set; the hub's mirror `{hubUrl}/hub/v1/worker-manifest`; the
    Starfront server `{serverUrl}/download/collab-worker/latest.json`; and
    last the public releases (`RELEASES_URLS`: Rapid Stacker Collab's own
    downloads, then the GitHub release mirror), so a server that does not
    carry the release, a lab's say, still lets its volunteers install.  The
    signature file is always the manifest URL plus `.sig` (an assumption for
    the hub route, which the hub must honour).  The first source whose
    signature verifies is used; one that fails is skipped and reported, so a
    lagging or tampered mirror never blocks an install the server can serve.
    File URLs in the manifest may be relative to the manifest's URL.
  * **One binary per manifest.**  The plan's manifest shape names a single
    asset per OS, so `collab-hub` has its own manifest, beside the worker's:
    `{serverUrl}/download/collab-hub/latest.json` (+ `.sig`), same key, same
    checks.  A hub does not mirror its own binary, so the server is tried,
    then the public releases (`RELEASES_URLS`).  `POST /api/hub/install` installs it at `HubSupervisor.binary()`,
    so the supervisor keeps pointing at the installed path; the installed
    version is kept in a `<binary>.version` file beside it.
  * **Refusals before any download:** a version that is not newer than the
    one installed (an update; an install of the same version reuses the
    binary already there), anything older than the installed (a downgrade,
    always), and a `contract` below the hub's `min_contract` when the hub
    reports one (`GET {hub}/api/hub/v1/status`, best effort).
  * **The archive is bound to the manifest:** it is streamed to a temporary
    file, stopping if it outgrows the signed size, then the size and SHA-256
    are checked.  Unpacking (zip on Windows and macOS, tar.gz on Linux, by the
    file name's suffix) refuses absolute paths, `..`, links and devices, and
    goes into `<name>.partial` that is renamed into place only when whole.
  * **The binary is swapped in, not overwritten.**  The running copy lives at
    `<data_root>/collab-worker/bin/collab-worker[.exe]`; the service points
    at it.  An update stops the service, copies the new binary to a
    temporary name beside it, renames the old to `.previous`, renames the new
    into place and starts the service; if the start fails the old one comes
    back.  (`collab-worker self-update` can also do this by itself.)
  * **Settings reach the worker through its TOML.**  The CLI's `enrol` has no
    flags for them, so after `enrol` this module sets `busy_file`,
    `idle_policy`, `work_window`, `scratch_dir`, `scratch_cap_gb` and
    `pause_while_nina` (a TOML bool) in `collab-worker.toml` in the config dir
    (`<data_root>/collab-worker/config` - the key is the variable's name
    without `COLLAB_`, lower-cased).  A blank scratch folder or a cap of 0
    leaves the values `enrol` proposed in place; the worker requires both.
    `busy_file` is
    `<data_root>/collab-worker/busy` (touched by the sequencer) only while
    "pause while sequencing" is on.  `pause_while_nina` is the worker's
    "pause while N.I.N.A. runs" gate (T206).
  * **Nothing blocks a handler.**  Install, update, uninstall, start, stop
    and status run on one worker thread each; progress is a small state dict
    the routes and the live snapshot read under a lock.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import platform as _platform_mod
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

from .collabadmin import CollabError

#: The release-signing public key: base64 of the raw 32-byte Ed25519 key.
#: The private half was generated offline by the author and never leaves it;
#: the same public key is `RELEASE_PUBLIC_KEY` in the worker's
#: `self_update.rs`, and the two must change together.
#: `PLACEHOLDER_KEY` stays as the value that means "no key": a build carrying
#: it accepts no manifest, whatever its signature.
PLACEHOLDER_KEY = "PLACEHOLDER-release-signing-key-not-generated-yet"
MANIFEST_PUBLIC_KEY = "pjSI2HuDvFSFXjDu9u5FoJi7rT7uw5a1ZXHA6fdSDbo="

#: Where the signed releases are published for anyone, tried in this order,
#: after the server. First Rapid Stacker Collab's own downloads (its S3 bucket
#: behind CloudFront): the programs are Collab's and proprietary, so their
#: home is with their owner, not in this MPL repository, and the address
#: stays put whichever repository Starfront lives in. Then the GitHub release
#: per product on Starfront's repository, which carried 0.1.0 before Collab
#: did and stays as a mirror. Either way `{base}/<product>/latest.json` never
#: moves while its contents do, and the manifests name their archives relative
#: to themselves, so the same signed files serve from every base and from any
#: Starfront server. Trust comes from the signature, not from the host.
#: check_volunteer points both at its fake.
RELEASES_URLS = (
    "https://collab.astro.ekeservices.com/downloads",
    "https://github.com/EKEServices/starfront/releases/download",
)


def release_manifests(product: str) -> list[str]:
    """The public releases' manifest URLs for `collab-worker` or `collab-hub`, in the order tried."""
    return [f"{base}/{product}/latest.json" for base in RELEASES_URLS]

#: The manifest's OS keys.
OS_KEYS = ("macos-arm64", "windows-x64", "linux-x64")
#: Most bytes a manifest may be; it is a few hundred.
MAX_MANIFEST = 256 * 1024
#: Most files an archive may hold (a binary and a few notes).
MAX_ARCHIVE_FILES = 500
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9-]{1,32}$")
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$")

#: Settings the worker's TOML carries, as (toml key, how to take it).
WORKER_KEYS = ("busy_file", "idle_policy", "work_window", "scratch_dir", "scratch_cap_gb",
               "pause_while_nina")


class VolunteerError(CollabError):
    """Something an install refused or could not do; the message is for the person."""


def _platform() -> str:
    """`sys.platform`, as a function so a check can pretend."""
    return sys.platform


# --------------------------------------------------------------- signature
def key_configured(public_key: str) -> bool:
    """True when `public_key` is a real 32-byte key rather than the placeholder."""
    if not public_key or public_key == PLACEHOLDER_KEY:
        return False
    try:
        return len(base64.b64decode(public_key, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


def verify_signature(public_key: str, message: bytes, signature_b64: str,
                     force_pure: bool = False) -> bool:
    """Check a base64 Ed25519 signature; `cryptography` if present, else pure Python."""
    try:
        key = base64.b64decode(public_key, validate=True)
        sig = base64.b64decode(signature_b64.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    if len(key) != 32 or len(sig) != 64:
        return False
    if not force_pure:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError:
            pass
        else:
            try:
                Ed25519PublicKey.from_public_bytes(key).verify(sig, message)
                return True
            except (InvalidSignature, ValueError):
                return False
    from . import ed25519_verify
    return ed25519_verify.verify(key, message, sig)


def parse_version(text: str) -> tuple:
    """A sortable form of a semantic version; a pre-release sorts below its release."""
    match = _SEMVER.match(str(text).strip())
    if not match:
        raise VolunteerError(f"{text!r} is not a version number")
    major, minor, patch, pre = match.groups()
    # (core, 1) for a release, (core, 0, parts) for a pre-release.
    core = (int(major), int(minor), int(patch))
    if not pre:
        return (core, 1, ())
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (core, 0, parts)


# ------------------------------------------------------------------ helpers
def _toml_str(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def _http_get(url: str, limit: int, timeout: float = 20.0) -> bytes:
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise VolunteerError(f"{url} is not an http(s) address")
    request = urllib.request.Request(url, headers={"User-Agent": "Starfront"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise VolunteerError(f"{url} answered {exc.code}") from exc
    except Exception as exc:                       # noqa: BLE001 - offline is normal
        raise VolunteerError(f"{url} could not be reached: {exc}") from exc
    if len(data) > limit:
        raise VolunteerError(f"{url} is larger than expected")
    return data


def safe_extract(archive: Path, dest: Path, kind: str) -> list[str]:
    """Unpack `archive` into the empty directory `dest`, refusing anything unsafe.

    Every member is checked before a single byte is written.  Returns the
    relative paths of the files written.
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()

    def target(name: str) -> Path:
        clean = name.replace("\\", "/")
        if (not clean or clean.startswith("/") or re.match(r"^[A-Za-z]:", clean)
                or "\x00" in clean or any(p == ".." for p in clean.split("/"))):
            raise VolunteerError(f"the archive holds an unsafe path ({name!r}); nothing was unpacked")
        out = (root / clean).resolve()
        if root != out and root not in out.parents:
            raise VolunteerError(f"the archive holds an unsafe path ({name!r}); nothing was unpacked")
        return out

    written: list[str] = []
    if kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ARCHIVE_FILES:
                raise VolunteerError("the archive holds too many files")
            plan = []
            for info in infos:
                kind_bits = stat.S_IFMT(info.external_attr >> 16)
                if kind_bits and kind_bits not in (stat.S_IFREG, stat.S_IFDIR):
                    raise VolunteerError(f"the archive holds a link or device ({info.filename!r})")
                plan.append((info, target(info.filename)))
            for info, out in plan:
                if info.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                written.append(str(out.relative_to(root)))
    else:
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()
            if len(members) > MAX_ARCHIVE_FILES:
                raise VolunteerError("the archive holds too many files")
            plan = []
            for member in members:
                if not (member.isfile() or member.isdir()):
                    raise VolunteerError(f"the archive holds a link or device ({member.name!r})")
                plan.append((member, target(member.name)))
            for member, out in plan:
                if member.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                with open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                written.append(str(out.relative_to(root)))
    return written


class Progress:
    """What an install is doing, readable from any thread."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {"phase": "idle", "message": "", "progress": 0.0,
                                       "error": "", "version": "", "task": "",
                                       "updatedAt": None}
        self._busy = False

    def begin(self, task: str) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._state.update(phase="starting", message="", progress=0.0, error="",
                               version="", task=task, updatedAt=time.time())
            return True

    def set(self, phase: str, message: str = "", progress: float | None = None, **extra: Any) -> None:
        with self._lock:
            self._state.update(phase=phase, message=message, updatedAt=time.time(), **extra)
            if progress is not None:
                self._state["progress"] = progress

    def finish(self, error: str = "") -> None:
        with self._lock:
            self._busy = False
            self._state.update(phase="failed" if error else "done", error=error,
                               updatedAt=time.time(),
                               progress=self._state["progress"] if error else 1.0)

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**self._state, "busy": self._busy}


class Release:
    """A manifest that passed its signature, and the asset chosen for this machine."""

    def __init__(self, source: str, version: str, contract: str, asset: dict[str, Any],
                 os_key: str) -> None:
        self.source = source
        self.version = version
        self.contract = contract
        self.asset = asset
        self.os_key = os_key


class Installer:
    """Fetches, verifies, downloads and unpacks one product (`collab-worker` or `collab-hub`)."""

    def __init__(self, product: str, *, public_key: str | None = None,
                 os_key: str | None = None, say: Callable[..., None] | None = None) -> None:
        self.product = product
        self._public_key = public_key
        self._os_key = os_key
        self._say = say or (lambda *a, **k: None)
        self.progress = Progress()

    # -- configuration ------------------------------------------------------
    @property
    def public_key(self) -> str:
        return self._public_key if self._public_key is not None else MANIFEST_PUBLIC_KEY

    def os_key(self) -> str:
        if self._os_key:
            return self._os_key
        plat = _platform()
        machine = _platform_mod.machine().lower()
        if plat == "darwin" and machine in ("arm64", "aarch64"):
            return "macos-arm64"
        if plat == "win32" and machine in ("amd64", "x86_64"):
            return "windows-x64"
        if plat.startswith("linux") and machine in ("x86_64", "amd64"):
            return "linux-x64"
        raise VolunteerError(f"there is no {self.product} build for this machine ({plat}, {machine})")

    def binary_name(self) -> str:
        return self.product + (".exe" if self.os_key().startswith("windows") else "")

    def require_key(self) -> None:
        if not key_configured(self.public_key):
            raise VolunteerError("no release signing key configured; "
                                 "this build cannot install anything it cannot verify")

    # -- the manifest --------------------------------------------------------
    def fetch_release(self, sources: list[str], min_contract: str = "") -> Release:
        """The first source whose signature verifies; raises if none does."""
        self.require_key()
        problems: list[str] = []
        for url in sources:
            try:
                return self._release_from(url, min_contract)
            except VolunteerError as exc:
                problems.append(f"{url}: {exc}")
                self._say(f"manifest from {url} refused: {exc}", "warn")
        if not problems:
            raise VolunteerError("there is nowhere to fetch the release manifest from")
        raise VolunteerError("no usable release manifest. " + " | ".join(problems))

    def _release_from(self, url: str, min_contract: str) -> Release:
        body = _http_get(url, MAX_MANIFEST)
        sig = _http_get(url + ".sig", 4096).decode("ascii", "replace")
        if not verify_signature(self.public_key, body, sig):
            raise VolunteerError("the signature does not match; the manifest was not trusted")
        try:
            manifest = json.loads(body.decode("utf-8"))
            version = str(manifest["version"])
            parse_version(version)
            contract = str(manifest.get("contract") or "")
            asset = manifest["assets"][self.os_key()]["primary"]
            asset_url = str(asset["url"])
            size = int(asset["size"])
            sha = str(asset["sha256"])
            filename = str(asset.get("filename") or asset_url)
        except (ValueError, KeyError, TypeError) as exc:
            raise VolunteerError(f"the manifest is not in the expected shape ({exc!r})") from exc
        if size <= 0 or not _SHA_RE.match(sha):
            raise VolunteerError("the manifest's size or SHA-256 is not valid")
        if min_contract and contract:
            try:
                low = parse_version(contract) < parse_version(min_contract)
            except VolunteerError:
                low = False
            if low:
                raise VolunteerError(f"this release's contract {contract} is below the "
                                     f"hub's minimum {min_contract}")
        return Release(url, version, contract,
                       {"url": urllib.parse.urljoin(url, asset_url), "size": size,
                        "sha256": sha.lower(), "filename": filename}, self.os_key())

    # -- the archive ---------------------------------------------------------
    def download(self, release: Release, tmp_dir: Path) -> Path:
        """Stream the asset to a temp file; size and SHA-256 must match the manifest."""
        asset = release.asset
        url = asset["url"]
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise VolunteerError(f"{url} is not an http(s) address")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.product + "-", suffix=".download", dir=tmp_dir)
        path = Path(name)
        digest = hashlib.sha256()
        total = 0
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Starfront"})
            with os.fdopen(fd, "wb") as out, urllib.request.urlopen(request, timeout=30) as resp:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > asset["size"]:
                        raise VolunteerError("the download is larger than the manifest says; "
                                             "it was discarded")
                    digest.update(chunk)
                    out.write(chunk)
                    self.progress.set("downloading", f"{total // (1 << 20)} MB", 0.1 + 0.6 * total / asset["size"])
            if total != asset["size"]:
                raise VolunteerError(f"the download is {total} bytes, the manifest says "
                                     f"{asset['size']}; it was discarded")
            if digest.hexdigest() != asset["sha256"]:
                raise VolunteerError("the download's SHA-256 does not match the manifest; "
                                     "it was discarded")
        except VolunteerError:
            path.unlink(missing_ok=True)
            raise
        except Exception as exc:                   # noqa: BLE001
            path.unlink(missing_ok=True)
            raise VolunteerError(f"the download failed: {exc}") from exc
        return path

    def unpack(self, archive: Path, release: Release, dest: Path) -> Path:
        """Unpack into `dest` (replacing it); returns the path of the program inside."""
        name = release.asset["filename"].lower()
        if name.endswith(".zip"):
            kind = "zip"
        elif name.endswith((".tar.gz", ".tgz")):
            kind = "tar"
        else:
            kind = "tar" if release.os_key.startswith("linux") else "zip"
        partial = dest.with_name(dest.name + ".partial")
        shutil.rmtree(partial, ignore_errors=True)
        try:
            safe_extract(archive, partial, kind)
        except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
            shutil.rmtree(partial, ignore_errors=True)
            raise VolunteerError(f"the archive could not be unpacked: {exc}") from exc
        except VolunteerError:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        wanted = self.binary_name()
        found = next((p for p in sorted(partial.rglob(wanted),
                                        key=lambda p: len(p.parts)) if p.is_file()), None)
        if found is None:
            shutil.rmtree(partial, ignore_errors=True)
            raise VolunteerError(f"the archive does not contain {wanted}")
        relative = found.relative_to(partial)
        if not wanted.endswith(".exe"):
            found.chmod(found.stat().st_mode | 0o755)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(partial, dest)
        return dest / relative

    def acquire(self, sources: list[str], work: Path, installed: str = "",
                min_contract: str = "", allow_same: bool = False) -> tuple[Release, Path | None]:
        """Fetch + verify + download + unpack.  Returns the release and the unpacked program.

        The program is None when `allow_same` and the version equals `installed`
        (the caller reuses what is on disk).  Nothing is run here.
        """
        self.progress.set("fetching", "reading the release manifest", 0.02)
        release = self.fetch_release(sources, min_contract)
        self.progress.set("fetching", f"version {release.version}", 0.08, version=release.version)
        if installed:
            have, new = parse_version(installed), parse_version(release.version)
            if new < have:
                raise VolunteerError(f"{release.version} is older than the installed "
                                     f"{installed}; refusing a downgrade")
            if new == have:
                if allow_same:
                    return release, None
                raise VolunteerError(f"{installed} is already the newest version")
        archive = self.download(release, work / "tmp")
        try:
            self.progress.set("unpacking", "", 0.75)
            program = self.unpack(archive, release, work / release.version)
        finally:
            archive.unlink(missing_ok=True)
        return release, program


def swap_binary(new: Path, current: Path) -> None:
    """Replace `current` with `new`: copy to a temp name beside it, then rename.

    The old program is kept as `<name>.previous` for one rollback.  The
    service must be stopped (Windows will not rename a running program).
    """
    current.parent.mkdir(parents=True, exist_ok=True)
    tmp = current.with_name(current.name + ".new")
    shutil.copy2(new, tmp)
    tmp.chmod(tmp.stat().st_mode | 0o755)
    previous = current.with_name(current.name + ".previous")
    if current.exists():
        os.replace(current, previous)
    try:
        os.replace(tmp, current)
    except OSError:
        if previous.exists():
            os.replace(previous, current)
        raise


def restore_previous(current: Path) -> bool:
    previous = current.with_name(current.name + ".previous")
    if previous.exists():
        os.replace(previous, current)
        return True
    return False


def set_toml_keys(path: Path, values: dict[str, Any]) -> None:
    """Set top-level keys in a TOML file, leaving everything else as it is.

    A value of None removes the key.  Top-level keys come before any table, so
    new ones are written at the top of the file.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    kept = []
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith(("#", "[")) else ""
        if key in values:
            continue
        kept.append(line)
    head = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            head.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            head.append(f"{key} = {value}")
        else:
            head.append(f"{key} = {_toml_str(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(head + kept) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Volunteer:
    """This machine's worker: install, update, service control and status."""

    def __init__(self, config: Any, log: Any = None, hub: Any = None, *,
                 public_key: str | None = None, os_key: str | None = None) -> None:
        self.config = config
        self._log_to = log
        self.hub = hub                              # the local HubSupervisor, if any
        self.worker = Installer("collab-worker", public_key=public_key, os_key=os_key,
                                say=self._say)
        self.hub_installer = Installer("collab-hub", public_key=public_key, os_key=os_key,
                                       say=self._say)
        self._lock = threading.RLock()
        self._svc: dict[str, Any] = {"status": "", "at": None, "ok": None}
        self._threads: list[threading.Thread] = []

    # -- settings and paths -------------------------------------------------
    def _setting(self, key: str, default: Any = "") -> Any:
        return self.config.get("volunteer", key, default)

    def root(self) -> Path:
        from .config import data_root
        return data_root() / "collab-worker"

    def bin_path(self) -> Path:
        return self.root() / "bin" / self.worker.binary_name()

    def config_dir(self) -> Path:
        return self.root() / "config"

    def toml_path(self) -> Path:
        return self.config_dir() / "collab-worker.toml"

    def busy_file(self) -> Path:
        """Touched by the sequencer (T504) while a sequence runs."""
        return self.root() / "busy"

    def installed_version(self) -> str:
        return str(self._setting("installedVersion") or "") if self.bin_path().exists() else ""

    def server_url(self) -> str:
        from .collabclient import server_url
        return server_url(self.config)

    def hub_url(self, override: str = "") -> str:
        chosen = (override or str(self._setting("hubUrl") or "")).strip().rstrip("/")
        if chosen:
            return chosen
        if self.hub is not None and self.hub.running():
            return self.hub.local_url()
        return ""

    def sources(self, hub_url: str) -> list[str]:
        out = []
        manifest = str(self._setting("manifestUrl") or "").strip()
        if manifest:
            out.append(manifest)
        if hub_url:
            out.append(f"{hub_url}/hub/v1/worker-manifest")
        out.append(f"{self.server_url()}/download/collab-worker/latest.json")
        out.extend(release_manifests("collab-worker"))
        return out

    def _say(self, message: str, level: str = "info") -> None:
        if self._log_to:
            try:
                self._log_to(f"Volunteer: {message}", level if level in ("warn", "error", "success") else "info")
            except Exception:                      # noqa: BLE001
                pass

    def _min_contract(self, hub_url: str) -> str:
        """The hub's `min_contract`, when it will say; blank when unknown."""
        if not hub_url:
            return ""
        try:
            data = json.loads(_http_get(f"{hub_url}/api/hub/v1/status", 65536, timeout=3.0))
            return str(data.get("min_contract") or "")
        except (VolunteerError, ValueError, AttributeError):
            return ""

    # -- running the worker ---------------------------------------------------
    def _exec(self, args: list[str], timeout: float = 180.0) -> tuple[int, str]:
        """Run the installed worker.  Only ever the program at `bin_path()`.

        That path is written only by `swap_binary` after `Installer.acquire`
        verified and unpacked a release.
        """
        binary = self.bin_path()
        if not binary.is_file():
            raise VolunteerError("the worker is not installed")
        try:
            done = subprocess.run([str(binary), *args], capture_output=True, text=True,
                                  timeout=timeout, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise VolunteerError(f"`{binary.name} {args[0]}` did not finish in time") from exc
        except OSError as exc:
            raise VolunteerError(f"the worker could not be run: {exc}") from exc
        return done.returncode, (done.stdout + done.stderr).strip()

    def _service(self, verb: str) -> str:
        code, out = self._exec(["service", verb, "--dir", str(self.config_dir())])
        if code != 0 and verb != "status":
            raise VolunteerError(f"`service {verb}` failed: {out[:300]}")
        return out

    def apply_settings(self) -> None:
        """Write the settings the worker reads into its TOML (if it exists yet)."""
        path = self.toml_path()
        if not path.exists():
            return
        busy = bool(self._setting("pauseWhileSequencing", True))
        values: dict[str, Any] = {
            "busy_file": str(self.busy_file()) if busy else None,
            "idle_policy": str(self._setting("idlePolicy") or "when_idle"),
            "work_window": str(self._setting("workWindow") or "") or None,
            "pause_while_nina": bool(self._setting("pauseWhileNina", True)),
        }
        # Scratch is required by the worker, and `enrol` writes both keys itself:
        # a folder beside its config and half the free space, up to 200 GB. A
        # blank folder or a cap of 0 here means "the worker's own", so the keys
        # are left alone rather than removed. Removing them (None) was the old
        # behaviour, and every install with the defaults then failed at
        # `service install` with COLLAB_SCRATCH_DIR and _CAP_GB missing.
        scratch = str(self._setting("scratchDir") or "")
        cap = int(self._setting("scratchCapGb", 0) or 0)
        if scratch:
            values["scratch_dir"] = scratch
        if cap > 0:
            values["scratch_cap_gb"] = cap
        set_toml_keys(path, values)
        if busy:
            self.busy_file().parent.mkdir(parents=True, exist_ok=True)

    # -- tasks (each on its own thread) --------------------------------------
    def _run(self, installer: Progress, task: str, fn: Callable[[], None]) -> bool:
        if not installer.begin(task):
            return False

        def work() -> None:
            try:
                fn()
            except VolunteerError as exc:
                self._say(f"{task} failed: {exc}", "error")
                installer.finish(str(exc))
            except Exception as exc:               # noqa: BLE001 - reported, never raised
                self._say(f"{task} failed: {exc}", "error")
                installer.finish(f"{task} failed: {exc}")
            else:
                installer.finish()

        thread = threading.Thread(target=work, daemon=True, name=f"volunteer-{task}")
        self._threads.append(thread)
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        thread.start()
        return True

    def install(self, code: str = "", hub_url: str = "", worker_id: str = "") -> bool:
        """Install (or re-enrol) the worker.  False if something else is running."""
        return self._run(self.worker.progress, "install",
                         lambda: self.install_blocking(code, hub_url, worker_id))

    def update(self) -> bool:
        return self._run(self.worker.progress, "update", self.update_blocking)

    def uninstall(self) -> bool:
        return self._run(self.worker.progress, "uninstall", self.uninstall_blocking)

    def control(self, verb: str) -> bool:
        return self._run(self.worker.progress, verb, lambda: self._control(verb))

    def refresh_status(self) -> bool:
        return self._run(self.worker.progress, "status", self._refresh_status)

    def install_hub(self, binary: Path) -> bool:
        return self._run(self.hub_installer.progress, "install-hub",
                         lambda: self.install_hub_blocking(binary))

    def wait(self, timeout: float = 30.0) -> None:
        for thread in list(self._threads):
            thread.join(timeout)

    # -- the blocking bodies (also what the check calls) ----------------------
    def install_blocking(self, code: str = "", hub_url: str = "", worker_id: str = "") -> None:
        prog = self.worker.progress
        worker = self.worker
        worker.require_key()
        code = (code or "").strip()
        if code and not _CODE_RE.match(code):
            raise VolunteerError("the enrol code looks wrong; it is like ABCD-1234")
        worker_id = (worker_id or str(self._setting("workerId") or "")).strip()
        if worker_id and not re.match(r"^[a-z0-9-]{1,40}$", worker_id):
            raise VolunteerError("the worker name may use lower-case letters, digits and hyphens")
        hub = self.hub_url(hub_url)
        if not hub:
            raise VolunteerError("say which hub to work for (its address), or run a hub here")
        if not code and not (self.hub is not None and self.hub.running()
                             and hub == self.hub.local_url()):
            raise VolunteerError("type the enrol code from the hub")
        installed = self.installed_version()
        release, program = worker.acquire(self.sources(hub), self.root(), installed,
                                          self._min_contract(hub), allow_same=True)
        if program is not None:
            self._swap_in(program, release.version)
        prog.set("enrolling", "joining the hub", 0.85)
        if not code:                                # this machine's own hub: mint one
            code = str(self.hub.new_enrol_code("worker", "this machine")["code"])
            if not code:
                raise VolunteerError("the hub did not give an enrol code")
        args = ["enrol", hub, "--code", code, "--dir", str(self.config_dir())]
        if worker_id:
            args += ["--worker-id", worker_id]
        self.config_dir().mkdir(parents=True, exist_ok=True)
        status, out = self._exec(args)
        if status != 0:
            raise VolunteerError("enrol failed: " + out.replace(code, "****")[:300])
        self.config.update("volunteer", {"enabled": True, "hubUrl": hub,
                                         **({"workerId": worker_id} if worker_id else {})})
        self.apply_settings()
        prog.set("installing", "installing the service", 0.92)
        self._service("install")
        self._refresh_status()
        self._say(f"worker {release.version} installed for {hub}", "success")

    def _swap_in(self, program: Path, version: str) -> None:
        swap_binary(program, self.bin_path())
        self.config.update("volunteer", {"installedVersion": version})

    def update_blocking(self) -> None:
        prog = self.worker.progress
        self.worker.require_key()
        installed = self.installed_version()
        if not installed:
            raise VolunteerError("the worker is not installed yet")
        hub = self.hub_url()
        release, program = self.worker.acquire(self.sources(hub), self.root(), installed,
                                               self._min_contract(hub))
        prog.set("stopping", "stopping the service", 0.8)
        self._service("stop")
        prog.set("swapping", "replacing the program", 0.88)
        swap_binary(program, self.bin_path())
        prog.set("starting", "starting the service", 0.95)
        try:
            self._service("start")
        except VolunteerError:
            if restore_previous(self.bin_path()):
                with_error = "the new version would not start; the previous one was put back"
                try:
                    self._service("start")
                except VolunteerError:
                    pass
                raise VolunteerError(with_error)
            raise
        self.config.update("volunteer", {"installedVersion": release.version})
        self._refresh_status()
        self._say(f"worker updated to {release.version}", "success")

    def uninstall_blocking(self) -> None:
        if self.bin_path().is_file():
            try:
                self._service("stop")
            except VolunteerError:
                pass
            self._service("uninstall")
        self.config.update("volunteer", {"enabled": False, "installedVersion": ""})
        shutil.rmtree(self.root() / "bin", ignore_errors=True)
        self._service_state(status="not installed", ok=None)
        self._say("worker uninstalled", "success")

    def _control(self, verb: str) -> None:
        self._service(verb)
        self._refresh_status()

    def _service_state(self, **values: Any) -> None:
        with self._lock:
            self._svc.update(values, at=time.time())

    def _refresh_status(self) -> None:
        if not self.bin_path().is_file():
            self._service_state(status="not installed", ok=None)
            return
        try:
            out = self._service("status")
            self._service_state(status=out[:500], ok=True)
        except VolunteerError as exc:
            self._service_state(status=str(exc), ok=False)

    def install_hub_blocking(self, binary: Path) -> None:
        inst = self.hub_installer
        inst.require_key()
        version_file = binary.with_name(binary.name + ".version")
        installed = ""
        if binary.exists() and version_file.exists():
            installed = version_file.read_text(encoding="utf-8").strip()
        work = binary.parent.parent / "releases"
        release, program = inst.acquire([f"{self.server_url()}/download/collab-hub/latest.json",
                                         *release_manifests("collab-hub")], work, installed)
        swap_binary(program, binary)
        version_file.write_text(release.version + "\n", encoding="utf-8")
        self._say(f"hub {release.version} installed at {binary}", "success")

    # -- what the tab reads ----------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """The small picture for the live snapshot.  Never blocks."""
        with self._lock:
            service = dict(self._svc)
        prog = self.worker.progress.snapshot()
        return {
            "enabled": bool(self._setting("enabled", False)),
            "installed": bool(self.installed_version()),
            "version": str(self._setting("installedVersion") or ""),
            "service": service.get("status", ""),
            "busy": prog["busy"],
            "phase": prog["phase"],
            "lastError": prog["error"],
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            service = dict(self._svc)
        return {
            **self.summary(),
            "progress": self.worker.progress.snapshot(),
            "hubInstall": self.hub_installer.progress.snapshot(),
            "serviceStatus": service,
            "hubUrl": str(self._setting("hubUrl") or ""),
            "workerId": str(self._setting("workerId") or ""),
            "localHub": bool(self.hub is not None and self.hub.running()),
            "signingKeyConfigured": key_configured(self.worker.public_key),
            "binary": str(self.bin_path()),
            "configDir": str(self.config_dir()),
            "busyFile": str(self.busy_file()),
            "settings": self.config.section("volunteer"),
        }
