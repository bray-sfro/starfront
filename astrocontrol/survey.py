"""Survey image cutouts for the framing planner.

CDS `hips2fits` renders a single image from any HiPS survey for coordinates, a
field size and a rotation we choose.  That is a much better fit here than a
streaming tile layer: one request gives one picture with exactly the geometry
the planner is drawing on, and it can be cached to disk so a framing you have
already looked at still opens with the network unplugged.

Requests are proxied through the application rather than fetched by the page so
that the cache exists at all, and so a slow or missing CDS does not turn into an
opaque browser error.
"""

from __future__ import annotations

import hashlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import data_root
from .devices.base import DeviceError

ENDPOINT = "https://alasky.cds.unistra.fr/hips-image-services/hips2fits"

# Surveys worth offering for framing: wide optical coverage first, then the
# narrowband and infrared ones that show structure DSS misses.
SURVEYS = (
    {"id": "CDS/P/DSS2/color", "name": "DSS2 colour", "note": "whole sky, good default"},
    {"id": "CDS/P/DSS2/red", "name": "DSS2 red", "note": "deeper on nebulosity"},
    {"id": "CDS/P/SDSS9/color", "name": "SDSS9 colour", "note": "sharper, northern sky only"},
    {"id": "CDS/P/2MASS/color", "name": "2MASS infrared", "note": "sees through dust"},
    {"id": "CDS/P/AllWISE/color", "name": "AllWISE infrared", "note": "warm dust structure"},
    {"id": "CDS/P/Finkbeiner", "name": "H-alpha (Finkbeiner)", "note": "emission nebulae"},
    {"id": "CDS/P/GALEXGR6/AIS/color", "name": "GALEX ultraviolet", "note": "hot young stars"},
)
SURVEY_IDS = {survey["id"] for survey in SURVEYS}

MAX_PIXELS = 4000
CACHE_LIMIT_BYTES = 400 * 1024 * 1024
#: After the survey service cannot be reached, how long every new cutout
#: fails at once instead of trying again. The page fetches a picture per
#: collaboration and per target, and the window's browser gives this program
#: six connections: six cutouts each waiting out a dead host's connect
#: timeout held all six, and every click behind them (join, sign out, ...)
#: waited too (2026-11-03, CDS down).
DOWN_SECONDS = 120.0
#: Connect/read timeout for one cutout.
FETCH_TIMEOUT = 12.0


def _hips2fits(endpoint: str):
    def build(hips, ra, dec, fov, width, height, rotation):
        return endpoint + "?" + urllib.parse.urlencode({
            "hips": hips, "ra": f"{ra:.6f}", "dec": f"{dec:.6f}", "fov": f"{fov:.6f}",
            "width": width, "height": height, "rotation_angle": f"{rotation:.3f}",
            "projection": "TAN", "coordsys": "icrs", "format": "jpg"})
    return build


#: NASA SkyView names for the surveys above (a fallback: monochrome DSS2 red
#: for the colour default, which is fine for framing and thumbnails).
SKYVIEW = {
    "CDS/P/DSS2/color": "DSS2 Red", "CDS/P/DSS2/red": "DSS2 Red",
    "CDS/P/SDSS9/color": "SDSSr", "CDS/P/2MASS/color": "2MASS-K",
    "CDS/P/AllWISE/color": "WISE 12", "CDS/P/Finkbeiner": "H-Alpha Comp",
    "CDS/P/GALEXGR6/AIS/color": "GALEX Near UV",
}


def _skyview(hips, ra, dec, fov, width, height, rotation):
    survey = SKYVIEW.get(hips)
    if survey is None:
        return None
    return "https://skyview.gsfc.nasa.gov/current/cgi/runquery.pl?" + urllib.parse.urlencode({
        "Survey": survey, "Position": f"{ra:.6f},{dec:.6f}", "Coordinates": "J2000",
        "Projection": "Tan", "Size": f"{fov:.6f},{fov * height / width:.6f}",
        "Pixels": f"{width},{height}", "Rotation": f"{rotation:.3f}",
        "Scaling": "Log", "Return": "JPEG"})


#: Where cutouts come from, in order. The first is the one every cached file
#: was made from; the rest are only asked when it fails.
MIRRORS = (
    {"name": "alasky", "url": _hips2fits(ENDPOINT), "timeout": 4.0},
    {"name": "alaskybis", "url": _hips2fits(
        "https://alaskybis.cds.unistra.fr/hips-image-services/hips2fits"), "timeout": 4.0},
    {"name": "skyview", "url": _skyview, "timeout": 8.0},
)


