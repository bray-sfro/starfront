"""The calibration library: master frames on disk, and the arithmetic on them.

A master frame is one file in the library folder whose FITS header says what it
is a master *of* — exposure, gain, offset, binning, temperature, filter, which
telescope — and how many subs went into it.  That is deliberately the whole
index: the folder itself is the database, so a master dropped in by hand from
PixInsight is picked up on the next scan, and deleting one is deleting a file.

Two things happen here:

  * **building** a master out of a stack of subs.  Sigma-clipped mean by
    default, which is what everything else uses, and streamed over the files
    rather than loaded at once — thirty frames off a 26-megapixel sensor is a
    gigabyte and a half, and this runs on the same machine that is driving the
    mount.

  * **matching and applying** them to a light frame.  A master only applies to a
    frame it actually describes: same sensor, same binning, same gain and
    offset, a close enough exposure and temperature, and recent enough to still
    be true.  A mismatched master is worse than none at all, so anything that
    does not match is refused rather than stretched to fit.

Nothing here ever touches the raw light frame.  Calibrated frames are written
alongside it in a `calibrated` subfolder, so the night's real data is still the
night's real data.
"""

from __future__ import annotations

import contextlib
import math
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from .config import Config, data_root
from .devices.base import DeviceError
from .imaging import fits, xisf

# What a master can be a master of.  `darkflat` is a dark at the flat's own
# exposure: with a CMOS camera it is what a flat should be corrected by, because
# a bias does not describe the amp glow in a two-second frame.
MASTER_TYPES = ("bias", "dark", "darkflat", "flat")

# Frame types that need a filter to be identified.  A dark does not care what
# was in the light path; a flat is meaningless without it.
FILTERED_TYPES = ("flat", "darkflat")

# How many master frames to hold in memory.  Each is the size of one sub, and a
# light frame needs at most three of them, so this covers a night of one rig
# comfortably and two rigs without thrashing.
CACHE_SIZE = 6

# Sigma clipping needs a spread to clip against, and a handful of frames does
# not have one worth trusting.  Below this the stack falls back to a plain mean.
MIN_FRAMES_FOR_CLIPPING = 5

# Loading a whole stack for a true median is only allowed when it fits in this
# much memory.  Above it the sigma-clipped mean is used instead, which is a
# better estimator anyway once there are enough frames to clip.
MEDIAN_BYTE_BUDGET = 1_500_000_000

_UNSAFE = re.compile(r"[^A-Za-z0-9._+-]+")


def _tag(text: Any) -> str:
    return _UNSAFE.sub("_", str(text or "").strip()).strip("_")


def library_root(config: Config | None = None) -> Path:
    """Where the masters live: the configured folder, or one beside the data."""
    configured = ""
    if config is not None:
        configured = (config.get("calibration", "libraryDirectory", "") or "").strip()
    return (Path(configured).expanduser() if configured
            else data_root() / "calibration")


# ---------------------------------------------------------------------------
# Describing a master
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    """A header string, with the absent cases all reading as absent."""
    text = str(value or "").strip()
    return "" if text.lower() in ("none", "null", "n/a") else text


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def describe(path: Path, header: dict[str, Any]) -> dict[str, Any] | None:
    """One master, as the matcher sees it.  None if the file is not one."""
    kind = str(header.get("IMAGETYP") or "").strip().lower()
    # Written by this program as "Master dark"; PixInsight and Siril use plain
    # "Dark" or "Master Dark", so match on the word rather than the whole card.
    # Longest first: "master darkflat" contains both "dark" and "flat", and it
    # is neither of them.
    for candidate in sorted(MASTER_TYPES, key=len, reverse=True):
        if candidate in kind.replace(" ", ""):
            kind = candidate
            break
    else:
        return None

    # Frames written before the header writer was fixed carry the four letters
    # "None" where a value was simply not known, so those read as absent too.
    filter_name = _text(header.get("FILTER"))
    return {
        "id": path.stem,
        "path": str(path),
        "type": kind,
        "exposure": _number(header.get("EXPTIME")) or _number(header.get("EXPOSURE")) or 0.0,
        "binning": int(_number(header.get("XBINNING")) or 1),
        "gain": _number(header.get("GAIN")),
        "offset": _number(header.get("OFFSET")),
        "temperature": _number(header.get("CCD-TEMP")),
        "filter": filter_name,
        "telescope": _text(header.get("TELESCOP")),
        "camera": _text(header.get("INSTRUME")),
        "width": int(_number(header.get("NAXIS1")) or 0),
        "height": int(_number(header.get("NAXIS2")) or 0),
        "frames": int(_number(header.get("NCOMBINE")) or 1),
        "method": str(header.get("STACKMTH") or "").strip(),
        # The library is a folder, so the file's own timestamp is the honest
        # answer to "how old is this master" even for one copied in by hand.
        "created": path.stat().st_mtime,
        "sizeBytes": path.stat().st_size,
    }


