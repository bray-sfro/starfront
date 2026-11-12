"""Which filter is in which telescope, found out from the stars.

A RASA has no filter wheel: the filter sits in a drawer at prime focus and is
changed by hand in the afternoon. Nothing in the camera, the driver or the FITS
header can tell which one went in - the program only knows what somebody last
typed into Equipment. On a mount carrying three of them the chance of one
drawer not matching the setting is real, and a night shot through the wrong
filter is a night lost to every stack it lands in.

What *can* be observed is the light itself. Every star in a frame has a
catalogued brightness and colour (Gaia DR3: G, and BP-RP for colour), and how
bright each star comes out through a filter depends on that filter in two ways
that are easy to measure and hard to fake:

* **The colour term.** Fit instrumental magnitude minus Gaia G against BP-RP.
  Through a blue filter red stars come out faint, so the slope is steep and
  positive (about +1); through green it is gentler (+0.4), through red it is
  slightly negative, and through luminance it is small. OIII behaves blue,
  H-alpha and SII red.
* **The throughput.** How much light gets through at all, at a fixed colour.
  A 3-7 nm narrowband filter passes a few per cent of what a broadband one
  does: three to four magnitudes fainter. Luminance passes the most.

The throughput is measured best *between* telescopes on one mount: their
frames are taken at the same moment through the same air, so a passing haze
dims them all alike and cancels out of the difference. That is why the check
wants a frame from every telescope at once.

The answer is a suggestion with numbers behind it. Nothing here changes the
filter setting: the Equipment dialog shows what was found next to what is set,
and offers a button. A frame records what was detected in its own keywords
(FILTDET, FILTCONF), never in FILTER.

The pipeline for one frame: plate solve (ASTAP, as for centring) -> Gaia stars
in the field (cached on disk; fetched from the ESA Gaia archive or a VizieR
mirror, with a short timeout and a back-off so a dead service cannot hold
anything up) -> forced aperture photometry at each catalogued position,
background from an annulus, saturated, crowded and faint stars dropped -> a
robust fit of (m_inst - G) = zp + k (BP-RP - 1).

NumPy only, like the rest of `imaging`.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import itertools
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from . import filters as filter_names

# ---------------------------------------------------------------------------
# What each filter does to starlight
# ---------------------------------------------------------------------------

#: The colour at which the zero point is quoted. Near the middle of the field
#: star population, so the zero point and the slope are nearly independent.
PIVOT_COLOUR = 1.0

#: Stars used for the fit: colours Gaia measures well and that every filter
#: here behaves smoothly across.
COLOUR_RANGE = (-0.1, 2.8)


@dataclass(frozen=True)
class FilterModel:
    """How one kind of filter shows up in the photometry.

    `slope` is d(m_inst - G)/d(BP-RP) at the pivot colour, with `slope_sigma`
    the spread expected between real filters of that kind (different makers'
    bandpasses, a different sensor) plus the measurement's own floor.

    `throughput` is the zero point at the pivot colour relative to a luminance
    filter on the same optics and camera, in magnitudes (positive is fainter),
    with `throughput_sigma` its spread. Narrowband depends heavily on the
    bandwidth (3 nm vs 7 nm is 0.9 mag), hence the wide sigma there.
    """

    letter: str
    name: str
    slope: float
    slope_sigma: float
    throughput: float
    throughput_sigma: float
    narrowband: bool = False


#: The seven filters `filters.canonical` knows. Starting points from the Gaia
#: DR3 colour transformations (Riello et al. 2021: V, Rc, g, r against BP-RP),
#: then set from Bray's own triple-RASA frames, measured with this module
#: (colour term k / brightness -zp, e-/s):
#:
#:   2026-08-27 (NINA labels B/R/G)   B +0.94/18.91  R -0.22/18.91  G +0.56/19.12
#:   2026-10-07 (same three, by eye)  B +1.04/19.21  R -0.07/19.06  G +0.63/19.34
#:   300 s narrowband, Markarian and
#:   SNR G206.9, all three RASAs      k -0.20 to -0.29, -zp 15.8 to 16.5
#:
#: Broadband B, R and G all land within 0.3 mag of each other on this rig, so
#: they are told apart by colour term; narrowband sits 3-4 mag below them, and
#: H-alpha and SII (k differs by ~0.1) mostly by the prior. No luminance frame
#: was available: L is the theoretical value.
MODELS: dict[str, FilterModel] = {m.letter: m for m in (
    FilterModel("L", "Luminance", slope=0.25, slope_sigma=0.15,
                throughput=0.0, throughput_sigma=0.35),
    FilterModel("R", "Red", slope=-0.15, slope_sigma=0.12,
                throughput=0.95, throughput_sigma=0.35),
    FilterModel("G", "Green", slope=0.58, slope_sigma=0.12,
                throughput=0.75, throughput_sigma=0.35),
    FilterModel("B", "Blue", slope=1.00, slope_sigma=0.15,
                throughput=0.90, throughput_sigma=0.35),
    FilterModel("H", "H-alpha", slope=-0.27, slope_sigma=0.12,
                throughput=3.9, throughput_sigma=0.8, narrowband=True),
    FilterModel("O", "OIII", slope=0.75, slope_sigma=0.20,
                throughput=4.3, throughput_sigma=0.8, narrowband=True),
    FilterModel("S", "SII", slope=-0.35, slope_sigma=0.15,
                throughput=4.3, throughput_sigma=0.8, narrowband=True),
)}

#: How likely each filter is to be in a drawer at all, before looking. SII is
#: the rarest; it is also the one hardest to tell from H-alpha (their colour
#: terms differ by about 0.1), so without this a coin flip between them would
#: come out SII half the time.
PRIOR = {"L": 1.0, "R": 1.0, "G": 1.0, "B": 1.0, "H": 1.0, "O": 1.0, "S": 0.6}

#: Throughput differences between telescopes on one mount, beyond what the
#: filter models already allow: unequal dew on a corrector, a slightly
#: different sensor, collimation. Between simultaneous frames the sky cancels,
#: so this is small.
RELATIVE_SIGMA = 0.30

#: The absolute zero point of a luminance filter is only known once a check has
#: been confirmed on this telescope (see `reference_from`). Until then this
#: default stands in for an 8" f/2 with a modern CMOS sensor (measured on
#: Bray's RASAs with an ASI2600MM), in magnitudes of
#: (electrons per second for a G=0, BP-RP=1 star), electrons approximated as
#: ADU x 10^(-gain/200). Wide, because transparency and airmass move it.
DEFAULT_REFERENCE_ZP = 19.9
REFERENCE_SIGMA = 0.8
REFERENCE_APERTURE_MM = 203.0

#: Scatter of a clean fit about its line, magnitudes. Above it, sigmas grow.
ROUGH_SCATTER = 0.12


# ---------------------------------------------------------------------------
# Gaia
# ---------------------------------------------------------------------------

class CatalogUnavailable(RuntimeError):
    """No catalogue service answered, and nothing cached covers the field."""


@dataclass
class Stars:
    ra: np.ndarray
    dec: np.ndarray
    g: np.ndarray
    colour: np.ndarray            # BP-RP
    source: str = ""

    def __len__(self) -> int:
        return int(self.ra.size)


def _esa_tap(ra: float, dec: float, radius: float, limit: float, rows: int) -> str:
    query = (
        f"SELECT TOP {rows} ra, dec, phot_g_mean_mag, bp_rp FROM gaiadr3.gaia_source "
        f"WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra:.5f}, {dec:.5f}, {radius:.4f})) "
        f"AND phot_g_mean_mag < {limit:.2f}")
    return ("https://gea.esac.esa.int/tap-server/tap/sync?" + urllib.parse.urlencode(
        {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": query}))


def _vizier(host: str):
    def build(ra: float, dec: float, radius: float, limit: float, rows: int) -> str:
        return (f"https://{host}/viz-bin/asu-tsv?" + urllib.parse.urlencode({
            "-source": "I/355/gaiadr3", "-c": f"{ra:.5f} {dec:+.5f}",
            "-c.rd": f"{radius:.4f}", "-out": "RA_ICRS DE_ICRS Gmag BP-RP",
            "Gmag": f"<{limit:.2f}", "-out.max": str(rows), "-sort": "Gmag"}))
    return build


#: Tried in order. The ESA archive is Gaia's own home; the VizieR mirrors are
#: there for when it is not answering. CDS is last: on 2026-11-03 it was the
#: service that was down.
CATALOG_SOURCES: tuple[tuple[str, Callable[..., str]], ...] = (
    ("ESA Gaia archive", _esa_tap),
    ("VizieR (CfA mirror)", _vizier("vizier.cfa.harvard.edu")),
    ("VizieR (CDS)", _vizier("vizier.cds.unistra.fr")),
)

#: Per request. Short, so a dead service costs seconds rather than minutes.
FETCH_TIMEOUT = 40.0
#: After a source fails, how long it is skipped.
DOWN_SECONDS = 120.0


def _parse_rows(text: str) -> Stars:
    """ESA's CSV or VizieR's TSV, to arrays. Rows without a colour are dropped."""
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    if not lines:
        raise ValueError("empty answer")
    delimiter = "," if "," in lines[0] and "\t" not in lines[0] else "\t"
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    header = [h.strip().lower() for h in next(reader)]

    def column(*names: str) -> int:
        for name in names:
            if name in header:
                return header.index(name)
        raise ValueError(f"no {names[0]} column in {header}")

    ira, idec = column("ra", "ra_icrs"), column("dec", "de_icrs")
    ig, ic = column("phot_g_mean_mag", "gmag"), column("bp_rp", "bp-rp")
    ra, dec, g, c = [], [], [], []
    for row in reader:
        try:
            values = (float(row[ira]), float(row[idec]), float(row[ig]), float(row[ic]))
        except (ValueError, IndexError):
            continue                       # VizieR's unit and dash lines, blank colours
        if all(math.isfinite(v) for v in values):
            ra.append(values[0]); dec.append(values[1]); g.append(values[2]); c.append(values[3])
    if not ra and len(header) < 4:
        raise ValueError("unexpected answer")
    return Stars(np.array(ra), np.array(dec), np.array(g), np.array(c))


class GaiaCatalog:
    """Gaia stars around a point, from disk if they have been fetched before.

    A capture PC points at the same handful of targets for weeks, so nearly
    every check after the first is answered from the cache with the network
    unplugged. A cached cone answers any smaller cone inside it.
    """

    def __init__(self, cache_dir: Path | None = None,
                 fetch: Callable[[str, float], str] | None = None) -> None:
        if cache_dir is None:
            from .config import data_root
            cache_dir = data_root() / "gaia-cache"
        self.cache_dir = Path(cache_dir)
        self._fetch = fetch or self._http
        self._down: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _http(url: str, timeout: float) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "Starfront filter check"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    # -- cache -------------------------------------------------------------
    def _index(self) -> list[dict[str, Any]]:
        path = self.cache_dir / "index.json"
        with contextlib.suppress(OSError, ValueError):
            return list(json.loads(path.read_text("utf-8")))
        return []

    def _cached(self, ra: float, dec: float, radius: float, limit: float) -> Stars | None:
        for entry in self._index():
            if entry["limit"] + 1e-6 < limit:
                continue
            if _separation(ra, dec, entry["ra"], entry["dec"]) + radius > entry["radius"] + 1e-6:
                continue
            with contextlib.suppress(OSError, ValueError, KeyError):
                data = np.load(self.cache_dir / entry["file"])
                stars = Stars(data["ra"], data["dec"], data["g"], data["colour"],
                              source=f"cache ({entry.get('source', '')})")
                keep = (stars.g < limit) & (_separation(ra, dec, stars.ra, stars.dec) <= radius)
                return Stars(stars.ra[keep], stars.dec[keep], stars.g[keep],
                             stars.colour[keep], stars.source)
        return None

    def _store(self, ra: float, dec: float, radius: float, limit: float,
               stars: Stars) -> None:
        with contextlib.suppress(OSError):
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha1(f"{ra:.4f},{dec:.4f},{radius:.3f},{limit:.2f}".encode()
                                ).hexdigest()[:16] + ".npz"
            np.savez_compressed(self.cache_dir / name, ra=stars.ra, dec=stars.dec,
                                g=stars.g, colour=stars.colour)
            index = [e for e in self._index() if e.get("file") != name]
            index.append({"ra": ra, "dec": dec, "radius": radius, "limit": limit,
                          "file": name, "source": stars.source, "rows": len(stars),
                          "fetched": time.time()})
            (self.cache_dir / "index.json").write_text(json.dumps(index[-500:]), "utf-8")

    # -- the query ---------------------------------------------------------
    def stars(self, ra: float, dec: float, radius: float, limit: float = 14.0,
              rows: int = 60000) -> Stars:
        """Gaia stars brighter than `limit` within `radius` degrees.

        The cache first; then each service in turn, skipping any that failed
        in the last two minutes. Raises `CatalogUnavailable` when nothing can
        answer, saying why for each.
        """
        found = self._cached(ra, dec, radius, limit)
        if found is not None:
            return found
        # Fetch a slightly larger cone than asked, so the next frame on the same
        # target - a few arcminutes off after a dither or a meridian flip - is a
        # cache hit. Only slightly: the archive's time grows much faster than
        # the area in a crowded field (0.6 deg at Sh2-132 to G 15.7: 4 s;
        # 0.8 deg: timed out at 60 s).
        wide = radius * 1.08 + 0.03
        reasons = []
        for name, build in CATALOG_SOURCES:
            with self._lock:
                until = self._down.get(name, 0.0)
            if time.monotonic() < until:
                reasons.append(f"{name}: skipped, failed {DOWN_SECONDS - (until - time.monotonic()):.0f}s ago")
                continue
            # A service that times out on the whole cone is asked once more
            # for the middle of it: fewer stars, still hundreds.
            cones = [wide] + ([radius * SHRINK] if radius * SHRINK >= MIN_CONE else [])
            stars, cone, failure = None, wide, None
            for cone in cones:
                try:
                    stars = _parse_rows(self._fetch(build(ra, dec, cone, limit, rows), FETCH_TIMEOUT))
                    break
                except Exception as exc:             # noqa: BLE001 - any failure moves on
                    failure = exc
                    if not _slow(exc):
                        break
            if stars is None:
                with self._lock:
                    self._down[name] = time.monotonic() + DOWN_SECONDS
                reasons.append(f"{name}: {failure}")
                continue
            stars.source = name
            self._store(ra, dec, cone, limit, stars)
            keep = _separation(ra, dec, stars.ra, stars.dec) <= min(radius, cone)
            return Stars(stars.ra[keep], stars.dec[keep], stars.g[keep],
                         stars.colour[keep], name)
        raise CatalogUnavailable("no star catalogue could be reached: " + "; ".join(reasons))


#: The second, smaller cone after a timeout, as a fraction of the first.
SHRINK = 0.55
MIN_CONE = 0.3


def _slow(exc: BaseException) -> bool:
    """A failure that a smaller query might not have: a timeout, or the
    archive giving up on the query (HTTP 408/504, an empty read)."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(word in text for word in ("timed out", "timeout", "408", "504", "incompleteread"))