class SurveyDown(DeviceError):
    """The survey service failed a moment ago; answered at once, as a 503."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after + 0.999))


class SurveyImages:
    """Fetches cutouts, and keeps the last few hundred megabytes of them."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or (data_root() / "survey-cache")
        self._lock = threading.Lock()
        self._down_until = 0.0
        self._down_reason = ""
        self._mirror_down: dict[str, float] = {}

    # -- cache -------------------------------------------------------------
    def _key(self, **parameters: object) -> str:
        canonical = "&".join(f"{k}={parameters[k]}" for k in sorted(parameters))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    def _prune(self) -> None:
        """Drop the least recently used files once the cache gets large."""
        try:
            files = sorted(self.cache_dir.glob("*.jpg"), key=lambda p: p.stat().st_atime)
        except OSError:
            return
        total = sum(path.stat().st_size for path in files)
        while total > CACHE_LIMIT_BYTES and files:
            victim = files.pop(0)
            try:
                total -= victim.stat().st_size
                victim.unlink()
            except OSError:
                break

    # -- fetching ----------------------------------------------------------
    def cutout(self, ra_deg: float, dec_deg: float, fov_deg: float,
               width: int, height: int, rotation: float = 0.0,
               hips: str = "CDS/P/DSS2/color", timeout: float = FETCH_TIMEOUT) -> tuple[bytes, bool]:
        """Return (jpeg bytes, came_from_cache).

        `fov_deg` is the width of the image on the sky.  `rotation` turns the
        image so that a framing drawn at a position angle can be shown the way
        the camera will actually see it.
        """
        if hips not in SURVEY_IDS:
            raise DeviceError(f"unknown survey {hips!r}")
        if not 0.001 <= fov_deg <= 90.0:
            raise DeviceError("field of view must be between 0.001 and 90 degrees")
        width = max(64, min(MAX_PIXELS, int(width)))
        height = max(64, min(MAX_PIXELS, int(height)))

        key = self._key(ra=round(ra_deg, 5), dec=round(dec_deg, 5),
                        fov=round(fov_deg, 5), w=width, h=height,
                        rot=round(rotation, 2), hips=hips)
        path = self.cache_dir / f"{key}.jpg"
        if path.is_file():
            try:
                path.touch()                       # keep it fresh for the pruner
                return path.read_bytes(), True
            except OSError:
                pass

        left = self._down_until - time.monotonic()
        if left > 0:
            raise SurveyDown(f"could not reach the survey service: {self._down_reason}. "
                             "Trying again shortly; the star chart does not need it.", left)

        # Each mirror in turn, skipping any that failed in the last two
        # minutes; the whole attempt stays inside `timeout` so the page's own
        # 15 s limit is never reached (2026-11-03: alasky and alaskybis were
        # both down for hours, SkyView at NASA was not).
        deadline = time.monotonic() + timeout
        errors: list[str] = []
        payload = b""
        for mirror in MIRRORS:
            name = mirror["name"]
            if time.monotonic() < self._mirror_down.get(name, 0.0):
                errors.append(f"{name}: skipped (failed recently)")
                continue
            url = mirror["url"](hips, ra_deg, dec_deg, fov_deg, width, height, rotation)
            if url is None:
                continue                           # this mirror lacks that survey
            budget = deadline - time.monotonic()
            if budget < 1.0:
                errors.append(f"{name}: no time left")
                break
            request = urllib.request.Request(url, headers={"User-Agent": "Starfront"})
            try:
                with urllib.request.urlopen(request, timeout=min(budget, mirror["timeout"])) as response:
                    payload = response.read()
            except urllib.error.HTTPError as exc:
                if exc.code < 500 and mirror is MIRRORS[0]:
                    raise DeviceError(
                        f"the survey service refused that request (HTTP {exc.code}). "
                        "Try a smaller field or a different survey.") from exc
                self._mirror_down[name] = time.monotonic() + DOWN_SECONDS
                errors.append(f"{name}: HTTP {exc.code}")
                continue
            except Exception as exc:               # noqa: BLE001 - offline is normal
                self._mirror_down[name] = time.monotonic() + DOWN_SECONDS
                errors.append(f"{name}: {exc}")
                continue
            if payload.startswith(b"\xff\xd8"):
                break
            self._mirror_down[name] = time.monotonic() + DOWN_SECONDS
            errors.append(f"{name}: not an image")
            payload = b""
        if not payload:
            self._down_until = time.monotonic() + min(
                [DOWN_SECONDS] + [max(0.0, t - time.monotonic()) for t in self._mirror_down.values()])
            if self._down_until <= time.monotonic():
                self._down_until = time.monotonic() + DOWN_SECONDS
            self._down_reason = "; ".join(errors) or "no mirror has that survey"
            raise SurveyDown(
                f"could not reach the survey service ({self._down_reason}). "
                "Survey images need an internet connection; the star chart does not.",
                DOWN_SECONDS)

        if not payload or not payload.startswith(b"\xff\xd8"):
            raise DeviceError("the survey service returned something that is not an image")

        with self._lock:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                self._prune()
            except OSError:
                pass                               # a full disk is not a reason to fail
        return payload, False

    def down_status(self) -> dict[str, object]:
        left = self._down_until - time.monotonic()
        return {"down": left > 0, "retryAfter": max(0, round(left)),
                "reason": self._down_reason if left > 0 else ""}

    def cache_status(self) -> dict[str, object]:
        try:
            files = list(self.cache_dir.glob("*.jpg"))
        except OSError:
            return {"images": 0, "bytes": 0}
        return {"images": len(files),
                "bytes": sum(path.stat().st_size for path in files),
                "path": str(self.cache_dir)}