def master_filename(kind: str, meta: dict[str, Any]) -> str:
    """A name that says what the master is, for a folder people read."""
    parts = [f"MASTER_{kind.upper()}"]
    if kind in FILTERED_TYPES and meta.get("filter"):
        parts.append(_tag(meta["filter"]))
    if kind in ("dark", "darkflat", "flat") and meta.get("exposure"):
        parts.append(f"{float(meta['exposure']):g}s")
    if meta.get("gain") is not None:
        parts.append(f"g{int(meta['gain'])}")
    if meta.get("offset") is not None:
        parts.append(f"o{int(meta['offset'])}")
    parts.append(f"bin{int(meta.get('binning') or 1)}")
    if meta.get("temperature") is not None:
        parts.append(f"{float(meta['temperature']):.0f}C")
    if meta.get("telescope"):
        parts.append(_tag(meta["telescope"]))
    parts.append(time.strftime("%Y%m%d", time.localtime()))
    return "_".join(parts) + ".fits"


# ---------------------------------------------------------------------------
# Stacking
# ---------------------------------------------------------------------------

def _load(path: str | Path) -> np.ndarray:
    frame, _ = fits.read(path)
    return frame.astype(np.float32)


def stack(paths: list[str | Path], method: str = "sigma",
          sigma_low: float = 3.0, sigma_high: float = 3.0,
          subtract: np.ndarray | None = None,
          normalise: bool = False,
          should_abort=None) -> tuple[np.ndarray, dict[str, Any]]:
    """Combine a set of subs into one master.

    `subtract` is taken off every sub first, which is how a flat gets its own
    dark removed before the flats are combined.

    `normalise` scales every sub to a common level before combining, which is
    what makes a sky flat possible: twilight fades by a factor of two while the
    set is being taken, and without this the first frames would simply outvote
    the last.  It also lets the clipping do its job — stars land in a different
    place in each frame, and only frames at a common level can be compared
    pixel by pixel to reject them.

    The sigma-clipped mean is done in three passes over the files rather than
    holding the stack in memory: a night's darks off a large sensor is more RAM
    than the machine driving the mount can spare, and the passes are disk reads
    that take seconds.
    """
    if not paths:
        raise DeviceError("there are no frames to stack")

    def check() -> None:
        if should_abort is not None and should_abort():
            raise DeviceError("stacking was stopped")

    first = _load(paths[0])
    shape = first.shape
    if subtract is not None and subtract.shape != shape:
        raise DeviceError("the frame being subtracted is a different size")

    # Scaling every frame to the level of the first keeps the master in real
    # ADU rather than around 1.0, so it stays a 16-bit file like every other
    # master and the same division applies it.
    reference = 0.0
    if normalise:
        base = first if subtract is None else first - subtract
        reference = float(np.median(base))
        if reference <= 0:
            normalise = False

    def prepared(path: str | Path) -> np.ndarray:
        frame = _load(path)
        if frame.shape != shape:
            raise DeviceError(f"{Path(path).name} is {frame.shape[1]}x{frame.shape[0]}, "
                              f"not {shape[1]}x{shape[0]}")
        if subtract is not None:
            frame = frame - subtract
        if normalise:
            level = float(np.median(frame))
            if level > 0:
                frame = frame * (reference / level)
        return frame

    count = len(paths)
    if method == "median" and count * shape[0] * shape[1] * 4 <= MEDIAN_BYTE_BUDGET:
        cube = np.empty((count,) + shape, dtype=np.float32)
        for index, path in enumerate(paths):
            check()
            cube[index] = prepared(path)
        result = np.median(cube, axis=0)
        return result, {"method": "median", "frames": count,
                        "normalised": bool(normalise)}

    # Pass one: the mean.
    total = np.zeros(shape, dtype=np.float32)
    for path in paths:
        check()
        total += prepared(path)
    mean = total / count
    if method == "mean" or count < MIN_FRAMES_FOR_CLIPPING:
        note = ("mean" if method == "mean"
                else f"mean ({count} frames is too few to clip against)")
        return mean, {"method": note, "frames": count,
                      "normalised": bool(normalise)}

    # Pass two: the spread about it.  Deviations rather than a sum of squares,
    # so the accumulator stays small enough for single precision to be exact.
    squared = np.zeros(shape, dtype=np.float32)
    for path in paths:
        check()
        squared += np.square(prepared(path) - mean)
    deviation = np.sqrt(squared / max(1, count - 1))

    # Pass three: the mean of what survives the clip.
    low = mean - sigma_low * deviation
    high = mean + sigma_high * deviation
    kept_total = np.zeros(shape, dtype=np.float32)
    kept_count = np.zeros(shape, dtype=np.float32)
    for path in paths:
        check()
        frame = prepared(path)
        inside = (frame >= low) & (frame <= high)
        kept_total += np.where(inside, frame, 0.0)
        kept_count += inside
    # A pixel every frame disagreed about keeps the plain mean rather than a
    # division by zero.
    result = np.where(kept_count > 0, kept_total / np.maximum(kept_count, 1.0), mean)
    rejected = float(count - kept_count.mean())
    return result, {
        "method": f"sigma clip {sigma_low:g}/{sigma_high:g}",
        "frames": count,
        "rejectedPerPixel": round(rejected, 3),
        "normalised": bool(normalise),
    }