def _separation(ra1: Any, dec1: Any, ra2: Any, dec2: Any) -> Any:
    """Degrees between points, vectorised."""
    r1, d1, r2, d2 = (np.radians(np.asarray(v, dtype=np.float64)) for v in (ra1, dec1, ra2, dec2))
    cos = np.sin(d1) * np.sin(d2) + np.cos(d1) * np.cos(d2) * np.cos(r1 - r2)
    out = np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))
    return float(out) if np.ndim(out) == 0 else out


# ---------------------------------------------------------------------------
# Photometry
# ---------------------------------------------------------------------------

#: Signal-to-noise below which a star is not used.
MIN_SNR = 15.0
#: Fewer stars than this and the fit is not trusted at all.
MIN_STARS = 8
#: Pixels kept clear of the frame's edge.
EDGE = 24


def gain_factor(header: dict[str, Any]) -> float:
    """ADU to (approximately) electrons, up to one constant per camera model.

    ZWO and most CMOS drivers step gain in 0.1 dB, so the conversion at gain g
    is the one at gain 0 times 10^(-g/200). That constant is the same for three
    identical cameras, which is all a comparison between them needs; it is
    also what lets one telescope shoot at gain 0 and another at 100 (as on
    2026-10-07) and still be compared.
    """
    gain = header.get("GAIN")
    try:
        return 10.0 ** (-float(gain) / 200.0) if gain is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def saturation_level(image: np.ndarray) -> float:
    top = float(image.max()) if image.size else 65535.0
    # A frame whose brightest pixel is far below full scale has nothing
    # saturated; one that hits a ceiling many times has found its ceiling.
    if top >= 60000 or int((image >= top).sum()) > 8:
        return 0.85 * top
    return 0.85 * 65535.0


