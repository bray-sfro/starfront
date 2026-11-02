"""Which focuser is on which camera, found out by moving them.

Three identical cameras and three identical focusers on one mount cannot be
told apart by looking at a driver list, and on a remote telescope they cannot
be told apart by looking at the telescope either.  What *can* be observed from
anywhere is cause and effect: move one focuser, and exactly one camera's stars
change size.  That is the whole method here.

Every camera is measured as it is, then each focuser in turn is moved out and
back by a few focus steps with every camera measured at each stop.  The camera
whose star size moved with the focuser is the one on the same telescope.  A
focuser that moves nobody's stars, or two that move the same camera, is said
plainly rather than guessed at.

The measure is the frame's median half-flux radius when there are stars to
measure, and a plain sharpness figure when there are not, so the check works on
a daytime horizon as well as on a star field — though stars are far more
sensitive, and a cloudy frame answers nothing.

`PairingCheck` runs on a thread of its own and reports progress the way a
focus run does, so the Equipment dialog can watch it.  `fix()` then puts each
focuser on the telescope whose camera it moved, which is a change to what the
telescopes *remember*, followed by a reconnect.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable

import numpy as np

from .devices.base import DeviceError
from .focusing import AutoFocuser
from .imaging import stars

#: Below this many stars the HFR is one bright thing's opinion; sharpness is
#: used instead.
MIN_STARS = 5

#: How much the measure has to move (as a fraction) before a camera is said to
#: have responded.  A quarter: a sweep of four focus steps on a focuser that
#: belongs to the camera moves the HFR by far more than that, while noise, a
#: passing cloud or seeing move it by far less.
RESPONSE = 0.25

#: A response has to beat the next best camera by this factor to be called
#: unambiguous.  Two cameras both moving is a sign of a shared mount flexing
#: or of clouds, not of two focusers.
CLEAR_MARGIN = 2.0

#: How many focus steps to move, in units of the telescope's own step size.
STEPS_OF_SWEEP = 4


class _Stopped(Exception):
    pass


def sharpness(frame: np.ndarray) -> float:
    """Fine detail in the frame relative to its contrast, focus-sensitive and
    exposure-insensitive.  Variance of the Laplacian over the variance of the
    image, on a 2x2 binned copy so read noise does not dominate."""
    image = np.asarray(frame, dtype=np.float64)
    if image.ndim != 2 or image.shape[0] < 8 or image.shape[1] < 8:
        return 0.0
    h, w = (image.shape[0] // 2) * 2, (image.shape[1] // 2) * 2
    small = image[:h, :w].reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    core = small[1:-1, 1:-1]
    lap = (4.0 * core - small[:-2, 1:-1] - small[2:, 1:-1]
           - small[1:-1, :-2] - small[1:-1, 2:])
    spread = float(small.var())
    if spread <= 0:
        return 0.0
    return float(lap.var() / spread)


def measure_frame(frame: np.ndarray) -> dict[str, Any]:
    """Both measures of one frame, and which of them is worth trusting."""
    found = stars.measure(frame)
    hfr = found.get("hfr")
    count = int(found.get("stars") or 0)
    return {
        "hfr": hfr,
        "stars": count,
        "sharpness": round(sharpness(frame), 6),
        "metric": "hfr" if (hfr and count >= MIN_STARS) else "sharpness",
    }


def change_between(before: dict[str, Any], after: dict[str, Any]) -> tuple[float, str]:
    """How far a camera's measure moved, as |ln(after/before)|, and by which
    measure.  HFR only counts when both frames had stars enough."""
    if (before.get("hfr") and after.get("hfr")
            and before.get("stars", 0) >= MIN_STARS and after.get("stars", 0) >= MIN_STARS):
        return abs(math.log(after["hfr"] / before["hfr"])), "hfr"
    b, a = float(before.get("sharpness") or 0.0), float(after.get("sharpness") or 0.0)
    if b <= 0 or a <= 0:
        return 0.0, "none"
    return abs(math.log(a / b)), "sharpness"


def decide(responses: dict[str, dict[str, float]], threshold: float = RESPONSE,
           margin: float = CLEAR_MARGIN) -> dict[str, dict[str, Any]]:
    """For each focuser (by telescope id), which camera moved with it.

    `responses[focuser_rig][camera_rig]` is the change that focuser produced on
    that camera.  Returns, per focuser, the chosen camera (or None), the change
    it showed, and whether the call was clear.  Pure, so it can be checked
    without a telescope.
    """
    verdicts: dict[str, dict[str, Any]] = {}
    for focuser_rig, seen in responses.items():
        ranked = sorted(seen.items(), key=lambda item: item[1], reverse=True)
        if not ranked:
            verdicts[focuser_rig] = {"camera": None, "change": 0.0, "clear": False,
                                     "why": "no camera was measured"}
            continue
        best_rig, best = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0.0
        if best < math.log1p(threshold):
            verdicts[focuser_rig] = {
                "camera": None, "change": best, "clear": False,
                "why": "no camera's stars changed when this focuser moved"}
            continue
        clear = runner <= 0 or best / max(runner, 1e-9) >= margin
        verdicts[focuser_rig] = {
            "camera": best_rig, "change": best, "clear": clear,
            "why": "" if clear else "more than one camera changed; "
                                    "the strongest response was taken"}
    return verdicts


def swaps_needed(verdicts: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Which telescope should *own* each focuser: camera telescope -> the
    telescope whose focuser slot currently holds it.  Only clear, complete
    verdicts count; the result is empty when nothing needs moving, and raises
    when two focusers claim the same camera."""
    owner: dict[str, str] = {}
    for focuser_rig, verdict in verdicts.items():
        camera = verdict.get("camera")
        if not camera:
            continue
        if camera in owner:
            raise DeviceError(
                "two focusers moved the same camera's stars; nothing was changed "
                "— try again on a steadier sky or with a longer exposure")
        owner[camera] = focuser_rig
    return {camera: focuser for camera, focuser in owner.items() if camera != focuser}