def to_uint16(frame: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(frame), 0, 65535).astype(np.uint16)


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def apply_masters(frame: np.ndarray, dark: np.ndarray | None = None,
                  flat: np.ndarray | None = None,
                  bias: np.ndarray | None = None) -> tuple[np.ndarray, list[str]]:
    """Calibrate one light frame with whichever masters were found.

    Dark first — it carries the bias with it, so a bias is only used when there
    is no dark.  Then the flat, normalised by its own median so the frame keeps
    the scale it had rather than being pushed into a corner of the range.

    Values are clipped at zero.  A dark-subtracted sky background genuinely does
    go negative on some pixels, and a 16-bit file cannot say so; anything that
    cares about the noise below zero should be stacking the raw frames, which is
    exactly why the raw frames are kept.
    """
    out = frame.astype(np.float32)
    steps: list[str] = []

    if dark is not None:
        out -= dark.astype(np.float32)
        steps.append("dark")
    elif bias is not None:
        out -= bias.astype(np.float32)
        steps.append("bias")

    if flat is not None:
        normal = flat.astype(np.float32)
        level = float(np.median(normal))
        if level <= 0:
            steps.append("flat skipped (it is blank)")
        else:
            # A dead column in the flat would otherwise become a divide by zero
            # and then a white stripe; leaving those pixels uncorrected is the
            # honest answer.
            usable = normal > (level * 0.05)
            gain = np.where(usable, level / np.maximum(normal, 1e-6), 1.0)
            out *= gain
            steps.append("flat")

    return to_uint16(out), steps


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------