def photometry(image: np.ndarray, wcs: Any, catalog: Stars,
               hfr: float | None = None, edge: int = EDGE) -> dict[str, np.ndarray]:
    """Forced aperture photometry at every catalogued star in the frame.

    Positions come from the plate solution and are refined on the star itself
    (a few pixels at most: lens distortion, proper motion since 2016). Each
    star's sky is the median of an annulus around it. Stars that are
    saturated, too near a brighter neighbour, or whose centroid wandered off
    (a mismatch, a blend) are dropped.

    Returns arrays: g, colour, flux (ADU), error, snr, x, y (zero-based).
    """
    image = np.asarray(image)
    height, width = image.shape
    x1, y1, good = wcs.to_pixel(catalog.ra, catalog.dec)
    x, y = x1 - 1.0, y1 - 1.0
    inside = good & (x > edge) & (x < width - 1 - edge) & (y > edge) & (y < height - 1 - edge)
    empty = {k: np.array([]) for k in ("g", "colour", "flux", "error", "snr", "x", "y")}
    if not inside.any():
        return empty

    radius = float(np.clip(3.0 * (hfr or 2.0), 4.0, 14.0))
    inner, outer = radius + 3.0, radius + 10.0
    box = int(math.ceil(outer)) + 1

    order = np.where(inside)[0]
    xs, ys, gs = x[order], y[order], catalog.g[order]
    # Every catalogued star in the frame, for the crowding test - including
    # those too faint to measure, which still put light in an aperture.
    neighbours_x, neighbours_y, neighbours_g = x[good], y[good], catalog.g[good]
    cell = 64.0
    grid: dict[tuple[int, int], list[int]] = {}
    for index, (px, py) in enumerate(zip(neighbours_x, neighbours_y)):
        if -200 < px < width + 200 and -200 < py < height + 200:
            grid.setdefault((int(px // cell), int(py // cell)), []).append(index)

    saturated_at = saturation_level(image)
    yy, xx = np.mgrid[-box:box + 1, -box:box + 1]

    out: dict[str, list[float]] = {k: [] for k in empty}
    for sx, sy, sg, index in zip(xs, ys, gs, order):
        # Crowding: a neighbour near the aperture that is not more than 3
        # magnitudes fainter, one in the sky annulus about as bright, and any
        # very bright star whose halo reaches this far.
        cx, cy = int(sx // cell), int(sy // cell)
        crowded = False
        for gx in (cx - 4, cx - 3, cx - 2, cx - 1, cx, cx + 1, cx + 2, cx + 3, cx + 4):
            for gy in (cy - 4, cy - 3, cy - 2, cy - 1, cy, cy + 1, cy + 2, cy + 3, cy + 4):
                for other in grid.get((gx, gy), ()):
                    ox, oy, og = neighbours_x[other], neighbours_y[other], neighbours_g[other]
                    distance = math.hypot(ox - sx, oy - sy)
                    if distance < 0.5:
                        continue
                    # In the aperture (or its edge) a neighbour 3 mag fainter
                    # still adds 6%; in the sky annulus the median shrugs off
                    # anything but one about as bright as the star itself.
                    if distance < radius * 1.6 and og < sg + 3.0:
                        crowded = True
                    elif distance < outer + radius and og < sg + 0.75:
                        crowded = True
                    elif og < sg - 2.0 and distance < 12.0 * 10 ** (0.2 * (9.0 - og)):
                        crowded = True
                    if crowded:
                        break
                if crowded:
                    break
            if crowded:
                break
        if crowded:
            continue

        px, py = int(round(sx)), int(round(sy))
        if px - box < 0 or py - box < 0 or px + box >= width or py + box >= height:
            continue
        patch = image[py - box:py + box + 1, px - box:px + box + 1].astype(np.float64)
        r = np.hypot(xx - (sx - px), yy - (sy - py))
        ring = patch[(r >= inner) & (r <= outer)]
        if ring.size < 20:
            continue
        sky = float(np.median(ring))
        noise = float(np.median(np.abs(ring - sky))) * 1.4826
        # Refine the centre on the star: flux-weighted, within 4 px of where the
        # solution put it. Twice, the second from the first's answer.
        fx, fy = sx - px, sy - py
        for _ in range(2):
            near = np.hypot(xx - fx, yy - fy) <= max(4.0, radius * 0.6)
            weights = np.where(near, patch - sky, 0.0)
            weights[weights < 2.0 * max(noise, 1e-3)] = 0.0
            total = weights.sum()
            if total <= 0:
                break
            fx, fy = float((weights * xx).sum() / total), float((weights * yy).sum() / total)
        if math.hypot(fx - (sx - px), fy - (sy - py)) > 4.0:
            continue                        # not the catalogued star, or a blend
        r = np.hypot(xx - fx, yy - fy)
        aperture = r <= radius
        values = patch[aperture]
        if values.max() >= saturated_at:
            continue
        flux = float((values - sky).sum())
        n = int(aperture.sum())
        # Poisson on the star (in ADU, so divided by an assumed gain of one
        # electron per ADU's worth of the camera's scale) plus sky noise in the
        # aperture plus the error of the sky estimate itself.
        variance = max(flux, 0.0) + n * noise ** 2 + (n ** 2) * (noise ** 2) * 1.57 / ring.size
        error = math.sqrt(max(variance, 1e-9))
        if flux <= 0:
            continue
        out["g"].append(float(sg))
        out["colour"].append(float(catalog.colour[index]))
        out["flux"].append(flux)
        out["error"].append(error)
        out["snr"].append(flux / error)
        out["x"].append(px + fx)
        out["y"].append(py + fy)
    return {k: np.array(v, dtype=np.float64) for k, v in out.items()}


@dataclass
class ColourFit:
    """(m_inst - G) = zp + slope (BP-RP - pivot), fitted robustly."""

    zp: float
    slope: float
    zp_error: float
    slope_error: float
    scatter: float
    stars: int
    colour_low: float
    colour_high: float
    curvature: float = 0.0
    slope_quadratic: float = 0.0

    def payload(self) -> dict[str, Any]:
        return {"zp": round(self.zp, 3), "slope": round(self.slope, 3),
                "zpError": round(self.zp_error, 3), "slopeError": round(self.slope_error, 3),
                "scatter": round(self.scatter, 3), "stars": self.stars,
                "colourRange": [round(self.colour_low, 2), round(self.colour_high, 2)],
                "curvature": round(self.curvature, 3)}


def fit_colour(magnitude: np.ndarray, g: np.ndarray, colour: np.ndarray,
               error: np.ndarray, pivot: float = PIVOT_COLOUR) -> ColourFit | None:
    """The robust straight line through (BP-RP, m_inst - G).

    Weighted least squares, then clipped at 3 sigma on the scatter (variable
    stars, unresolved doubles, a cosmic ray in an aperture) until nothing more
    goes. A floor of 0.02 mag on every star's error stops a few bright stars
    from owning the fit.
    """
    use = (np.isfinite(magnitude) & (colour >= COLOUR_RANGE[0]) & (colour <= COLOUR_RANGE[1]))
    if use.sum() < MIN_STARS:
        return None
    y = (magnitude - g)[use]
    x = colour[use] - pivot
    sigma = np.hypot(error[use], 0.02)
    keep = np.ones(y.size, dtype=bool)
    for _ in range(8):
        if keep.sum() < MIN_STARS:
            return None
        w = 1.0 / sigma[keep] ** 2
        design = np.vstack([np.ones(keep.sum()), x[keep]]).T
        normal = design.T @ (design * w[:, None])
        try:
            coeffs = np.linalg.solve(normal, design.T @ (w * y[keep]))
        except np.linalg.LinAlgError:
            return None
        residual = y - (coeffs[0] + coeffs[1] * x)
        spread = 1.4826 * float(np.median(np.abs(residual[keep] - np.median(residual[keep]))))
        limit = 3.0 * math.hypot(spread, 0.01)
        new = np.abs(residual) <= np.maximum(limit, 3.0 * sigma)
        if np.array_equal(new, keep):
            break
        keep = new
    n = int(keep.sum())
    if n < MIN_STARS:
        return None
    # Errors from the scatter actually seen, which includes everything the
    # per-star errors leave out (flat field, the aperture's sampling).
    chi = residual[keep] / sigma[keep]
    scale = max(1.0, float(np.sqrt(np.sum(chi ** 2) / max(1, n - 2))))
    covariance = np.linalg.inv(normal) * scale ** 2
    curvature = 0.0
    slope_q = float(coeffs[1])
    if n >= 3 * MIN_STARS and np.ptp(x[keep]) > 1.0:
        with contextlib.suppress(Exception):
            q = np.polyfit(x[keep], y[keep], 2, w=1.0 / sigma[keep])
            curvature, slope_q = float(q[0]), float(q[1])
    return ColourFit(
        zp=float(coeffs[0]), slope=float(coeffs[1]),
        zp_error=float(math.sqrt(covariance[0, 0])),
        slope_error=float(math.sqrt(covariance[1, 1])),
        scatter=spread, stars=n,
        colour_low=float(np.min(colour[use][keep])), colour_high=float(np.max(colour[use][keep])),
        curvature=curvature, slope_quadratic=slope_q)


def instrumental(flux: np.ndarray, exposure: float, header: dict[str, Any]) -> np.ndarray:
    """-2.5 log10 of approximate electrons per second."""
    rate = np.asarray(flux, dtype=np.float64) * gain_factor(header) / max(float(exposure), 1e-6)
    with np.errstate(divide="ignore", invalid="ignore"):
        return -2.5 * np.log10(rate)


def measure(image: np.ndarray, header: dict[str, Any], wcs: Any, catalog: Stars,
            exposure: float | None = None, hfr: float | None = None) -> dict[str, Any]:
    """One frame, all the way to its colour fit. Raises ValueError with a reason
    a person can act on when there is nothing to fit."""
    exposure = float(exposure if exposure is not None else header.get("EXPTIME") or 0.0)
    if exposure <= 0:
        raise ValueError("the frame does not say how long it was exposed")
    if hfr is None:
        with contextlib.suppress(Exception):
            from .imaging import stars as star_finder
            measured = star_finder.measure(image)
            hfr = measured.get("hfr")
    phot = photometry(image, wcs, catalog, hfr=hfr)
    good = phot["snr"] >= MIN_SNR
    if good.sum() < MIN_STARS:
        raise ValueError(f"only {int(good.sum())} catalogued stars could be measured "
                         f"(of {len(phot['snr'])} in the field) - clouds, a frame too "
                         "short, or out of focus")
    magnitude = instrumental(phot["flux"][good], exposure, header)
    error = 1.0857 / phot["snr"][good]
    fit = fit_colour(magnitude, phot["g"][good], phot["colour"][good], error)
    if fit is None:
        raise ValueError("the stars measured do not cover enough colours to fit")
    return {"fit": fit, "measured": int(good.sum()), "inField": int(len(phot["snr"])),
            "hfr": hfr, "exposure": exposure, "gainFactor": gain_factor(header),
            "photometry": {k: v[good] for k, v in phot.items()}}


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------

def _aperture_shift(aperture_mm: float | None) -> float:
    """How much brighter (negative) a telescope's zero point is than the
    reference 8 inch, from its aperture alone."""
    if not aperture_mm or aperture_mm <= 0:
        return 0.0
    return -5.0 * math.log10(float(aperture_mm) / REFERENCE_APERTURE_MM)


def classify(fits: dict[str, ColourFit], references: dict[str, float] | None = None,
             candidates: Iterable[str] | None = None,
             apertures: dict[str, float | None] | None = None) -> dict[str, dict[str, Any]]:
    """The likeliest filter behind each telescope, from all of them together.

    Every combination of filters over the telescopes is scored (seven filters,
    three telescopes: 343 combinations, nothing). A combination's score adds,
    for each telescope, how far its colour term is from the filter's and how
    far its zero point is from where that filter would put it on this
    telescope; and for each pair of telescopes, how far apart their zero points
    are against how far apart those two filters would put them. The pair terms
    are the strong ones: they come from frames taken at the same moment, so
    the sky drops out.

    Each telescope's answer is its filter's share of the total probability
    over all combinations, so a telescope whose answer depends on what the
    others are is not reported as more certain than it is.

    `references` holds, per telescope, the luminance-equivalent zero point
    measured on a night when its filter was confirmed. Without one, the
    default for an 8 inch stands in, with a wide sigma.
    """
    letters = [c for c in (candidates or MODELS) if c in MODELS]
    rigs = [r for r in fits if fits[r] is not None]
    if not rigs:
        return {}
    references = references or {}
    apertures = apertures or {}

    def single(rig: str, letter: str) -> float:
        model, fit = MODELS[letter], fits[rig]
        # A frame with a ragged fit (moonlight, cloud, a gradient) gets wider
        # sigmas, so it cannot be confidently wrong.
        rough = max(1.0, fit.scatter / ROUGH_SCATTER)
        chi = ((fit.slope - model.slope) ** 2
               / ((model.slope_sigma * rough) ** 2 + fit.slope_error ** 2))
        # Zero points in "brightness" form, -zp: magnitudes of e-/s for a G=0
        # star at the pivot colour, so larger is more light. A filter takes its
        # throughput off the telescope's luminance figure.
        if rig in references:
            luminance, sigma = references[rig], 0.35
        else:
            luminance = DEFAULT_REFERENCE_ZP - _aperture_shift(apertures.get(rig))
            sigma = REFERENCE_SIGMA
        chi += ((-fit.zp) - (luminance - model.throughput)) ** 2 / (
            sigma ** 2 + model.throughput_sigma ** 2 + fit.zp_error ** 2)
        return chi - 2.0 * math.log(PRIOR.get(letter, 1.0))

    singles = {(r, l): single(r, l) for r in rigs for l in letters}
    total = 0.0
    marginal = {r: {l: 0.0 for l in letters} for r in rigs}
    best_combo, best_score = None, math.inf
    for combo in itertools.product(letters, repeat=len(rigs)):
        score = sum(singles[(r, l)] for r, l in zip(rigs, combo))
        for (i, ri), (j, rj) in itertools.combinations(enumerate(rigs), 2):
            mi, mj = MODELS[combo[i]], MODELS[combo[j]]
            measured = fits[ri].zp - fits[rj].zp
            expected = mi.throughput - mj.throughput
            sigma2 = (RELATIVE_SIGMA ** 2 + 0.5 * (mi.throughput_sigma ** 2 + mj.throughput_sigma ** 2)
                      * (0.25 if not (mi.narrowband or mj.narrowband) else 1.0)
                      + fits[ri].zp_error ** 2 + fits[rj].zp_error ** 2)
            # Apertures differ: a bigger telescope is brighter.
            measured -= _aperture_shift(apertures.get(ri)) - _aperture_shift(apertures.get(rj))
            score += (measured - expected) ** 2 / sigma2
        weight = math.exp(-0.5 * score) if score < 1400 else 0.0
        total += weight
        for r, l in zip(rigs, combo):
            marginal[r][l] += weight
        if score < best_score:
            best_score, best_combo = score, combo
    out: dict[str, dict[str, Any]] = {}
    for index, rig in enumerate(rigs):
        if total <= 0:
            probabilities = {l: (1.0 if l == best_combo[index] else 0.0) for l in letters}
        else:
            probabilities = {l: marginal[rig][l] / total for l in letters}
        ranked = sorted(probabilities.items(), key=lambda item: -item[1])
        out[rig] = {
            "filter": ranked[0][0],
            "confidence": round(ranked[0][1], 3),
            "runnerUp": ranked[1][0] if len(ranked) > 1 else None,
            "runnerUpConfidence": round(ranked[1][1], 3) if len(ranked) > 1 else None,
            "jointBest": best_combo[index],
            "probabilities": {l: round(p, 4) for l, p in ranked},
        }
    return out


#: Below this the answer is shown but called a guess.
CONFIDENT = 0.80


def compare(detected: dict[str, Any], configured: str | None) -> dict[str, Any]:
    """The detected filter against what the telescope is set to."""
    setting = filter_names.canonical(configured or "")
    found = detected.get("filter")
    confidence = float(detected.get("confidence") or 0.0)
    if not setting:
        verdict, level = "unset", "info"
    elif setting not in MODELS:
        verdict, level = "unknown-setting", "info"
    elif setting == found:
        verdict, level = "match", "ok"
    elif confidence >= CONFIDENT:
        verdict, level = "mismatch", "warn"
    elif detected.get("probabilities", {}).get(setting, 0.0) >= 0.15:
        verdict, level = "uncertain", "info"
    else:
        verdict, level = "probable-mismatch", "warn"
    return {"configured": setting or None, "verdict": verdict, "level": level}


def reference_from(fit: ColourFit, letter: str) -> float:
    """The luminance-equivalent zero point a confirmed filter implies, for
    `classify(references=...)` on later nights."""
    return -fit.zp + MODELS[letter].throughput


# ---------------------------------------------------------------------------
# Running a check on the telescopes
# ---------------------------------------------------------------------------

#: Frames this far apart in time are still "the same moment" for comparing
#: throughput: the sky does not change much in a few minutes, and a sequence
#: on a three-camera mount starts its cameras within seconds of each other.
SIMULTANEOUS_SECONDS = 15 * 60
#: A frame older than this is not reused for a check started by hand.
REUSE_MAX_AGE = 45 * 60
#: Short exposure for a check that takes its own frames.
DEFAULT_EXPOSURE = 5.0


def catalog_limit(exposure: float) -> float:
    """How faint to ask Gaia for, for a frame this long.

    Deep enough to have hundreds of stars above the noise, not so deep that a
    galactic-plane field returns a hundred thousand rows: about G 12.3 at half
    a second, 13.5 at five, 15.7 at five minutes.
    """
    return float(np.clip(12.7 + 1.2 * math.log10(max(exposure, 0.05)), 11.5, 16.0))


def wcs_from_solution(values: dict[str, Any], width: int, height: int) -> Any:
    """A `Wcs` out of ASTAP's answer file (or any header that has one)."""
    from .imaging import wcs as wcs_module
    header: dict[str, Any] = {"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN"}
    for key in ("CRPIX1", "CRPIX2", "CRVAL1", "CRVAL2", "CD1_1", "CD1_2", "CD2_1",
                "CD2_2", "CDELT1", "CDELT2", "CROTA1", "CROTA2"):
        if values.get(key) not in (None, ""):
            with contextlib.suppress(TypeError, ValueError):
                header[key] = float(values[key])
    return wcs_module.from_header(header, width, height, allow_derived=False)


#: Long exposures reach so deep that a whole-field query in the Milky Way is
#: tens of thousands of rows and a minute of the archive's time. The middle of
#: the field has plenty of stars at that depth, so ask for that only.
DEEP_RADIUS = 1.2


#: ...and in the Milky Way itself, less still: 0.6 degrees to G 15.7 is still
#: five thousand stars at Sh2-132, and 1.2 degrees timed the archive out.
PLANE_RADIUS = 0.6
PLANE_LATITUDE = 20.0


def galactic_latitude(ra: float, dec: float) -> float:
    """Galactic latitude in degrees of an ICRS position (J2000 pole)."""
    pole_ra, pole_dec = math.radians(192.85948), math.radians(27.12825)
    r, d = math.radians(ra), math.radians(dec)
    sin_b = (math.sin(d) * math.sin(pole_dec)
             + math.cos(d) * math.cos(pole_dec) * math.cos(r - pole_ra))
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_b))))