class PairingCheck:
    """The check itself, run on a thread and watched through `status()`."""

    def __init__(self, rigs: Any) -> None:
        self.rigs = rigs
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._abort = threading.Event()
        self._log: list[str] = []
        self._message = ""
        self._result: dict[str, Any] | None = None
        self._error: str | None = None
        self._started: float | None = None
        self._finished: float | None = None
        self._progress = (0, 0)

    # -- reporting ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict[str, Any]:
        with self._lock:
            done, total = self._progress
            return {
                "running": self.running,
                "message": self._message,
                "log": list(self._log[-40:]),
                "result": self._result,
                "error": self._error,
                "started": self._started,
                "finished": self._finished,
                "done": done,
                "total": total,
            }

    def _say(self, text: str, log: Callable[[str], None] | None = None) -> None:
        with self._lock:
            self._message = text
            self._log.append(f"{time.strftime('%H:%M:%S')}  {text}")
        if log:
            log(text)

    def _check(self) -> None:
        if self._abort.is_set():
            raise _Stopped()

    # -- control -----------------------------------------------------------
    def abort(self) -> None:
        self._abort.set()
        with self._lock:
            self._message = "stopping"

    def start(self, rig_ids: list[str] | None = None, steps: int | None = None,
              exposure: float | None = None,
              log: Callable[[str], None] | None = None) -> dict[str, Any]:
        if self.running:
            raise DeviceError("a focuser check is already running")
        chosen = [rig for rig in self.rigs.all
                  if not rig_ids or rig.id in rig_ids]
        cameras = [rig for rig in chosen
                   if (cam := rig.manager.get("camera")) is not None and cam.connected]
        focusers = [rig for rig in chosen
                    if (foc := rig.manager.get("focuser")) is not None and foc.connected]
        if not cameras:
            raise DeviceError("no camera is connected — connect the cameras first")
        if not focusers:
            raise DeviceError("no focuser is connected — connect the focusers first")
        for rig in cameras:
            if rig.capture.busy:
                raise DeviceError(f"{rig.name}'s camera is busy; wait for it or abort")
        if len(cameras) < 2 and len(focusers) < 2:
            raise DeviceError("there is only one telescope here; nothing to tell apart")

        with self._lock:
            self._abort.clear()
            self._log = []
            self._result = None
            self._error = None
            self._started = time.time()
            self._finished = None
            self._progress = (0, 1 + 2 * len(focusers))
        self._thread = threading.Thread(
            target=self._run, args=(cameras, focusers, steps, exposure, log),
            daemon=True, name="focuser-check")
        self._thread.start()
        return {"cameras": [r.id for r in cameras], "focusers": [r.id for r in focusers]}

    # -- the work ------------------------------------------------------------
    def _exposure_for(self, rig: Any, exposure: float | None) -> float:
        if exposure:
            return float(exposure)
        return float(rig.focuser.settings().get("focusExposure") or 6.0)

    def _steps_for(self, rig: Any, steps: int | None) -> int:
        if steps:
            return int(steps)
        return STEPS_OF_SWEEP * max(1, int(rig.focuser.settings().get("focusStepSize") or 100))

    def _measure_all(self, cameras: list[Any], exposure: float | None
                     ) -> dict[str, dict[str, Any]]:
        """One frame off every camera at once, measured."""
        results: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}

        def job(rig: Any) -> None:
            was_saving = rig.capture.save_enabled
            rig.capture.save_enabled = False
            try:
                record = rig.capture.capture_blocking(self._exposure_for(rig, exposure))
                frame = rig.capture.frame(record.id)
                results[rig.id] = measure_frame(frame)
            except Exception as exc:                 # noqa: BLE001 - reported per camera
                errors[rig.id] = str(exc)
            finally:
                rig.capture.save_enabled = was_saving

        threads = [threading.Thread(target=job, args=(rig,), daemon=True,
                                    name=f"check-{rig.id}") for rig in cameras]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=600)
        for rig_id, text in errors.items():
            name = next((r.name for r in cameras if r.id == rig_id), rig_id)
            self._say(f"{name}: the frame failed — {text}")
        self._check()
        return results

    def _run(self, cameras: list[Any], focusers: list[Any], steps: int | None,
             exposure: float | None, log: Callable[[str], None] | None) -> None:
        def describe(measures: dict[str, dict[str, Any]]) -> str:
            parts = []
            for rig in cameras:
                m = measures.get(rig.id)
                if not m:
                    parts.append(f"{rig.name}: no frame")
                elif m["metric"] == "hfr":
                    parts.append(f"{rig.name}: HFR {m['hfr']:.2f} from {m['stars']} stars")
                else:
                    parts.append(f"{rig.name}: sharpness {m['sharpness']:.4f} "
                                 f"({m['stars']} stars, too few for HFR)")
            return "; ".join(parts)

        moved_focuser = None
        start_position = None
        try:
            self._say("Measuring every camera as it is now", log)
            base = self._measure_all(cameras, exposure)
            if not base:
                raise DeviceError("no camera produced a frame")
            self._say(describe(base), log)
            with self._lock:
                self._progress = (1, self._progress[1])

            responses: dict[str, dict[str, float]] = {}
            metrics: dict[str, dict[str, str]] = {}
            for index, rig in enumerate(focusers):
                self._check()
                focuser = rig.manager.require("focuser")
                step = self._steps_for(rig, steps)
                start_position = int(focuser.position)
                moved_focuser = focuser
                # Out one way, then the other, so a focuser that happens to
                # start exactly between two equal star sizes still shows.
                stops = []
                for sign in (+1, -1):
                    target = start_position + sign * step
                    if focuser.max_step and not 0 <= target <= focuser.max_step:
                        target = start_position - sign * step
                    stops.append(target)
                responses[rig.id] = {cam.id: 0.0 for cam in cameras}
                metrics[rig.id] = {}
                for stop_index, target in enumerate(stops):
                    self._check()
                    self._say(f"Moving {rig.name}'s focuser to {target} "
                              f"({target - start_position:+d} steps)", log)
                    AutoFocuser._settle(focuser, target)
                    measured = self._measure_all(cameras, exposure)
                    self._say(describe(measured), log)
                    for cam in cameras:
                        if cam.id in base and cam.id in measured:
                            change, metric = change_between(base[cam.id], measured[cam.id])
                            if change > responses[rig.id][cam.id]:
                                responses[rig.id][cam.id] = change
                                metrics[rig.id][cam.id] = metric
                    with self._lock:
                        self._progress = (2 + 2 * index + stop_index, self._progress[1])
                self._say(f"Putting {rig.name}'s focuser back to {start_position}", log)
                AutoFocuser._settle(focuser, start_position)
                moved_focuser = None

            verdicts = decide(responses)
            names = {rig.id: rig.name for rig in self.rigs.all}
            focuser_names = {rig.id: rig.manager.require("focuser").name for rig in focusers}
            rows = []
            for rig in focusers:
                verdict = verdicts[rig.id]
                camera = verdict["camera"]
                percent = round((math.exp(verdict["change"]) - 1) * 100)
                rows.append({
                    "rig": rig.id, "name": rig.name,
                    "focuser": focuser_names.get(rig.id, ""),
                    "camera": camera,
                    "cameraName": names.get(camera, "") if camera else "",
                    "ownCamera": camera == rig.id,
                    "changePercent": percent,
                    "metric": metrics[rig.id].get(camera, "") if camera else "",
                    "clear": verdict["clear"],
                    "why": verdict["why"],
                    "responses": {cam: round((math.exp(v) - 1) * 100)
                                  for cam, v in responses[rig.id].items()},
                })
            try:
                moves = swaps_needed(verdicts)
                conflict = ""
            except DeviceError as exc:
                moves = {}
                conflict = str(exc)
            summary = []
            for row in rows:
                if row["camera"] is None:
                    summary.append(f"{row['name']}'s focuser moved nobody's stars")
                elif row["ownCamera"]:
                    summary.append(f"{row['name']}'s focuser is on its own camera")
                else:
                    summary.append(f"the focuser on {row['name']} actually focuses "
                                   f"{row['cameraName']}'s camera")
            with self._lock:
                self._result = {
                    "rows": rows,
                    "cameras": [{"rig": r.id, "name": r.name} for r in cameras],
                    "moves": [{"to": camera, "toName": names.get(camera, camera),
                               "from": focuser, "fromName": names.get(focuser, focuser),
                               "focuser": focuser_names.get(focuser, "")}
                              for camera, focuser in moves.items()],
                    "conflict": conflict,
                    "allMatched": all(row["ownCamera"] for row in rows),
                    "summary": summary,
                }
            self._say("Done: " + "; ".join(summary), log)
        except _Stopped:
            self._say("Stopped", log)
            with self._lock:
                self._error = "stopped"
        except Exception as exc:                     # noqa: BLE001 - reported
            self._say(f"The check failed: {exc}", log)
            with self._lock:
                self._error = str(exc)
        finally:
            if moved_focuser is not None and start_position is not None:
                try:
                    AutoFocuser._settle(moved_focuser, start_position)
                except Exception:                    # noqa: BLE001 - best effort
                    pass
            with self._lock:
                self._finished = time.time()
                self._progress = (self._progress[1], self._progress[1])

    # -- putting it right ----------------------------------------------------
    def fix(self, equipment: Any) -> dict[str, Any]:
        """Give each focuser to the telescope whose camera it moved.

        What a telescope *remembers* for its focuser slot is changed, then the
        focusers are reconnected from what is now remembered, so the fix is
        the same as having chosen the right driver in the first place and it
        survives a restart.
        """
        with self._lock:
            result = dict(self._result or {})
        if self.running:
            raise DeviceError("the check is still running")
        if not result:
            raise DeviceError("run the check first")
        if result.get("conflict"):
            raise DeviceError(result["conflict"])
        moves = result.get("moves") or []
        if not moves:
            return {"changed": [], "note": "every focuser is already on its own telescope"}

        # The focuser spec each telescope remembers now, before anything moves.
        remembered = {rig.id: dict(equipment.rig(rig.id)["devices"].get("focuser") or {})
                      for rig in self.rigs.all}
        involved = {m["to"] for m in moves} | {m["from"] for m in moves}
        for rig in self.rigs.all:
            if rig.id in involved:
                rig.manager.disconnect("focuser")
        changed = []
        for move in moves:
            spec = remembered.get(move["from"])
            if not spec:
                continue
            equipment.set_device(move["to"], "focuser", spec)
            changed.append(move["to"])
        # A telescope whose focuser went elsewhere, and received nothing, is
        # left with no focuser rather than a stale one that opens somebody
        # else's.
        receivers = {m["to"] for m in moves}
        for rig_id in involved - receivers:
            equipment.set_device(rig_id, "focuser", None)
        reconnected: list[str] = []
        failed: list[str] = []
        for rig in self.rigs.all:
            if rig.id in involved:
                outcome = self.rigs.connect_remembered(rig, kinds=("focuser",))
                reconnected += [f"{rig.name}" for _ in outcome["connected"]]
                failed += [f"{rig.name}: {text}" for text in outcome["failed"]]
        with self._lock:
            if self._result is not None:
                self._result = {**self._result, "applied": True}
        return {"changed": changed, "reconnected": reconnected, "failed": failed}
