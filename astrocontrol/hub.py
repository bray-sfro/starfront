"""Running the collaboration hub from this machine.

`collab-hub` is a separate program (a Rust binary, built in the Rapid Stacker
Collab repository) that stacks a collaboration's frames at home.  This module
is the desktop's half: it writes the hub's configuration from the settings,
checks it, starts the hub, keeps it running, and lets the person see what it
is doing.  It is shaped like `desktop.ServerThread` - a thread that owns a
long-running server and stops it cleanly - except that the server is a child
process rather than uvicorn.

Why each piece exists:

  * **The TOML is written from the settings on every start.**  The settings
    are the one place the person edits, so the file the hub reads can never
    drift from them; and a registration that changes `hub_id` takes effect on
    the next (re)start without anyone editing a file.
  * **`check` runs first and its report is kept.**  A hub pointed at a missing
    catalogue folder or a full disk would otherwise start, look alive, and
    fail on the first frame at two in the morning.  A failed check is not
    retried: the same configuration will fail the same way.
  * **A crash restarts with back-off (1, 2, 4 ... 60 s).**  A hub that dies
    should come back without anybody noticing, but one that dies on start
    must not spin the processor or fill the log, so the wait doubles until it
    has stayed up for a while.
  * **The Mac is kept awake with `caffeinate -w <pid>` while the hub runs.**
    A sleeping Mac is a hub that vanished from its workers mid-stack.  `-w`
    ties the assertion to the hub's pid, so it ends with the hub even if this
    program is killed.
  * **Nothing here blocks an HTTP handler on the subprocess.**  Start, stop
    and restart set flags the supervising thread acts on; `check` runs on its
    own thread; handlers read a snapshot taken under a lock.
  * **The hub's token and grant secret go in `hub-credentials.toml` (0600) in
    the hub's configuration folder, never in `settings.json`**, which is
    backed up, synced and pasted into bug reports.
"""

from __future__ import annotations

import collections
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .collabadmin import CollabError

#: Windows: no console window for a background program (as for ASTAP).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: How many lines of the hub's log the program keeps.
LOG_LINES = 500
#: How often the hub's status route is asked, once it is up.
POLL_SECONDS = 10.0
#: Back-off between restarts after a crash: base, doubling, ceiling (seconds).
BACKOFF_BASE = 1.0
BACKOFF_MAX = 60.0
#: A hub that stayed up this long is not crash-looping: the back-off resets.
STABLE_AFTER = 60.0
#: How long a stopping hub is given to exit before it is killed.
TERMINATE_WAIT = 10.0

CAFFEINATE = "caffeinate"
CONFIG_NAME = "collab-hub.toml"
CREDENTIALS_NAME = "hub-credentials.toml"


def _platform() -> str:
    """`sys.platform`, as a function so a check can pretend to be a Mac."""
    return sys.platform


def _toml_str(value: Any) -> str:
    """A TOML basic string.  JSON's escaping is a subset of it."""
    return json.dumps(str(value), ensure_ascii=True)


def _split_bind(bind: str) -> tuple[str, int]:
    host, _, port = str(bind).rpartition(":")
    try:
        return host or "0.0.0.0", int(port)
    except ValueError:
        return "0.0.0.0", 8790