def catalog_radius(wcs: Any, exposure: float) -> float:
    radius = field_radius(wcs)
    if exposure < 30:
        return radius
    ra, dec = wcs.centre
    if abs(galactic_latitude(ra, dec)) < PLANE_LATITUDE:
        return min(radius, PLANE_RADIUS)
    return min(radius, DEEP_RADIUS)


def field_radius(wcs: Any) -> float:
    """Degrees from the centre to the furthest corner, plus a margin."""
    ra, dec = wcs.centre
    corners = wcs.corners()
    return max(_separation(ra, dec, c[0], c[1]) for c in corners) + 0.05


def analyse_frame(image: np.ndarray, header: dict[str, Any], wcs: Any,
                  catalog: GaiaCatalog, exposure: float | None = None) -> dict[str, Any]:
    """Catalogue, photometry and fit for one solved frame."""
    exposure = float(exposure if exposure is not None else header.get("EXPTIME") or 0.0)
    ra, dec = wcs.centre
    stars = catalog.stars(ra, dec, catalog_radius(wcs, exposure), round(catalog_limit(exposure), 1))
    measured = measure(image, header, wcs, stars, exposure=exposure)
    measured["catalog"] = stars.source
    measured["catalogStars"] = len(stars)
    measured["centre"] = (round(ra, 4), round(dec, 4))
    return measured