class Library:
    """The masters on disk, and the rules for which one fits a given frame."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        # Path -> (mtime, description), so a rescan of an unchanged folder costs
        # one stat per file instead of one header read.
        self._seen: dict[str, tuple[float, dict[str, Any]]] = {}

    # -- where things are --------------------------------------------------
    @property
    def root(self) -> Path:
        return library_root(self.config)

    @property
    def masters_dir(self) -> Path:
        return self.root / "masters"

    @property
    def subs_dir(self) -> Path:
        return self.root / "subs"

    def settings(self) -> dict[str, Any]:
        return self.config.section("calibration")

    # -- reading it --------------------------------------------------------
    def masters(self) -> list[dict[str, Any]]:
        """Every master in the library, newest first."""
        folder = self.masters_dir
        found: list[dict[str, Any]] = []
        if not folder.is_dir():
            return found
        with self._lock:
            live: dict[str, tuple[float, dict[str, Any]]] = {}
            for path in sorted(folder.glob("*.fit*")):
                key = str(path)
                try:
                    stamp = path.stat().st_mtime
                except OSError:
                    continue
                cached = self._seen.get(key)
                if cached is not None and cached[0] == stamp:
                    live[key] = cached
                    found.append(cached[1])
                    continue
                try:
                    described = describe(path, fits.read_header(path))
                except (OSError, ValueError):
                    continue
                if described is None:
                    continue
                live[key] = (stamp, described)
                found.append(described)
            self._seen = live
        found.sort(key=lambda m: m["created"], reverse=True)
        return found

    def frame(self, master: dict[str, Any]) -> np.ndarray:
        """The pixels of a master, held for the next frame that wants them."""
        key = master["path"]
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        image, _ = fits.read(key)
        with self._lock:
            self._cache[key] = image
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return image

    def forget(self) -> None:
        """Drop everything held in memory; the next read comes off disk."""
        with self._lock:
            self._cache.clear()
            self._seen.clear()

    def remove(self, master_id: str) -> dict[str, Any]:
        for master in self.masters():
            if master["id"] == master_id:
                Path(master["path"]).unlink()
                self.forget()
                return master
        raise DeviceError(f"no master called {master_id!r}")

    # -- matching ----------------------------------------------------------
    def match(self, kind: str, want: dict[str, Any],
              candidates: list[dict[str, Any]] | None = None,
              ignore_age: bool = False) -> tuple[dict[str, Any] | None, str]:
        """The best master of `kind` for a frame described by `want`.

        Returns the master and a sentence saying why, or None and a sentence
        saying why not — the second half is the point: "no master dark" and
        "the only master dark is at -5 C and this frame is at -20 C" call for
        very different actions, and a silent failure says neither.

        `ignore_age` answers a different question - is there one at all,
        however old - which is how "out of date" is told from "missing".
        """
        settings = self.settings()
        pool = [m for m in (candidates if candidates is not None else self.masters())
                if m["type"] == kind]
        if not pool:
            return None, f"there is no master {kind} in the library"

        width, height = want.get("width"), want.get("height")
        sized = [m for m in pool
                 if not width or not height
                 or (m["width"] == width and m["height"] == height)]
        if not sized:
            return None, (f"every master {kind} is a different size from this frame "
                          f"({width}x{height})")

        binned = [m for m in sized if m["binning"] == int(want.get("binning") or 1)]
        if not binned:
            return None, (f"no master {kind} at bin {int(want.get('binning') or 1)}")

        # Gain and offset change the pedestal and the noise, so a master taken
        # at another setting describes a different camera as far as this is
        # concerned.  A master that never recorded them is allowed through.
        matched = [m for m in binned
                   if _same(m.get("gain"), want.get("gain"))
                   and _same(m.get("offset"), want.get("offset"))]
        if not matched:
            return None, (f"no master {kind} at gain {want.get('gain')} / "
                          f"offset {want.get('offset')}")

        if kind in FILTERED_TYPES:
            # Compared in the one spelling: a flat filed as "Ha" last month
            # still matches a frame shot through "H" tonight.
            from .filters import canonical
            wanted_filter = canonical(want.get("filter") or "").lower()
            matched = [m for m in matched
                       if canonical(m["filter"] or "").lower() == wanted_filter]
            if not matched:
                return None, (f"no master {kind} through "
                              + (f"the {want.get('filter')} filter"
                                 if wanted_filter else "no filter"))

        # A telescope's flats are its own — different dust, different vignetting
        # — so a named one only ever matches itself.  Darks are the camera's, so
        # a telescope name on them is a preference rather than a rule.
        scope = (want.get("telescope") or "").strip().lower()
        if kind in ("flat", "darkflat") and scope:
            same_scope = [m for m in matched
                          if (m["telescope"] or "").strip().lower() == scope]
            if not same_scope:
                return None, f"no master {kind} for {want.get('telescope')}"
            matched = same_scope

        if kind in ("dark", "darkflat"):
            tolerance = max(0.0, float(settings.get("matchExposurePercent") or 0) / 100.0)
            wanted = float(want.get("exposure") or 0)
            close = [m for m in matched
                     if abs(m["exposure"] - wanted) <= max(0.001, wanted * tolerance)]
            if not close:
                available = ", ".join(sorted({f"{m['exposure']:g}s" for m in matched}))
                return None, (f"no master {kind} at {wanted:g}s "
                              f"(the library has {available})")
            matched = close

        limit = float(settings.get("matchTemperatureC") or 0)
        wanted_temp = _number(want.get("temperature"))
        if limit and wanted_temp is not None:
            warm = [m for m in matched
                    if m["temperature"] is None
                    or abs(m["temperature"] - wanted_temp) <= limit]
            if not warm:
                nearest = min(matched,
                              key=lambda m: abs((m["temperature"] or 0) - wanted_temp))
                return None, (f"the nearest master {kind} is at "
                              f"{nearest['temperature']:g} C and this frame is at "
                              f"{wanted_temp:g} C")
            matched = warm

        age_key = "maxFlatAgeDays" if kind in ("flat", "darkflat") else "maxDarkAgeDays"
        max_age = 0.0 if ignore_age else float(settings.get(age_key) or 0)
        if max_age:
            cutoff = time.time() - max_age * 86400.0
            fresh = [m for m in matched if m["created"] >= cutoff]
            if not fresh:
                newest = max(matched, key=lambda m: m["created"])
                days = (time.time() - newest["created"]) / 86400.0
                return None, (f"the newest master {kind} is {days:.0f} days old and "
                              f"the limit is {max_age:g}")
            matched = fresh

        # Among the ones that fit: the telescope's own master first, then
        # closest in temperature, then the most subs, then the newest.
        #
        # The telescope comes first even for darks, where it is a preference
        # rather than a rule.  Two telescopes on one mount are two different
        # sensors, and the same sensor a degree off the ideal is a better
        # description of this frame than a different sensor at exactly the right
        # temperature.  A master with no telescope on it — one built elsewhere,
        # or by another program — is not penalised, because it may well be the
        # only one there is.
        camera = (want.get("camera") or "").strip().lower()

        def rank(master: dict[str, Any]) -> tuple:
            owner = (master["telescope"] or "").strip().lower()
            mine = 0 if (not owner or not scope or owner == scope) else 1
            same_camera = 0 if (not camera or not master["camera"]
                                or master["camera"].strip().lower() == camera) else 1
            temp_gap = (abs((master["temperature"] or 0) - wanted_temp)
                        if wanted_temp is not None and master["temperature"] is not None
                        else 0.0)
            return (mine, same_camera, round(temp_gap, 2),
                    -master["frames"], -master["created"])

        best = sorted(matched, key=rank)[0]
        return best, (f"{Path(best['path']).name} "
                      f"({best['frames']} frames"
                      + (f", {best['temperature']:g} C"
                         if best["temperature"] is not None else "")
                      + ")")

    def coverage(self, needs: list[dict[str, Any]]) -> dict[str, Any]:
        """Whether the library holds what tonight will ask of it, item by item.

        Each need is a kind, a label and a `want` the matcher understands.
        The answer per item is one of three words, because those are the
        three things a person can do about it: `ok` (nothing), `stale` (the
        master is there but older than the limit - shoot it again), or
        `missing` (shoot it, or bring one in). A summary line says which of
        those the night is in.
        """
        pool = self.masters()
        rows: list[dict[str, Any]] = []
        for need in needs:
            kind = need["kind"]
            found, why = self.match(kind, need["want"], pool)
            if found is not None:
                age = (time.time() - found["created"]) / 86400.0
                rows.append({**need, "state": "ok", "master": Path(found["path"]).name,
                             "ageDays": round(age, 1), "detail": why})
                continue
            old, _ = self.match(kind, need["want"], pool, ignore_age=True)
            if old is not None:
                age = (time.time() - old["created"]) / 86400.0
                rows.append({**need, "state": "stale", "master": Path(old["path"]).name,
                             "ageDays": round(age, 1),
                             "detail": f"{age:.0f} days old - {why}"})
            else:
                rows.append({**need, "state": "missing", "master": None,
                             "ageDays": None, "detail": why})
        stale = sum(1 for r in rows if r["state"] == "stale")
        missing = sum(1 for r in rows if r["state"] == "missing")
        if not rows:
            summary, state = "nothing to check - no filters or exposures are known", "unknown"
        elif not stale and not missing:
            summary, state = "a complete, current set of masters", "ok"
        elif not missing:
            summary, state = (f"{stale} master{'s' if stale != 1 else ''} out of date",
                              "stale")
        else:
            bits = [f"{missing} missing"]
            if stale:
                bits.append(f"{stale} out of date")
            summary, state = ", ".join(bits), "missing"
        return {"rows": rows, "state": state, "summary": summary,
                "stale": stale, "missing": missing, "ok": len(rows) - stale - missing}

    def plan_for(self, want: dict[str, Any]) -> dict[str, Any]:
        """Which masters would be used for a frame like this, and why."""
        pool = self.masters()
        result: dict[str, Any] = {"reasons": {}, "masters": {}}
        for kind in ("dark", "flat", "bias"):
            # There is no "bias" type as such; a bias is a dark of no length,
            # which is what every camera actually writes.
            lookup = "bias" if kind == "bias" else kind
            found, why = self.match(lookup, want, pool)
            result["masters"][kind] = found
            result["reasons"][kind] = why
        result["usable"] = any(result["masters"].values())
        return result

    # -- applying ----------------------------------------------------------
    def wanted_for(self, context: str) -> bool:
        """Whether frames of this sort should be calibrated as they are taken."""
        mode = str(self.settings().get("applyTo") or "off").lower()
        if mode == "all":
            return context in ("survey", "light")
        if mode == "survey":
            return context == "survey"
        return False

    def calibrate_file(self, path: Path, want: dict[str, Any]) -> dict[str, Any]:
        """Calibrate a frame that has just been written, alongside the original.

        The raw frame is never touched.  If nothing in the library matches, the
        reasons come back so the operator finds out during the night rather than
        while stacking a week later.
        """
        chosen = self.plan_for(want)
        masters = chosen["masters"]
        if not chosen["usable"]:
            return {"calibrated": False, "reasons": chosen["reasons"]}

        frame, header = fits.read(path)
        pixels = {kind: (self.frame(master) if master is not None else None)
                  for kind, master in masters.items()}
        for kind, image in pixels.items():
            if image is not None and image.shape != frame.shape:
                masters[kind] = None
                pixels[kind] = None
                chosen["reasons"][kind] = (
                    f"the master {kind} is {image.shape[1]}x{image.shape[0]} and this "
                    f"frame is {frame.shape[1]}x{frame.shape[0]}")
        if all(image is None for image in pixels.values()):
            return {"calibrated": False, "reasons": chosen["reasons"]}

        result, steps = apply_masters(frame, pixels["dark"], pixels["flat"],
                                      pixels["bias"])

        # Everything the original said, plus what was done to it.  A stacker
        # that reads CALSTAT will not try to calibrate this a second time.
        carried = {key: value for key, value in header.items()
                   if key not in ("SIMPLE", "BITPIX", "NAXIS", "NAXIS1", "NAXIS2",
                                  "BZERO", "BSCALE", "END")}
        # The conventional one-letter-per-step card: B bias, D dark, F flat.
        # Only steps that were actually applied count, so a flat that was
        # skipped as blank does not claim to have been used.
        letters = {"bias": "B", "dark": "D", "flat": "F"}
        carried["CALSTAT"] = ("".join(sorted(letters[s] for s in steps
                                             if s in letters)),
                              "calibration applied")
        for kind, master in masters.items():
            if master is not None:
                carried[{"dark": "MDARK", "flat": "MFLAT",
                         "bias": "MBIAS"}[kind]] = (Path(master["path"]).name, "")
        carried["CALSWARE"] = ("Starfront", "")

        out = path.parent / "calibrated" / f"{path.stem}_cal{path.suffix}"
        fits.write(out, result, carried)
        return {
            "calibrated": True,
            "path": str(out),
            "steps": steps,
            "reasons": chosen["reasons"],
            "used": {kind: (Path(m["path"]).name if m else None)
                     for kind, m in masters.items()},
        }

    # -- bringing one in ---------------------------------------------------
    def import_master(self, source: str | Path,
                      overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Copy a master built elsewhere into the library, as a FITS master.

        PixInsight, Siril, N.I.N.A.'s own stacker: whatever made it, a master
        is a frame plus what it is a master *of*. What the file's header says
        is the starting point and `overrides` (type, filter, exposure,
        temperature, gain, offset, binning, telescope) is what the operator
        corrected on the way in, because headers from other programs are
        often missing the one card that matters.

        The pixels are written as 16-bit ADU. A float master normalised to
        0-1 - PixInsight's default - is scaled back up by 65535 so a dark
        subtracts in the same units as the lights it is subtracted from.
        """
        overrides = overrides or {}
        source = Path(source).expanduser()
        if not source.is_file():
            raise DeviceError(f"{source} is not a file")
        values, header = read_master_file(source)

        kind = str(overrides.get("type") or "").strip().lower()
        if not kind:
            described = describe(source, header)
            kind = described["type"] if described else ""
        if kind not in MASTER_TYPES:
            raise DeviceError("say what this master is: a bias, a dark, a flat "
                              "or a dark for the flats - the file does not")

        seen = describe(source, {**header, "IMAGETYP": f"Master {kind}"}) or {}
        meta = {
            "exposure": _pick(overrides.get("exposure"), seen.get("exposure"), 0.0),
            "binning": int(_pick(overrides.get("binning"), seen.get("binning"), 1)),
            "gain": _pick(overrides.get("gain"), seen.get("gain"), None),
            "offset": _pick(overrides.get("offset"), seen.get("offset"), None),
            "temperature": _pick(overrides.get("temperature"),
                                 seen.get("temperature"), None),
            "filter": str(_pick(overrides.get("filter"), seen.get("filter"), "") or ""),
            "telescope": str(_pick(overrides.get("telescope"),
                                   seen.get("telescope"), "") or ""),
            "camera": str(seen.get("camera") or ""),
        }
        if kind == "bias":
            meta["exposure"] = 0.0
        if meta["filter"]:
            # Filed in the one spelling, so a flat brought in as "Ha" is the
            # flat a light shot through "H" tonight is looking for.
            from .filters import canonical
            meta["filter"] = canonical(meta["filter"]) or meta["filter"]
        if kind in FILTERED_TYPES and not meta["filter"]:
            raise DeviceError(f"a master {kind} needs a filter name - the file "
                              "does not carry one, so type it in")

        frame = _as_adu(values)
        info = {"frames": int(seen.get("frames") or 1),
                "method": seen.get("method") or "imported"}
        return self.store(kind, frame, meta, info,
                          extra={"IMPORTED": (source.name[:66], "brought in from")})

    # -- writing one -------------------------------------------------------
    def store(self, kind: str, frame: np.ndarray, meta: dict[str, Any],
              info: dict[str, Any], extra: dict[str, Any] | None = None
              ) -> dict[str, Any]:
        """Write a freshly built master into the library and describe it."""
        header = {
            "IMAGETYP": (f"Master {kind}", "master calibration frame"),
            "EXPTIME": (float(meta.get("exposure") or 0.0), "seconds"),
            "EXPOSURE": (float(meta.get("exposure") or 0.0), "seconds"),
            "XBINNING": (int(meta.get("binning") or 1), ""),
            "YBINNING": (int(meta.get("binning") or 1), ""),
            "GAIN": (None if meta.get("gain") is None else int(meta["gain"]), ""),
            "OFFSET": (None if meta.get("offset") is None else int(meta["offset"]), ""),
            "CCD-TEMP": (meta.get("temperature"), "mean sensor temperature in C"),
            "FILTER": (meta.get("filter") or None, ""),
            "TELESCOP": (meta.get("telescope") or None, ""),
            "INSTRUME": (meta.get("camera") or None, "camera"),
            "NCOMBINE": (int(info.get("frames") or 0), "frames stacked"),
            "STACKMTH": (info.get("method") or "", "how they were combined"),
            "DATE-OBS": (fits.utc_now(), "UTC the master was built"),
            "SWCREATE": ("Starfront", ""),
        }
        header.update(extra or {})
        path = self.masters_dir / master_filename(kind, meta)
        fits.write(path, to_uint16(frame), header)
        self.forget()
        described = describe(path, fits.read_header(path))
        return described or {"id": path.stem, "path": str(path), "type": kind}

    # -- reporting ---------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        found = self.masters()
        by_type: dict[str, int] = {}
        for master in found:
            by_type[master["type"]] = by_type.get(master["type"], 0) + 1
        total = sum(m["sizeBytes"] for m in found)
        writable = True
        with contextlib.suppress(OSError):
            self.masters_dir.mkdir(parents=True, exist_ok=True)
        if not self.masters_dir.is_dir():
            writable = False
        return {
            "root": str(self.root),
            "mastersDir": str(self.masters_dir),
            "subsDir": str(self.subs_dir),
            "writable": writable,
            "counts": by_type,
            "total": len(found),
            "bytes": total,
            "masters": found,
            "settings": self.settings(),
        }


