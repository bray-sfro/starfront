"""Polling, cards, alerts and commands. No FastAPI here, so it can be tested alone."""
from __future__ import annotations

import collections
import itertools
import math
import threading
import time
from typing import Any

import httpx

# What the page may ask a node to do. (method, path, needs a rig, needs a confirm)
ACTIONS: dict[str, tuple[str, str, bool, bool]] = {
    "sequence.start": ("POST", "/api/sequence/start", False, False),
    "sequence.pause": ("POST", "/api/sequence/pause", False, False),
    "sequence.resume": ("POST", "/api/sequence/resume", False, False),
    "sequence.stop": ("POST", "/api/sequence/stop", False, False),
    "camera.abort": ("POST", "/api/camera/abort", True, False),
    "mount.park": ("POST", "/api/mount/park", False, False),
    # Moving a telescope nobody is looking at: the page asks first, and so
    # does this side - a request without confirm=true is refused.
    "mount.unpark": ("POST", "/api/mount/unpark", False, True),
    "mount.slew": ("POST", "/api/mount/slew", False, True),
}


class CommandError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Node:
    """One Starfront PC, polled on its own thread."""

    def __init__(self, spec: dict[str, Any], fleet: "Fleet") -> None:
        self.spec = spec
        self.name = spec["name"]
        self.fleet = fleet
        self.base = f"{spec.get('scheme', 'http')}://{spec['host']}:{spec['port']}"
        token = spec.get("token") or fleet.settings.get("token") or ""
        self.headers = {"X-Fleet-Token": token} if token else {}
        self.lock = threading.Lock()
        self.status: dict[str, Any] | None = None
        self.images: list[dict[str, Any]] = []
        self.info: dict[str, Any] = {}
        self.last_ok: float | None = None          # wall clock
        self.last_try: float | None = None
        self.last_error: str | None = None
        self.failures = 0
        self.latency: float | None = None
        self.polls = 0
        self.down_since: float | None = time.time()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- polling -----------------------------------------------------------
    def client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(base_url=self.base, headers=self.headers,
                            timeout=timeout or float(self.fleet.settings["timeoutSeconds"]))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"fleet-{self.name}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        poll = float(self.fleet.settings["pollSeconds"])
        with self.client() as client:
            while not self._stop.is_set():
                started = time.monotonic()
                self.poll_once(client)
                self._stop.wait(max(0.2, poll - (time.monotonic() - started)))

    def poll_once(self, client: httpx.Client) -> None:
        self.last_try = time.time()
        started = time.monotonic()
        try:
            response = client.get("/api/status")
            if response.status_code == 403:
                raise RuntimeError("refused: wrong or missing fleet token "
                                   "(server.fleetToken on that PC)")
            response.raise_for_status()
            status = response.json()
            images = None
            if self.polls % 3 == 0:
                with _quiet():
                    images = client.get("/api/images", params={"limit": 40}).json().get(
                        "images") or []
            if self.polls % 15 == 0:
                with _quiet():
                    reply = client.get("/api/fleet/node")
                    if reply.status_code == 200:
                        self.info = reply.json()
        except Exception as exc:                  # noqa: BLE001 - shown on the card
            with self.lock:
                self.failures += 1
                self.last_error = _describe(exc)
                if self.down_since is None:
                    self.down_since = time.time()
            return
        finally:
            self.polls += 1
        with self.lock:
            self.status = status
            if images is not None:
                self.images = images
            self.last_ok = time.time()
            self.latency = round(time.monotonic() - started, 3)
            self.failures = 0
            self.last_error = None
            self.down_since = None
        self.fleet.node_polled(self)

    # -- state -------------------------------------------------------------
    def freshness(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        settings = self.fleet.settings
        with self.lock:
            age = None if self.last_ok is None else now - self.last_ok
            error = self.last_error
            down = self.down_since
        if age is None:
            state = "offline" if (self.last_try and error) else "connecting"
        elif age <= float(settings["staleSeconds"]):
            state = "live"
        elif age <= float(settings["offlineSeconds"]):
            state = "stale"
        else:
            state = "offline"
        return {"state": state, "age": None if age is None else round(age, 1),
                "error": error, "downSince": down, "latency": self.latency}

    # -- commands ----------------------------------------------------------
    def send(self, method: str, path: str, params: dict[str, Any] | None = None,
             body: dict[str, Any] | None = None, timeout: float = 10.0) -> httpx.Response:
        with self.client(timeout) as client:
            return client.request(method, path, params=params or None, json=body)


class _quiet:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "no answer in time"
    if isinstance(exc, httpx.ConnectError):
        return "cannot connect (is Starfront running with LAN access on?)"
    return str(exc) or type(exc).__name__


def _num(value: Any, digits: int = 2) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else round(number, digits)


class Fleet:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.nodes: dict[str, Node] = {n["name"]: Node(n, self) for n in settings["nodes"]}
        self.lock = threading.Lock()
        self.commands: collections.OrderedDict[int, dict[str, Any]] = collections.OrderedDict()
        self._ids = itertools.count(1)
        self.alert_first_seen: dict[str, float] = {}

    def start(self) -> None:
        for node in self.nodes.values():
            node.start()

    def stop(self) -> None:
        for node in self.nodes.values():
            node.stop()

    # -- aggregation ---------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        nodes = []
        cards = []
        for node in self.nodes.values():
            fresh = node.freshness(now)
            with node.lock:
                status = node.status
                images = list(node.images)
                info = dict(node.info)
            entry = {"name": node.name, "host": node.spec["host"],
                     "port": node.spec["port"], "note": node.spec.get("note", ""),
                     **fresh, "version": info.get("version"), "rigs": []}
            if status:
                seq = status.get("sequence") or {}
                entry["sequence"] = {
                    "running": bool(seq.get("running")), "paused": bool(seq.get("paused")),
                    "state": seq.get("state"), "message": seq.get("message"),
                    "error": seq.get("error"), "watchdog": seq.get("watchdog") or {},
                    "recovery": seq.get("recovery"), "resumed": seq.get("resumed"),
                    "framesDone": sum((seq.get("framesDone") or {}).values()),
                }
                entry["warnings"] = [w for w in (status.get("warnings") or {}).get("active") or []
                                     if not _rig_warning(w, status)]
                entry["serverTime"] = status.get("serverTime")
                for rig in status.get("rigs") or []:
                    card = rig_card(node.name, status, rig, images, fresh)
                    entry["rigs"].append(card["rigId"])
                    cards.append(card)
            else:
                entry["sequence"] = None
                entry["warnings"] = []
                cards.append(offline_card(node.name, fresh))
                entry["rigs"].append(None)
            nodes.append(entry)
        summary = collections.Counter(card["state"] for card in cards)
        alerts = self.alerts(nodes, cards, now)
        with self.lock:
            commands = [dict(c) for c in list(self.commands.values())[-40:]]
        return {
            "time": now,
            "nodes": nodes,
            "cards": cards,
            "summary": {
                "imaging": summary.get("imaging", 0),
                "busy": summary.get("busy", 0) + summary.get("paused", 0),
                "idle": summary.get("idle", 0),
                "recovering": summary.get("recovering", 0) + summary.get("failed", 0),
                "offline": summary.get("offline", 0) + summary.get("stale", 0),
                "rigs": len(cards),
            },
            "alerts": alerts,
            "commands": commands,
        }

    def alerts(self, nodes: list[dict[str, Any]], cards: list[dict[str, Any]],
               now: float) -> list[dict[str, Any]]:
        settings = self.settings
        found: list[dict[str, Any]] = []

        def add(key: str, level: str, node: str, text: str, rig: str | None = None,
                since: float | None = None) -> None:
            first = self.alert_first_seen.setdefault(key, since or now)
            found.append({"id": key, "level": level, "node": node, "rig": rig,
                          "text": text, "since": first})

        for node in nodes:
            if node["state"] in ("offline", "stale", "connecting"):
                since = node.get("downSince") or now
                if now - since >= float(settings["offlineAlertMinutes"]) * 60.0:
                    add(f"offline:{node['name']}", "critical", node["name"],
                        f"{node['name']} has not answered for "
                        f"{(now - since) / 60.0:.0f} min — {node.get('error') or 'no answer'}",
                        since=since)
                continue
            seq = node.get("sequence") or {}
            watchdog = seq.get("watchdog") or {}
            if watchdog.get("state") == "stalled":
                add(f"stalled:{node['name']}", "critical", node["name"],
                    f"{node['name']}: the sequence has stalled — {watchdog.get('reason') or ''}")
            if seq.get("error") and not seq.get("running"):
                add(f"failed:{node['name']}:{seq['error'][:40]}", "warning", node["name"],
                    f"{node['name']}: {seq['error']}")
            for warning in node.get("warnings") or []:
                if warning.get("level") == "critical":
                    add(f"warn:{node['name']}:{warning.get('key')}", "warning", node["name"],
                        f"{node['name']}: {warning.get('title')}")
        for card in cards:
            rec = card.get("recovery") or {}
            if rec.get("state") == "failed":
                add(f"recfail:{card['node']}:{card['rigId']}", "critical", card["node"],
                    f"{card['name']}: recovery failed — {rec.get('reason') or ''}",
                    rig=card["rigId"])
            elif rec.get("state") == "recovering":
                since = rec.get("downSince") or rec.get("since") or now
                if now - since >= float(settings["recoveringAlertMinutes"]) * 60.0:
                    add(f"reclong:{card['node']}:{card['rigId']}", "warning", card["node"],
                        f"{card['name']}: recovering for {(now - since) / 60.0:.0f} min "
                        f"(attempt {rec.get('attempt')}/{rec.get('attempts')})",
                        rig=card["rigId"])
        live = {a["id"] for a in found}
        for key in [k for k in self.alert_first_seen if k not in live]:
            del self.alert_first_seen[key]
        order = {"critical": 0, "warning": 1}
        return sorted(found, key=lambda a: (order.get(a["level"], 2), a["since"]))

    # -- commands ----------------------------------------------------------
    def command(self, node_name: str, action: str, rig: str | None = None,
                confirm: bool = False, ra: float | None = None,
                dec: float | None = None) -> dict[str, Any]:
        node = self.nodes.get(node_name)
        if node is None:
            raise CommandError(404, f"no node called {node_name}")
        if action not in ACTIONS:
            raise CommandError(400, f"unknown command {action}")
        method, path, needs_rig, needs_confirm = ACTIONS[action]
        if needs_confirm and not confirm:
            raise CommandError(400, f"{action} moves the telescope: it needs confirm=true")
        if needs_rig and not rig:
            raise CommandError(400, f"{action} needs a telescope")
        body = None
        if action == "mount.slew":
            if ra is None or dec is None:
                raise CommandError(400, "a slew needs ra (hours) and dec (degrees)")
            body = {"ra": float(ra), "dec": float(dec)}
        record = {"id": next(self._ids), "node": node_name, "rig": rig, "action": action,
                  "state": "sending", "detail": "", "started": time.time(),
                  "finished": None, "taskId": None}
        with self.lock:
            self.commands[record["id"]] = record
            while len(self.commands) > 200:
                self.commands.popitem(last=False)
        params = {"rig": rig} if (rig and needs_rig) else None
        threading.Thread(target=self._forward, args=(node, record, method, path, params, body),
                         daemon=True, name=f"fleet-cmd-{record['id']}").start()
        return dict(record)

    def _forward(self, node: Node, record: dict[str, Any], method: str, path: str,
                 params: dict[str, Any] | None, body: dict[str, Any] | None) -> None:
        try:
            response = node.send(method, path, params=params, body=body)
            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.status_code == 200:
                if data.get("pending"):
                    task = data.get("task") or {}
                    update = {"state": "pending", "taskId": task.get("id"),
                              "detail": task.get("label") or "running on the node"}
                else:
                    update = {"state": "done", "finished": time.time(), "detail": "done"}
            else:
                detail = data.get("detail") if isinstance(data, dict) else None
                update = {"state": "failed", "finished": time.time(),
                          "detail": f"{response.status_code}: {detail or response.text[:200]}"}
        except Exception as exc:                  # noqa: BLE001 - shown on the page
            update = {"state": "failed", "finished": time.time(),
                      "detail": f"the node did not answer: {_describe(exc)}"}
        with self.lock:
            record.update(update)

    def node_polled(self, node: Node) -> None:
        """A fresh status: settle that node's pending commands from its tasks."""
        with node.lock:
            tasks = list((node.status or {}).get("tasks") or [])
        by_id = {task.get("id"): task for task in tasks}
        with self.lock:
            for record in self.commands.values():
                if record["node"] != node.name or record["state"] != "pending":
                    continue
                task = by_id.get(record["taskId"])
                if task is None:
                    if time.time() - record["started"] > 600:
                        record.update(state="unknown", finished=time.time(),
                                      detail="the node no longer reports this command")
                    continue
                if task.get("state") == "done":
                    record.update(state="done", finished=task.get("finished") or time.time(),
                                  detail="done")
                elif task.get("state") == "error":
                    record.update(state="failed", finished=task.get("finished") or time.time(),
                                  detail=task.get("error") or "failed")


# -- cards -------------------------------------------------------------------

def _rig_warning(warning: dict[str, Any], status: dict[str, Any]) -> bool:
    key = str(warning.get("key") or "")
    return any(str(rig.get("id")) and f".{rig.get('id')}" in key
               for rig in status.get("rigs") or [])


def offline_card(node_name: str, fresh: dict[str, Any]) -> dict[str, Any]:
    return {"node": node_name, "rigId": None, "name": node_name, "role": None,
            "state": "offline", "fresh": fresh, "mount": None, "sequence": None,
            "camera": None, "filter": None, "focuser": None, "guiding": None,
            "lastFrame": None, "recovery": None, "warnings": [], "capture": None}


def rig_card(node_name: str, status: dict[str, Any], rig: dict[str, Any],
             images: list[dict[str, Any]], fresh: dict[str, Any]) -> dict[str, Any]:
    devices = rig.get("devices") or {}
    rig_id = rig.get("id")
    master_id = status.get("masterRig")
    master = next((r for r in status.get("rigs") or [] if r.get("id") == master_id), rig)
    master_devices = master.get("devices") or {}
    seq = status.get("sequence") or {}
    tel = (seq.get("telescopes") or {}).get(rig_id) or {}
    rec = (seq.get("rigRecovery") or {}).get(rig_id) or tel.get("recovery")
    capture = rig.get("capture") or {}

    mount_raw = master_devices.get("mount") or {}
    mount = None
    if mount_raw.get("connected"):
        if mount_raw.get("atPark"):
            motion = "parked"
        elif mount_raw.get("slewing"):
            motion = "slewing"
        elif mount_raw.get("tracking"):
            motion = "tracking"
        else:
            motion = "stopped"
        mount = {"state": motion, "ra": _num(mount_raw.get("ra"), 4),
                 "dec": _num(mount_raw.get("dec"), 3),
                 "pierSide": mount_raw.get("sideOfPier"), "shared": rig_id != master_id,
                 "stale": bool(mount_raw.get("stale"))}
    elif "mount" in devices or rig_id == master_id:
        mount = {"state": "not connected", "shared": rig_id != master_id}

    camera_raw = devices.get("camera") or {}
    camera = None
    if camera_raw:
        camera = {"connected": bool(camera_raw.get("connected")),
                  "exposing": bool(camera_raw.get("exposing")),
                  "temperature": _num(camera_raw.get("temperature"), 1),
                  "setpoint": _num(camera_raw.get("setpoint"), 1),
                  "coolerOn": camera_raw.get("coolerOn"),
                  "coolerPower": _num(camera_raw.get("coolerPower"), 0),
                  "stale": bool(camera_raw.get("stale"))}
    wheel = devices.get("filterwheel") or {}
    focuser_raw = devices.get("focuser") or {}
    focuser = None
    if focuser_raw.get("connected"):
        focuser = {"position": focuser_raw.get("position"),
                   "temperature": _num(focuser_raw.get("temperature"), 1),
                   "moving": bool(focuser_raw.get("moving"))}
    guider_raw = master_devices.get("guider") or {}
    guiding = None
    if guider_raw.get("connected"):
        rms = guider_raw.get("rmsTotal")
        if rms is None and guider_raw.get("rmsRa") is not None \
                and guider_raw.get("rmsDec") is not None:
            rms = math.hypot(guider_raw["rmsRa"], guider_raw["rmsDec"])
        guiding = {"state": guider_raw.get("state"), "rms": _num(rms, 2),
                   "rmsPx": _num(guider_raw.get("rmsTotalPx"), 2),
                   "starLost": guider_raw.get("starLost") or ""}

    running = bool(seq.get("running"))
    in_run = running and (rig_id in (seq.get("telescopes") or {}) or rig_id == master_id)
    sequence = None
    if in_run or seq.get("error"):
        sequence = {
            "running": running, "paused": bool(seq.get("paused")),
            "state": seq.get("state"), "message": seq.get("message"),
            "target": capture.get("target") or "",
            "panel": seq.get("panel"), "panels": seq.get("panels"),
            "slot": seq.get("slot"), "slots": seq.get("slots"),
            "frame": tel.get("frame"), "frames": tel.get("frames"),
            "rigState": tel.get("state"), "error": seq.get("error"),
        }

    mine = [img for img in images if img.get("rig") == rig_id] if any(
        "rig" in img for img in images) else (images if rig_id == master_id else [])
    last = None
    if mine:
        image = max(mine, key=lambda i: i.get("timestamp") or 0)
        last = {"id": image.get("id"), "time": image.get("timestamp"),
                "filename": image.get("filename"), "filter": image.get("filter"),
                "exposure": image.get("exposure"),
                "thumb": f"/api/thumb/{node_name}/{image.get('id')}"}

    # Starfront's own warnings board is per PC (shown on the PC's header); a
    # card adds what can be told about this telescope alone.
    warnings = [{"level": w.get("level"), "title": w.get("title")}
                for w in (status.get("warnings") or {}).get("active") or []
                if f".{rig_id}" in str(w.get("key") or "")]
    if camera_raw and not camera_raw.get("connected"):
        warnings.append({"level": "critical", "title": "Camera not connected"})
    for kind, raw in (("camera", camera_raw), ("mount", mount_raw), ("focuser", focuser_raw)):
        if raw.get("stale"):
            warnings.append({"level": "warning",
                             "title": f"The {kind} is not answering (status is old)"})
    if camera and camera["connected"] and camera["coolerOn"] is False and in_run:
        warnings.append({"level": "warning", "title": "Cooler is off"})
    if guiding and guiding["starLost"]:
        warnings.append({"level": "warning", "title": "Guide star lost"})

    if fresh["state"] in ("offline", "connecting"):
        state = "offline"
    elif fresh["state"] == "stale":
        state = "stale"
    elif rec and rec.get("state") == "recovering":
        state = "recovering"
    elif rec and rec.get("state") == "failed":
        state = "failed"
    elif in_run and seq.get("paused"):
        state = "paused"
    elif in_run and seq.get("state") not in (None, "idle"):
        state = "imaging"
    elif capture.get("busy"):
        state = "busy"
    else:
        state = "idle"

    return {"node": node_name, "rigId": rig_id, "name": rig.get("name") or rig_id,
            "role": rig.get("role"), "state": state, "fresh": fresh, "mount": mount,
            "sequence": sequence, "camera": camera,
            "filter": wheel.get("currentName") if wheel.get("connected") else None,
            "focuser": focuser, "guiding": guiding, "lastFrame": last,
            "recovery": rec if rec and rec.get("state") not in (None, "ok") else None,
            "warnings": warnings,
            "capture": {"state": capture.get("state"), "busy": bool(capture.get("busy")),
                        "remaining": _num(capture.get("remaining"), 0)}}
