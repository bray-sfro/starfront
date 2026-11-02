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


class SurveyImages:
    """Fetches cutouts, and keeps the last few hundred megabytes of them."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or (data_root() / "survey-cache")
        self._lock = threading.Lock()

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
               hips: str = "CDS/P/DSS2/color", timeout: float = 60.0) -> tuple[bytes, bool]:
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

        query = urllib.parse.urlencode({
            "hips": hips,
            "ra": f"{ra_deg:.6f}",
            "dec": f"{dec_deg:.6f}",
            "fov": f"{fov_deg:.6f}",
            "width": width,
            "height": height,
            "rotation_angle": f"{rotation:.3f}",
            "projection": "TAN",
            "coordsys": "icrs",
            "format": "jpg",
        })
        request = urllib.request.Request(f"{ENDPOINT}?{query}",
                                         headers={"User-Agent": "Starfront"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise DeviceError(
                f"the survey service refused that request (HTTP {exc.code}). "
                "Try a smaller field or a different survey.") from exc
        except Exception as exc:                   # noqa: BLE001 - offline is normal
            raise DeviceError(
                f"could not reach the survey service: {exc}. "
                "Survey images need an internet connection; the star chart does not."
            ) from exc

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

    def cache_status(self) -> dict[str, object]:
        try:
            files = list(self.cache_dir.glob("*.jpg"))
        except OSError:
            return {"images": 0, "bytes": 0}
        return {"images": len(files),
                "bytes": sum(path.stat().st_size for path in files),
                "path": str(self.cache_dir)}