def _refused_base() -> type:
    try:
        from .devices.base import DeviceError
        return DeviceError
    except Exception:                              # noqa: BLE001 - standalone use
        return RuntimeError


class CheckRefused(_refused_base()):  # type: ignore[misc]
    """A check that cannot start as asked; the API answers it with a 400."""


class References:
    """What each telescope's luminance zero point was, the last time its filter
    was known for certain: a check that agreed with the setting, or one whose
    answer was applied. Kept beside the settings, not in them - it is a
    measurement, and nothing reads it but this."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from .config import data_root
            path = data_root() / "filter-check.json"
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, dict[str, Any]]:
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict):
                return data
        return {}

    def zero_points(self) -> dict[str, float]:
        return {rig: float(entry["luminance"]) for rig, entry in self.load().items()
                if isinstance(entry, dict) and entry.get("luminance") is not None}

    def remember(self, rig_id: str, fit: ColourFit, letter: str) -> None:
        if letter not in MODELS or fit is None:
            return
        with self._lock:
            data = self.load()
            data[rig_id] = {"luminance": round(reference_from(fit, letter), 3),
                            "filter": letter, "when": time.time(),
                            "slope": round(fit.slope, 3)}
            with contextlib.suppress(OSError):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(data, indent=1), "utf-8")


class FilterCheck:
    """The check, on a thread of its own, watched through `status()`.

    Two ways in:

    * `start()` - the Check filters button. Uses each telescope's latest light
      if every telescope has a recent one, otherwise takes one short frame on
      every idle camera at once (never while a sequence owns the cameras: the
      caller refuses that).
    * `note_light()` - called for every light any camera saves. At the start
      of a night's sequence it collects the first light from each telescope
      and, once all have one, analyses them in the background. Nothing is
      exposed, moved or changed: it reads frames the sequence took anyway.

    Either way the answer is a suggestion. `apply()` is what the button calls
    when a person accepts one, and it goes through the same setter as typing
    the filter in.
    """

    def __init__(self, rigs: Any, config: Any = None, catalog: GaiaCatalog | None = None,
                 references: References | None = None,
                 sequence_running: Callable[[], bool] | None = None) -> None:
        self.rigs = rigs
        self.config = config
        self._catalog = catalog
        self._references = references
        self.sequence_running = sequence_running or (lambda: False)
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
        # The automatic check: night -> {rig id: image id}, and the nights done.
        self._pending: dict[str, dict[str, tuple[str, float]]] = {}
        self._auto_done: set[str] = set()

    @property
    def catalog(self) -> GaiaCatalog:
        if self._catalog is None:
            self._catalog = GaiaCatalog()
        return self._catalog

    @property
    def references(self) -> References:
        if self._references is None:
            self._references = References()
        return self._references

    def _setting(self, key: str, default: Any) -> Any:
        if self.config is None:
            return default
        with contextlib.suppress(Exception):
            value = self.config.get("filterCheck", key, default)
            return default if value is None else value
        return default

    # -- reporting ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict[str, Any]:
        with self._lock:
            done, total = self._progress
            return {"running": self.running, "message": self._message,
                    "log": list(self._log[-40:]), "result": self._result,
                    "error": self._error, "started": self._started,
                    "finished": self._finished, "done": done, "total": total,
                    "auto": bool(self._setting("autoAtStart", True))}

    def _say(self, text: str, level: str = "info") -> None:
        with self._lock:
            self._message = text
            self._log.append(f"{time.strftime('%H:%M:%S')}  {text}")
        with contextlib.suppress(Exception):
            self.rigs.log(f"Filter check: {text}", level)

    def abort(self) -> None:
        self._abort.set()

    # -- choosing frames ---------------------------------------------------
    @staticmethod
    def _camera_ready(rig: Any) -> bool:
        camera = rig.manager.get("camera")
        return camera is not None and getattr(camera, "connected", False)

    def _drawer_rigs(self, rig_ids: list[str] | None = None) -> list[Any]:
        return [rig for rig in self.rigs.all if not rig_ids or rig.id in rig_ids]

    @staticmethod
    def _latest_light(rig: Any, newer_than: float) -> Any:
        for record in list(rig.capture.images):
            if record.timestamp < newer_than:
                break
            if record.frame_type == "light" and (record.path or rig.capture.has(record.id)):
                return record
        return None

    def start(self, rig_ids: list[str] | None = None, mode: str = "auto",
              exposure: float | None = None, cameras_free: bool = True) -> dict[str, Any]:
        """Begin a check. `mode` is auto (reuse recent lights, else expose),
        reuse (only recent lights) or expose (always take new frames).
        `cameras_free` is False while a sequence owns the cameras, which rules
        out exposing."""
        if self.running:
            raise CheckRefused("a filter check is already running")
        if mode not in ("auto", "reuse", "expose"):
            raise CheckRefused(f"unknown mode {mode!r}")
        chosen = self._drawer_rigs(rig_ids)
        if not chosen:
            raise CheckRefused("no telescopes to check")
        now = time.time()
        frames: dict[str, Any] = {}
        if mode in ("auto", "reuse"):
            for rig in chosen:
                record = self._latest_light(rig, now - REUSE_MAX_AGE)
                if record is not None:
                    frames[rig.id] = record
            if mode == "auto" and len(frames) < len(chosen) and cameras_free:
                frames = {}
        if mode == "reuse" and not frames:
            raise CheckRefused("no telescope has taken a light in the last "
                               f"{REUSE_MAX_AGE // 60} minutes")
        expose = not frames
        if expose:
            if not cameras_free:
                raise CheckRefused("the sequence is using the cameras; the check can only "
                                   "use frames it has already taken, and there are none recent")
            ready = [rig for rig in chosen if self._camera_ready(rig)]
            if not ready:
                raise CheckRefused("no camera is connected - connect the cameras first")
            busy = [rig.name for rig in ready if rig.capture.busy]
            if busy:
                raise CheckRefused(f"{', '.join(busy)} busy; wait for it or abort it")
            chosen = ready
        self._begin(chosen, frames, expose, float(exposure or DEFAULT_EXPOSURE), "manual")
        return {"telescopes": [rig.id for rig in chosen],
                "reusing": {rig_id: record.filename for rig_id, record in frames.items()},
                "exposing": expose}

    def _begin(self, chosen: list[Any], frames: dict[str, Any], expose: bool,
               exposure: float, trigger: str) -> None:
        with self._lock:
            self._abort.clear()
            self._log = []
            self._error = None
            self._started = time.time()
            self._finished = None
            self._progress = (0, len(chosen) * 2 + (len(chosen) if expose else 0))
        self._thread = threading.Thread(
            target=self._run, args=(chosen, frames, expose, exposure, trigger),
            daemon=True, name="filter-check")
        self._thread.start()

    # -- the automatic check at the start of a night ------------------------
    def note_light(self, capture: Any, record: Any) -> None:
        """Every saved light passes through here. Cheap, and never raises: it is
        on the capture thread."""
        try:
            if record.frame_type != "light" or not self._setting("autoAtStart", True):
                return
            if not self.sequence_running():
                return
            night = capture.night_name()
            rig = next((r for r in self.rigs.all if r.capture is capture), None)
            if rig is None:
                return
            with self._lock:
                if night in self._auto_done:
                    return
                pending = self._pending.setdefault(night, {})
                pending.setdefault(rig.id, (record.id, record.timestamp))
                wanted = [r for r in self.rigs.all if self._camera_ready(r)] or [rig]
                first = min(t for _, t in pending.values())
                complete = all(r.id in pending for r in wanted)
                waited = time.time() - first > SIMULTANEOUS_SECONDS
                if not (complete or waited) or self.running:
                    return
                self._auto_done.add(night)
                chosen = [r for r in self.rigs.all if r.id in pending]
                frames = {}
                for r in chosen:
                    with contextlib.suppress(Exception):
                        frames[r.id] = r.capture.record(pending[r.id][0])
            if frames:
                self._begin([r for r in chosen if r.id in frames], frames, False, 0.0, "night")
        except Exception:                            # noqa: BLE001 - never trouble a capture
            return

    # -- the work ------------------------------------------------------------
    def _tick(self) -> None:
        with self._lock:
            done, total = self._progress
            self._progress = (min(done + 1, total), total)
        if self._abort.is_set():
            raise RuntimeError("stopped")

    def _expose_all(self, chosen: list[Any], exposure: float) -> dict[str, Any]:
        frames: dict[str, Any] = {}
        errors: dict[str, str] = {}

        def job(rig: Any) -> None:
            # Not saved: a check frame is not data, and must not land in a stack.
            rig.capture.suspend_saving()
            try:
                frames[rig.id] = rig.capture.capture_blocking(exposure)
            except Exception as exc:                 # noqa: BLE001 - reported per camera
                errors[rig.id] = str(exc)
            finally:
                rig.capture.resume_saving()

        threads = [threading.Thread(target=job, args=(rig,), daemon=True) for rig in chosen]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=exposure + 300)
        for rig in chosen:
            if rig.id in errors:
                self._say(f"{rig.name}: the frame failed - {errors[rig.id]}", "warn")
            self._tick()
        return frames

    def _solve(self, rig: Any, record: Any, image: np.ndarray) -> Any:
        import tempfile
        from .imaging import fits as fits_io
        header: dict[str, Any] = {}
        if record.path and Path(record.path).is_file():
            with contextlib.suppress(Exception):
                header = fits_io.read_header(record.path)
        # A frame that already carries a solution needs no solver.
        with contextlib.suppress(Exception):
            if header.get("CRVAL1") is not None and (header.get("CD1_1") is not None
                                                      or header.get("CDELT1") is not None):
                return wcs_from_solution(header, image.shape[1], image.shape[0]), header
        if rig.solver.executable() is None:
            raise RuntimeError("ASTAP was not found, and the frame carries no plate solution")
        with tempfile.TemporaryDirectory(prefix="starfront-filtercheck-") as work:
            source = rig.solver._fits_for(record, Path(work))
            values = rig.solver._run_astap(source, Path(work) / "solution", record, track=False)
        return wcs_from_solution(values, image.shape[1], image.shape[0]), header

    def _header_for(self, record: Any, header: dict[str, Any]) -> dict[str, Any]:
        out = dict(header)
        out.setdefault("EXPTIME", record.exposure)
        out.setdefault("GAIN", record.gain)
        return out

    def _run(self, chosen: list[Any], frames: dict[str, Any], expose: bool,
             exposure: float, trigger: str) -> None:
        result: dict[str, Any] = {"trigger": trigger, "telescopes": {}, "when": time.time()}
        fits_by_rig: dict[str, ColourFit] = {}
        times: dict[str, float] = {}
        try:
            if expose:
                self._say(f"Taking a {exposure:g}s frame on {len(chosen)} camera(s) at once")
                frames = self._expose_all(chosen, exposure)
                if not frames:
                    raise RuntimeError("no camera produced a frame")
            for rig in chosen:
                record = frames.get(rig.id)
                row: dict[str, Any] = {"rig": rig.id, "name": rig.name,
                                       "configured": str(rig.config.get("camera", "fixedFilter", "") or "") or None}
                result["telescopes"][rig.id] = row
                if record is None:
                    row["error"] = "no frame"
                    self._tick(); self._tick()
                    continue
                row["frame"] = record.filename
                row["exposure"] = record.exposure
                try:
                    image = rig.capture.frame(record.id)
                    self._say(f"{rig.name}: plate solving {record.filename}")
                    wcs, header = self._solve(rig, record, image)
                    self._tick()
                    self._say(f"{rig.name}: measuring stars against Gaia")
                    measured = analyse_frame(image, self._header_for(record, header), wcs,
                                             self.catalog, exposure=record.exposure)
                    self._tick()
                except Exception as exc:              # noqa: BLE001 - per telescope
                    if str(exc) == "stopped":
                        raise
                    row["error"] = str(exc)
                    self._say(f"{rig.name}: {exc}", "warn")
                    continue
                fit = measured["fit"]
                fits_by_rig[rig.id] = fit
                times[rig.id] = record.timestamp
                row.update({"fit": fit.payload(), "stars": measured["measured"],
                            "catalog": measured["catalog"], "hfr": measured["hfr"],
                            "centre": measured["centre"]})
            if not fits_by_rig:
                raise RuntimeError("no telescope's frame could be measured")
            # Throughput is only compared between frames taken together.
            groups = _simultaneous(times)
            references = self.references.zero_points()
            for group in groups:
                verdicts = classify({r: fits_by_rig[r] for r in group}, references=references)
                for rig_id, verdict in verdicts.items():
                    row = result["telescopes"][rig_id]
                    row.update(verdict)
                    row["comparedWith"] = [r for r in group if r != rig_id]
                    row.update(compare(verdict, row.get("configured")))
                    model = MODELS[verdict["filter"]]
                    row["filterName"] = model.name
            self._report(result, chosen, fits_by_rig)
            with self._lock:
                self._result = result
        except Exception as exc:                     # noqa: BLE001 - shown to the operator
            with self._lock:
                self._error = str(exc)
                self._result = result if result["telescopes"] else None
            self._say(f"failed: {exc}", "warn")
        finally:
            with self._lock:
                self._finished = time.time()
                self._progress = (self._progress[1], self._progress[1])

    def _report(self, result: dict[str, Any], chosen: list[Any],
                fits_by_rig: dict[str, ColourFit]) -> None:
        night = None
        for rig in chosen:
            row = result["telescopes"].get(rig.id) or {}
            if "filter" not in row:
                continue
            percent = round(100 * row["confidence"])
            text = (f"{rig.name} looks like {row['filterName']} ({row['filter']}, {percent}%; "
                    f"colour term {row['fit']['slope']:+.2f}, zero point {-row['fit']['zp']:.2f}, "
                    f"{row['stars']} stars)")
            if row["verdict"] == "match":
                self._say(text + " - matches the setting")
                # A confident agreement is the best calibration there is.
                if row["confidence"] >= CONFIDENT:
                    self.references.remember(rig.id, fits_by_rig[rig.id], row["filter"])
            elif row["verdict"] in ("mismatch", "probable-mismatch"):
                self._say(text + f" - but it is set to {row['configured']}. Check the drawer; "
                          "nothing has been changed.", "warn")
            else:
                self._say(text)
            # Lights from now on say what was detected, in keywords of their own.
            with contextlib.suppress(Exception):
                night = night or rig.capture.night_name()
                rig.capture.detected_filter = {
                    "filter": row["filter"], "confidence": row["confidence"],
                    "night": night, "checked": result["when"],
                    "configured": row.get("configured")}

    # -- accepting an answer -------------------------------------------------
    def apply(self, choices: dict[str, str], setter: Callable[[dict[str, str]], Any]) -> Any:
        """Set the named telescopes to the filters given (what the button sends:
        normally the detected ones). The setter is the same one typing a
        filter into Equipment uses. The measurement becomes that telescope's
        reference for later checks."""
        clean = {rig_id: filter_names.canonical(letter) for rig_id, letter in choices.items()}
        outcome = setter(clean)
        with self._lock:
            result = self._result or {}
        for rig_id, letter in clean.items():
            row = (result.get("telescopes") or {}).get(rig_id) or {}
            if row.get("fit") and letter in MODELS:
                fit = ColourFit(zp=row["fit"]["zp"], slope=row["fit"]["slope"],
                                zp_error=row["fit"]["zpError"], slope_error=row["fit"]["slopeError"],
                                scatter=row["fit"]["scatter"], stars=row["fit"]["stars"],
                                colour_low=0.0, colour_high=0.0)
                self.references.remember(rig_id, fit, letter)
                row.update(compare(row, letter))
        return outcome


def _simultaneous(times: dict[str, float]) -> list[list[str]]:
    """Telescopes grouped by frames taken together, in order of time."""
    groups: list[list[str]] = []
    for rig_id, when in sorted(times.items(), key=lambda item: item[1]):
        if groups and when - times[groups[-1][0]] <= SIMULTANEOUS_SECONDS:
            groups[-1].append(rig_id)
        else:
            groups.append([rig_id])
    return groups


def header_cards(detected: dict[str, Any] | None, night: str) -> dict[str, tuple[Any, str]]:
    """FITS keywords for a light, from tonight's check. FILTER stays what the
    operator said; these sit beside it."""
    if not detected or detected.get("night") != night or not detected.get("filter"):
        return {}
    return {
        "FILTDET": (detected["filter"], "filter detected photometrically"),
        "FILTCONF": (round(float(detected.get("confidence") or 0.0), 3),
                     "confidence of FILTDET, 0-1"),
        "FILTCHKT": (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(float(detected.get("checked") or 0.0))),
                     "UTC of the filter check"),
    }


def install_light_hook(check: FilterCheck) -> None:
    """Have every camera's saved lights pass through `check.note_light`."""
    from .capture import CaptureService
    hooks = CaptureService.light_hooks
    hooks[:] = [h for h in hooks if getattr(h, "__self__", None).__class__ is not FilterCheck]
    hooks.append(check.note_light)