class HubSupervisor:
    """Owns the `collab-hub` child process on behalf of the program."""

    def __init__(self, config: Any, log: Any = None, *,
                 poll_seconds: float = POLL_SECONDS,
                 backoff_base: float = BACKOFF_BASE,
                 backoff_max: float = BACKOFF_MAX,
                 stable_after: float = STABLE_AFTER,
                 terminate_wait: float = TERMINATE_WAIT) -> None:
        self.config = config
        self._log_to = log
        self.poll_seconds = poll_seconds
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.stable_after = stable_after
        self.terminate_wait = terminate_wait

        self._lock = threading.RLock()
        self._lines: collections.deque[dict[str, Any]] = collections.deque(maxlen=LOG_LINES)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._caffeinate: subprocess.Popen | None = None
        self._checking = False

        self._state = "stopped"
        self._last_error = ""
        self._started_at: float | None = None
        self._restarts = 0
        self._next_restart_at: float | None = None
        self._delays: list[float] = []
        self._check: dict[str, Any] | None = None
        self._hub_status: dict[str, Any] | None = None
        self._status_at: float | None = None

    # -- settings ----------------------------------------------------------
    def _setting(self, key: str, default: Any = "") -> Any:
        return self.config.get("hub", key, default)

    def config_dir(self) -> Path:
        chosen = str(self._setting("configDir") or "").strip()
        if chosen:
            return Path(chosen)
        from .config import data_root
        return data_root() / "collab-hub"

    def hub_data_root(self) -> Path:
        chosen = str(self._setting("dataRoot") or "").strip()
        return Path(chosen) if chosen else self.config_dir() / "data"

    def config_path(self) -> Path:
        return self.config_dir() / CONFIG_NAME

    def credentials_path(self) -> Path:
        return self.config_dir() / CREDENTIALS_NAME

    def binary(self) -> Path:
        chosen = str(self._setting("binary") or "").strip()
        if chosen:
            return Path(chosen)
        name = "collab-hub.exe" if os.name == "nt" else "collab-hub"
        return self.hub_data_root() / "bin" / name

    def host_port(self) -> tuple[str, int]:
        return _split_bind(str(self._setting("bind") or "0.0.0.0:8790"))

    def local_url(self) -> str:
        """Where this program reaches its own hub: always loopback."""
        host, port = self.host_port()
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    def public_url(self) -> str:
        """What other machines are told: the setting, else a guess from the host name."""
        chosen = str(self._setting("publicUrl") or "").strip().rstrip("/")
        if chosen:
            return chosen
        _, port = self.host_port()
        return f"http://{socket.gethostname()}:{port}"

    def server_url(self) -> str:
        from .collabclient import server_url
        return server_url(self.config)

    # -- the hub's configuration -------------------------------------------
    def render_toml(self) -> str:
        lines = [f"bind = {_toml_str(self._setting('bind') or '0.0.0.0:8790')}"]
        public = str(self._setting("publicUrl") or "").strip()
        if public:
            lines.append(f"public_url = {_toml_str(public)}")
        lines.append(f"data_root = {_toml_str(self.hub_data_root())}")
        catalogue = str(self._setting("catalogueDir") or "").strip()
        if catalogue:
            lines.append(f"xpsd_dir = {_toml_str(catalogue)}")
            # Enrolment hands each worker the path for its own OS. A worker on
            # another OS reaches the folder through a share, so its path is a
            # setting of its own; this machine's OS uses the hub's own folder.
            here = {"darwin": "macos", "win32": "windows"}.get(_platform(), "linux")
            paths = {"macos": str(self._setting("catalogueMacos") or "").strip(),
                     "windows": str(self._setting("catalogueWindows") or "").strip(),
                     "linux": str(self._setting("catalogueLinux") or "").strip()}
            paths[here] = paths[here] or catalogue
            lines += ["", "[xpsd_paths]"] + [f"{key} = {_toml_str(path)}" for key, path in paths.items() if path]
        hub_id = str(self._setting("registeredId") or "").strip()
        if hub_id:
            lines += ["", "[starfront]", f"url = {_toml_str(self.server_url())}",
                      f"hub_id = {_toml_str(hub_id)}"]
        # TLS stays off: the bearer token is the secret and a LAN's raw subs
        # are not confidential (decision D006).  No [tls] table is written.
        return "\n".join(lines) + "\n"

    def write_config(self) -> Path:
        path = self.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.hub_data_root().mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(self.render_toml(), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def _write_credentials(self, hub: dict[str, Any]) -> None:
        """The hub token and grant secret, mode 0600 from the moment of creation."""
        path = self.credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exactly the keys the hub reads (RSC hub `starfront/` credentials): it refuses unknown
        # keys, and the server address belongs in collab-hub.toml's [starfront] url.
        text = (f"hub_id = {_toml_str(hub.get('hubId', ''))}\n"
                f"hub_token = {_toml_str(hub.get('hubToken', ''))}\n"
                f"grant_secret = {_toml_str(hub.get('grantSecret', ''))}\n")
        tmp = path.with_suffix(".toml.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    # -- reporting ---------------------------------------------------------
    def _say(self, message: str, level: str = "info") -> None:
        self._push({"source": "supervisor", "level": level, "message": message})
        if self._log_to:
            try:
                self._log_to(f"Hub: {message}", level if level in ("warn", "error", "success") else "info")
            except Exception:                      # noqa: BLE001 - logging must not stop the hub
                pass

    def _push(self, entry: dict[str, Any]) -> None:
        entry.setdefault("at", time.time())
        with self._lock:
            self._lines.append(entry)

    def _ingest(self, raw: str) -> None:
        """One line of the hub's output, structured where it is JSON."""
        raw = raw.rstrip("\r\n")
        if not raw:
            return
        entry: dict[str, Any] = {"source": "hub", "level": "info", "message": raw, "raw": raw}
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            fields = parsed.get("fields") if isinstance(parsed.get("fields"), dict) else {}
            entry["level"] = str(parsed.get("level") or parsed.get("severity") or "info").lower()
            entry["message"] = str(parsed.get("message") or fields.get("message")
                                   or parsed.get("msg") or raw)
        self._push(entry)

    def log_lines(self, count: int = LOG_LINES) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._lines)[-max(1, count):]

    def _set(self, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self, f"_{key}", value)

    # -- the check ---------------------------------------------------------
    def run_check(self) -> dict[str, Any]:
        """`collab-hub check`, with its report.  Blocks: call it off the HTTP thread."""
        binary = self.binary()
        result: dict[str, Any] = {"ok": False, "exit": None, "report": "",
                                  "at": time.time(), "binary": str(binary)}
        try:
            path = self.write_config()
            done = subprocess.run(
                [str(binary), "check", "--config", str(path)],
                capture_output=True, text=True, timeout=120,
                creationflags=_NO_WINDOW)
            result.update(ok=done.returncode == 0, exit=done.returncode,
                          report=(done.stdout + done.stderr).strip())
        except FileNotFoundError:
            result["report"] = (f"The hub program was not found at {binary}. "
                                "Install it, or set its location in the hub settings.")
        except subprocess.TimeoutExpired:
            result["report"] = "The hub's check did not finish within two minutes."
        except OSError as exc:
            result["report"] = f"The hub's check could not be run: {exc}"
        with self._lock:
            self._check = result
        return result

    def check_async(self) -> bool:
        """Run the check on its own thread; False if one is already running."""
        with self._lock:
            if self._checking:
                return False
            self._checking = True

        def work() -> None:
            try:
                self.run_check()
            finally:
                with self._lock:
                    self._checking = False

        threading.Thread(target=work, daemon=True, name="hub-check").start()
        return True

    # -- starting and stopping ---------------------------------------------
    def start(self) -> dict[str, Any]:
        """Ask for the hub to be running.  Returns at once."""
        with self._lock:
            if self._thread and self._thread.is_alive() and not self._stop.is_set():
                return self.summary()
            previous = self._thread
            stop = threading.Event()
            self._stop = stop
            self._restart.clear()
            self._last_error = ""
            self._state = "checking"
            thread = threading.Thread(target=self._supervise, args=(stop, previous),
                                      daemon=True, name="collab-hub")
            self._thread = thread
        thread.start()
        return self.summary()

    def request_stop(self) -> dict[str, Any]:
        """Ask for the hub to stop.  Returns at once; the thread does the work."""
        with self._lock:
            self._stop.set()
            if self._state not in ("stopped", "failed"):
                self._state = "stopping"
        return self.summary()

    def restart(self) -> None:
        """Replace the running hub (new configuration) without counting a crash."""
        self._restart.set()

    def shutdown(self) -> None:
        """Program exit: stop the hub and wait for it to be gone."""
        self.request_stop()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=self.terminate_wait + 5)
        self._end_child(self._proc)

    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive() and not self._stop.is_set())

    def delay_for(self, attempt: int) -> float:
        """The wait before restart number `attempt` (0-based): 1, 2, 4 ... 60."""
        return min(self.backoff_max, self.backoff_base * (2 ** attempt))

    def start_if_enabled(self) -> None:
        if self._setting("enabled", False) and self._setting("autoStart", True):
            self.start()

    # -- the supervising thread --------------------------------------------
    def _supervise(self, stop: threading.Event,
                   previous: threading.Thread | None) -> None:
        if previous and previous.is_alive():
            previous.join(timeout=self.terminate_wait + 5)
        try:
            self._supervise_loop(stop)
        except Exception as exc:                   # noqa: BLE001 - reported, never raised
            self._say(f"the supervisor failed: {exc}", "error")
            self._set(state="failed", last_error=str(exc))
        finally:
            with self._lock:
                if self._state != "failed":
                    self._state = "stopped"
                self._hub_status = None
                self._started_at = None
                self._next_restart_at = None

    def _supervise_loop(self, stop: threading.Event) -> None:
        self._set(state="checking")
        report = self.run_check()
        if stop.is_set():
            return
        if not report["ok"]:
            first = next((ln for ln in report["report"].splitlines() if ln.strip()), "")
            self._say("the check failed; the hub was not started"
                      + (f": {first}" if first else ""), "error")
            self._set(state="failed", last_error=first or "the check failed")
            return
        attempt = 0
        while not stop.is_set():
            proc = self._spawn()
            if proc is None:
                self._set(state="failed")
                return
            started = time.monotonic()
            outcome = self._watch(proc, stop)
            self._end_child(proc)
            if outcome == "stopped":
                return
            if outcome == "restart":
                attempt = 0
                self._say("restarting with the new configuration")
                continue
            code = proc.returncode
            if time.monotonic() - started >= self.stable_after:
                attempt = 0
            delay = self.delay_for(attempt)
            attempt += 1
            with self._lock:
                self._restarts += 1
                self._delays = (self._delays + [delay])[-20:]
                self._state = "backoff"
                self._last_error = f"the hub exited with code {code}"
                self._hub_status = None
                self._next_restart_at = time.time() + delay
            self._say(f"the hub exited with code {code}; restarting in {delay:g} s", "warn")
            if stop.wait(delay):
                return

    def _spawn(self) -> subprocess.Popen | None:
        binary = self.binary()
        try:
            path = self.write_config()
            proc = subprocess.Popen(
                [str(binary), "run", "--log-format", "json", "--config", str(path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
        except OSError as exc:
            self._say(f"the hub could not be started: {exc}", "error")
            self._set(last_error=f"the hub could not be started: {exc}")
            return None
        threading.Thread(target=self._read, args=(proc,), daemon=True,
                         name="collab-hub-log").start()
        with self._lock:
            self._proc = proc
            self._state = "starting"
            self._started_at = time.time()
            self._next_restart_at = None
        self._say(f"started {binary.name} (pid {proc.pid}) on {self.host_port()[1]}", "success")
        self._keep_awake(proc.pid)
        return proc

    def _read(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:               # type: ignore[union-attr]
                self._ingest(line)
        except (OSError, ValueError):
            pass

    def _keep_awake(self, pid: int) -> None:
        if _platform() != "darwin":
            return
        try:
            self._caffeinate = subprocess.Popen(
                [CAFFEINATE, "-w", str(pid)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self._say(f"could not keep the Mac awake: {exc}", "warn")

    def _watch(self, proc: subprocess.Popen, stop: threading.Event) -> str:
        next_poll = 0.0
        while True:
            if stop.is_set():
                return "stopped"
            if self._restart.is_set():
                self._restart.clear()
                return "restart"
            if proc.poll() is not None:
                return "crashed"
            now = time.monotonic()
            if now >= next_poll:
                ok = self._poll_status()
                # Quickly while it is coming up, then at the usual pace.
                next_poll = now + (self.poll_seconds if ok else min(0.5, self.poll_seconds))
            stop.wait(0.1)

    def _end_child(self, proc: subprocess.Popen | None) -> None:
        """terminate, wait, kill: the hub, then the caffeinate that watches it."""
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=self.terminate_wait)
            except subprocess.TimeoutExpired:
                self._say("the hub did not stop in time and was killed", "warn")
                proc.kill()
                proc.wait()
            except OSError:
                pass
        caff = self._caffeinate
        if caff is not None:
            self._caffeinate = None
            if caff.poll() is None:
                try:
                    caff.terminate()
                    caff.wait(timeout=3)
                except (subprocess.TimeoutExpired, OSError):
                    caff.kill()
        with self._lock:
            if self._proc is proc:
                self._proc = None

    # -- asking the hub ----------------------------------------------------
    def _hub_call(self, method: str, path: str, body: dict[str, Any] | None = None,
                  token: str = "", timeout: float = 3.0) -> dict[str, Any]:
        headers = {"User-Agent": "Starfront", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(f"{self.local_url()}{path}", data=data,
                                         method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise CollabError(detail or f"the hub answered {exc.code}", exc.code) from exc
        except Exception as exc:                   # noqa: BLE001 - a hub that is down is normal
            raise CollabError(f"the hub could not be reached: {exc}") from exc
        return json.loads(text) if text else {}

    def _poll_status(self) -> bool:
        try:
            status = self._hub_call("GET", "/api/hub/v1/status",
                                    token=str(self._setting("adminToken") or ""))
        except CollabError as exc:
            with self._lock:
                if self._state == "running":
                    self._state = "starting"       # alive but not answering
                    self._last_error = str(exc)
            return False
        with self._lock:
            self._hub_status = status
            self._status_at = time.time()
            if self._state in ("starting", "running"):
                if self._state == "starting":
                    self._last_error = ""          # it is up: the old fault is history
                self._state = "running"
        return True

    @staticmethod
    def _workers(status: dict[str, Any] | None) -> int | None:
        """The worker count, in whichever shape the hub reports it."""
        if not status:
            return None
        value = status.get("workers")
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            for key in ("online", "total", "count"):
                if isinstance(value.get(key), int):
                    return int(value[key])
        return None

    # -- what the tab reads --------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """The small picture for the live snapshot.  Never blocks."""
        with self._lock:
            return {
                "enabled": bool(self._setting("enabled", False)),
                "state": self._state,
                "url": self.public_url() if self._setting("publicUrl") else self.local_url(),
                "workers": self._workers(self._hub_status),
                "lastError": self._last_error,
                "registered": bool(self._setting("registeredId")),
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            binary = self.binary()
            return {
                **self.summary(),
                "pid": self._proc.pid if self._proc else None,
                "startedAt": self._started_at,
                "restarts": self._restarts,
                "backoff": list(self._delays),
                "nextRestartAt": self._next_restart_at,
                "check": dict(self._check) if self._check else None,
                "checking": self._checking,
                "hubStatus": self._hub_status,
                "hubStatusAt": self._status_at,
                "binary": str(binary),
                "binaryFound": binary.is_file(),
                "configPath": str(self.config_path()),
                "bind": str(self._setting("bind") or ""),
                "publicUrl": str(self._setting("publicUrl") or ""),
                "server": self.server_url(),
                "registeredId": str(self._setting("registeredId") or ""),
                "registeredName": str(self._setting("registeredName") or ""),
                "hasAdminToken": bool(self._setting("adminToken")),
                "keepingAwake": self._caffeinate is not None,
            }

    # -- the Starfront server and the hub's admin surface ---------------------
    def register(self, name: str, force: bool = False) -> dict[str, Any]:
        """Register this hub with the Starfront server as the signed-in person.

        The token and grant secret come back once, so they are written to the
        credentials file before anything else can fail.  Only the id and name
        go into the settings.  Blocks on the network: call it from a handler
        that runs in the thread pool.
        """
        name = (name or "").strip()
        if not name:
            raise CollabError("give the hub a name")
        if self._setting("registeredId") and not force:
            raise CollabError("this hub is already registered; register again "
                              "only to replace the registration")
        token = str(self.config.get("collab", "userToken", "") or "").strip()
        if not token:
            raise CollabError("sign in to the collaboration server first", 401)
        body = json.dumps({"name": name, "lanUrl": self.public_url()}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.server_url()}/api/v1/hubs", data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "Starfront",
                     "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                answer = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            raise CollabError(str(detail), exc.code) from exc
        except Exception as exc:                   # noqa: BLE001 - offline is normal
            raise CollabError(f"{self.server_url()} could not be reached: {exc}") from exc
        if not answer.get("hubId") or not answer.get("hubToken"):
            raise CollabError("the server's answer did not carry a hub id and token")
        self._write_credentials(answer)
        self.config.update("hub", {"registeredId": answer["hubId"], "registeredName": name})
        self.write_config()
        self._say(f"registered with {self.server_url()} as {name}", "success")
        if self.running():
            self.restart()                         # so hub_id reaches the running hub
        return {"hubId": answer["hubId"], "name": name}

    def mint_admin_token(self) -> str:
        """`collab-hub token --admin`, which prints the token once; keep it."""
        done = subprocess.run(
            [str(self.binary()), "token", "--admin", "--config", str(self.write_config())],
            capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
        if done.returncode != 0:
            raise CollabError("the hub would not make an admin token: "
                              + (done.stderr or done.stdout).strip()[:300])
        words = done.stdout.split()
        token = next((w for w in reversed(words) if w.startswith("rscu_")), "")
        if not token:
            raise CollabError("the hub did not print an admin token")
        self.config.update("hub", {"adminToken": token})
        return token

    def new_enrol_code(self, kind: str = "worker", note: str = "",
                       ttl_s: int = 900) -> dict[str, Any]:
        """A one-time code a worker types to join this hub, from the hub's admin surface."""
        if self.summary()["state"] != "running":
            raise CollabError("the hub is not running")
        token = str(self._setting("adminToken") or "").strip()
        if not token:
            token = self.mint_admin_token()
        answer = self._hub_call("POST", "/api/hub/v1/enrol-codes",
                                {"kind": kind, "note": note, "ttl_s": int(ttl_s)},
                                token=token, timeout=15)
        return {"code": answer.get("code", ""), "expires_at": answer.get("expires_at")}