def read_master_file(path: str | Path) -> tuple[np.ndarray, dict[str, Any]]:
    """A master from disk, FITS or XISF, as unclipped values and a header."""
    path = Path(path)
    if xisf.is_xisf(path):
        frame, header = xisf.read(path)
        return frame.astype(np.float64), header
    try:
        return fits.read_values(path)
    except (ValueError, KeyError) as exc:
        raise DeviceError(f"{path.name} is not a FITS or XISF file ({exc})") from exc


def inspect_master_file(path: str | Path) -> dict[str, Any]:
    """What a master file says about itself, for the import form to prefill."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise DeviceError(f"{path} is not a file")
    if xisf.is_xisf(path):
        header = xisf.read_header(path)
        fmt = "XISF " + str(header.get("XISF_FORMAT") or "")
    else:
        try:
            header = fits.read_header(path)
        except (ValueError, OSError) as exc:
            raise DeviceError(f"{path.name} is not a FITS or XISF file ({exc})") from exc
        fmt = f"FITS BITPIX {header.get('BITPIX', '?')}"
    described = describe(path, header) or {}
    kind = described.get("type") or ""
    return {
        "path": str(path),
        "name": path.name,
        "format": fmt,
        "type": kind,
        "exposure": _number(header.get("EXPTIME")) or _number(header.get("EXPOSURE")) or 0.0,
        "binning": int(_number(header.get("XBINNING")) or 1),
        "gain": _number(header.get("GAIN")),
        "offset": _number(header.get("OFFSET")),
        "temperature": _number(header.get("CCD-TEMP")),
        "filter": _text(header.get("FILTER")),
        "telescope": _text(header.get("TELESCOP")),
        "camera": _text(header.get("INSTRUME")),
        "width": int(_number(header.get("NAXIS1")) or 0),
        "height": int(_number(header.get("NAXIS2")) or 0),
        "frames": int(_number(header.get("NCOMBINE")) or 0),
        "software": _text(header.get("SWCREATE") or header.get("CREATOR")
                          or header.get("PROGRAM")),
    }


def _as_adu(values: np.ndarray) -> np.ndarray:
    """Pixels as 16-bit ADU, whatever range the file kept them in.

    A float master in 0-1 is PixInsight's normalised form and is scaled up
    by 65535; anything already in ADU is left alone. The boundary is 1.0
    plus a little: a real ADU master never has every pixel under 1.
    """
    data = np.asarray(values, dtype=np.float64)
    top = float(np.nanmax(data)) if data.size else 0.0
    if 0.0 < top <= 1.0001:
        data = data * 65535.0
    return data


def _pick(*candidates: Any) -> Any:
    """The first value that is actually there ("" and None are not)."""
    for value in candidates:
        if value is not None and value != "":
            return value
    return None


def _same(stored: Any, wanted: Any) -> bool:
    """Whether a master's gain or offset matches the frame being calibrated.

    A master that never recorded the value matches anything: plenty of drivers
    do not report gain, and refusing every one of those masters would leave the
    library unusable on exactly the cameras that need it most.
    """
    left, right = _number(stored), _number(wanted)
    if left is None or right is None:
        return True
    return math.isclose(left, right, abs_tol=0.5)
