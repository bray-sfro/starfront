"""Where a frame's pixels are on the sky, and the arithmetic both ways.

Registration by plate solution needs one thing above all others: a function
that turns a pixel into a right ascension and a declination, and back, and is
*right* — for a frame off any telescope, written by any program. Everything
downstream is resampling, and resampling a frame through a WCS that is a
degree out, or mirrored, or upside down, produces a stack that looks almost
plausible and is worthless.

So this module is deliberately narrow and deliberately paranoid.

**Gnomonic only.** `RA---TAN`/`DEC--TAN` is what every plate solver on earth
writes for a normal telescope frame, and a projection nobody uses is a
projection nobody has tested. A frame claiming anything else is refused by
name rather than quietly treated as a tangent plane.

**One internal representation.** A reference point on the sky, a reference
pixel, and a 2x2 matrix of degrees per pixel. That matrix carries scale,
rotation, handedness and skew at once, which means the rest of the program
never has to ask "is this frame mirrored?" — the determinant already knows.
Every dialect below is converted into it on the way in, so there is exactly
one place where the trigonometry lives.

**Four dialects, because that is what is on disk.** In order of how much they
are to be trusted:

  1. `CD1_1`..`CD2_2` — the modern form, and complete on its own.
  2. `PC1_1`..`PC2_2` with `CDELT1`/`CDELT2` — the same thing factored.
  3. `CDELT1`/`CDELT2` with `CROTA2` — the old form, still the commonest.
  4. A *pointing and an angle*: `CRVAL`/`RA`/`OBJCTRA`, plus a rotation from
     whichever of half a dozen keywords the program felt like using, plus a
     scale from `PIXSCALE` or from the focal length and pixel size.

Dialect 4 is the one that matters in practice and the reason this module is
not three lines of astropy. N.I.N.A. writes a solved frame with `OBJCTROT`
and no CD matrix at all; Sequence Generator Pro writes `CRPIX`, `CRVAL`,
`CTYPE`, `ANGLE`, `PIXSCALE` and `FLIPPED` and again no CD matrix. Both of
those frames *are* plate solved — the solution is simply spelled in the
program's own words — and a registrar that only read CD matrices would report
that neither of them has a solution and refuse to stack the night.

**Row order is not decoration.** FITS counts pixels from the bottom left;
almost every camera reads out top first, and so almost every program writes
`ROWORDER = 'TOP-DOWN'` and stores the image upside down with respect to the
standard. A solver that solved the array as stored has already accounted for
it and its CD matrix needs no help — but an angle in dialect 4 is the angle
of the image *as a person sees it*, and taking it at face value on a top-down
frame lands the frame mirrored through the horizontal. That is the single
easiest way to build a stack of two telescopes that cancels itself out, so
`ROWORDER` is honoured for derived solutions and deliberately ignored for
solved ones.

Pixel coordinates here are **FITS convention throughout**: one-based, and
`(1, 1)` is the centre of the first pixel of the array as stored. The helpers
that talk to NumPy convert at their own edges.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

#: The projections this understands. Anything else is refused by name.
TANGENT = ("RA---TAN", "DEC--TAN", "RA---TAN-SIP", "DEC--TAN-SIP")

#: Keywords a program might have written its position angle into. Searched in
#: this order, most standard first. All of them mean the same thing — the
#: position angle of the image's up direction, measured east of north — which
#: is why they can be searched as a list rather than interpreted apart.
ROTATION_KEYS = ("CROTA2", "CROTA1", "OBJCTROT", "ANGLE", "ROTATANG",
                 "POSANGLE", "PA", "ORIENTAT")

#: Keywords holding arcseconds per pixel directly.
SCALE_KEYS = ("PIXSCALE", "SCALE", "SECPIX", "SECPIX1", "PLTSCALE")

#: Keywords that say the frame is a mirror image. A frame that went through an
#: odd number of reflections has east and west swapped, and stacking it onto a
#: frame that did not is stacking a photograph onto its own negative.
MIRROR_KEYS = ("FLIPPED", "MIRRORED", "MIRROR", "FLIPPED2")


class WcsError(ValueError):
    """A frame whose position on the sky cannot be established."""


# ---------------------------------------------------------------------------
# Reading angles out of a header
# ---------------------------------------------------------------------------

def card(header: dict[str, Any], key: str) -> Any:
    """One header value, whether it is bare or carries its comment.

    This program writes headers as `{"CRVAL1": (343.25, "degrees")}` because
    that is what `imaging.fits.write` takes, and reads them back as bare
    values because that is what a file holds. Both shapes therefore turn up
    in memory, and a reader that only understood one of them would work
    perfectly on anything off disk and fail on a solution handed to it
    directly — which is exactly the sort of difference that survives every
    test and breaks the first real use.
    """
    value = header.get(key)
    if isinstance(value, tuple) and len(value) == 2:
        return value[0]
    return value


def _number(value: Any) -> float | None:
    """A header value as a float, with the absent cases all reading as absent."""
    if isinstance(value, tuple) and len(value) == 2:
        value = value[0]
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip()
    if not text or text.lower() in ("none", "null", "n/a", "nan"):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def sexagesimal(value: Any, hours: bool) -> float | None:
    """An angle from a header, whether written as a number or as three fields.

    `OBJCTRA` is conventionally `'22 52 23'` in hours and `OBJCTDEC`
    `'+57 53 07'` in degrees, but plenty of programs write a bare float
    instead, some use colons, and some write degrees where the convention
    says hours.

    The result is always **degrees**, so a caller never has to remember which
    keyword it came from. An "RA in hours" above 24 cannot be hours and is
    taken as degrees; below 24 it is genuinely ambiguous and the keyword's own
    meaning wins, because guessing there would turn a legitimate 10h into 40
    arcminutes.
    """
    if isinstance(value, tuple) and len(value) == 2:
        value = value[0]
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        angle = float(value)
        if not math.isfinite(angle):
            return None
        return angle * 15.0 if (hours and abs(angle) <= 24.0) else angle

    text = str(value).strip().replace(":", " ").replace("h", " ")
    text = text.replace("m", " ").replace("s", " ").replace("d", " ")
    if not text:
        return None
    negative = text.lstrip().startswith("-")
    try:
        parts = [float(part) for part in text.split() if part]
    except ValueError:
        return None
    if not parts:
        return None
    magnitude = abs(parts[0])
    if len(parts) > 1:
        magnitude += abs(parts[1]) / 60.0
    if len(parts) > 2:
        magnitude += abs(parts[2]) / 3600.0
    angle = -magnitude if negative else magnitude
    # Three fields is the sexagesimal form, so "22 52 23" under an RA keyword
    # is hours however small the first number is.
    if hours and (len(parts) > 1 or abs(angle) <= 24.0):
        angle *= 15.0
    return angle


def _first(header: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _number(card(header, key))
        if value is not None:
            return value
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, tuple) and len(value) == 2:
        value = value[0]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().upper() in ("T", "TRUE", "YES", "Y", "1")


# ---------------------------------------------------------------------------
# The solution itself
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Wcs:
    """One frame's place on the sky: a tangent plane, pinned at a pixel.

    `cd` is degrees of intermediate world coordinate per pixel, in the order
    `(cd11, cd12, cd21, cd22)`, exactly as the FITS keywords of those names.
    Row one is the right-ascension-like axis and row two the declination-like
    one; a negative determinant is the ordinary sky orientation and a positive
    one is a mirror image.

    Frozen, because a WCS describes a frame that has already been taken. A
    mutable one invites a resampling loop to adjust it halfway through.
    """

    crval1: float          # degrees, right ascension of the reference point
    crval2: float          # degrees, declination of it
    crpix1: float          # FITS pixels, one-based
    crpix2: float
    cd: tuple[float, float, float, float]
    width: int = 0         # the frame this describes, in pixels
    height: int = 0
    #: How the solution was arrived at, for a person reading a log: `cd`,
    #: `pc`, `cdelt`, or `derived`. A registration that went wrong is nearly
    #: always a `derived` one, so it is worth being able to see which.
    source: str = "cd"
    #: What the derived path had to assume, if anything. Empty for a real WCS.
    assumptions: tuple[str, ...] = ()

    # -- the numbers people ask for ----------------------------------------
    @property
    def scale(self) -> float:
        """Arcseconds per pixel: the geometric mean of the two axes.

        The mean rather than one axis, because a solver occasionally returns
        a matrix with a fraction of a percent of skew in it and "the scale" of
        such a frame is not either column on its own.
        """
        cd11, cd12, cd21, cd22 = self.cd
        return math.sqrt(abs(cd11 * cd22 - cd12 * cd21)) * 3600.0

    @property
    def rotation(self) -> float:
        """Position angle of the frame's +y axis, degrees east of north.

        Read off the second column, where the declination-like scale is
        positive on a normal sky orientation. Taking it off the first column
        lands 180 degrees out whenever `CDELT1` is negative — which is to say,
        on nearly every real frame.
        """
        _, cd12, _, cd22 = self.cd
        return math.degrees(math.atan2(-cd12, cd22)) % 360.0

    @property
    def mirrored(self) -> bool:
        """Whether east and west are swapped relative to the ordinary sky."""
        cd11, cd12, cd21, cd22 = self.cd
        return (cd11 * cd22 - cd12 * cd21) > 0.0

    @property
    def field(self) -> tuple[float, float]:
        """Degrees of sky across and down the frame, along its own axes."""
        cd11, cd12, cd21, cd22 = self.cd
        across = math.hypot(cd11, cd21) * max(1, self.width)
        down = math.hypot(cd12, cd22) * max(1, self.height)
        return across, down

    @property
    def centre(self) -> tuple[float, float]:
        """Where the middle of the frame points, in degrees.

        Not the reference point: a solver is free to pin its solution wherever
        it likes, and on a mosaic panel solved against a neighbour that can be
        off the edge of the frame entirely.
        """
        ra, dec = self.to_world(np.array([(self.width + 1) / 2.0]),
                               np.array([(self.height + 1) / 2.0]))
        return float(ra[0]) % 360.0, float(dec[0])

    # -- the transform ------------------------------------------------------
    def to_world(self, x: np.ndarray, y: np.ndarray
                 ) -> tuple[np.ndarray, np.ndarray]:
        """Pixels to sky. One-based pixels in, degrees out.

        The inverse gnomonic projection, written so that it vectorises: this
        is called once per output pixel of a mosaic canvas, which is millions
        of times, and a Python loop here would make a live stack a batch job.
        """
        cd11, cd12, cd21, cd22 = self.cd
        dx = np.asarray(x, dtype=np.float64) - self.crpix1
        dy = np.asarray(y, dtype=np.float64) - self.crpix2

        # Intermediate world coordinates, in radians for the projection.
        xi = np.radians(cd11 * dx + cd12 * dy)
        eta = np.radians(cd21 * dx + cd22 * dy)

        ra0 = math.radians(self.crval1)
        dec0 = math.radians(self.crval2)
        sin0, cos0 = math.sin(dec0), math.cos(dec0)

        # The standard deprojection. `cos0 - eta * sin0` is the projection of
        # the point onto the line of sight; at the pole of the tangent plane
        # it reaches zero, which is ninety degrees from the reference point and
        # further than any real frame ever spans.
        along = cos0 - eta * sin0
        ra = ra0 + np.arctan2(xi, along)
        dec = np.arcsin((sin0 + eta * cos0)
                        / np.sqrt(1.0 + xi * xi + eta * eta))
        return np.degrees(ra) % 360.0, np.degrees(dec)

    def to_pixel(self, ra: np.ndarray, dec: np.ndarray
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Sky to pixels. Degrees in; one-based pixels and a validity mask out.

        The mask is the point of the third return value. Half the celestial
        sphere has no image on a tangent plane at all, and the other half maps
        onto it with a singularity ninety degrees out — a reprojection that
        ignored that would silently fold the far side of the sky back onto the
        frame. Anything at or beyond the horizon of the projection comes back
        masked rather than as a plausible pixel.
        """
        ra_rad = np.radians(np.asarray(ra, dtype=np.float64))
        dec_rad = np.radians(np.asarray(dec, dtype=np.float64))
        ra0 = math.radians(self.crval1)
        dec0 = math.radians(self.crval2)
        sin0, cos0 = math.sin(dec0), math.cos(dec0)

        delta = ra_rad - ra0
        sin_dec, cos_dec = np.sin(dec_rad), np.cos(dec_rad)
        # The cosine of the angular distance from the reference point.
        toward = sin_dec * sin0 + cos_dec * cos0 * np.cos(delta)
        # A hard floor rather than a test for zero: within a degree of the
        # projection's horizon the intermediate coordinates are already
        # thousands of degrees and no frame reaches there.
        good = toward > 1e-6
        safe = np.where(good, toward, 1.0)

        xi = np.degrees(cos_dec * np.sin(delta) / safe)
        eta = np.degrees((sin_dec * cos0 - cos_dec * sin0 * np.cos(delta)) / safe)

        cd11, cd12, cd21, cd22 = self.cd
        det = cd11 * cd22 - cd12 * cd21
        if abs(det) < 1e-18:
            raise WcsError("the plate solution is degenerate (its scale is zero)")
        x = (cd22 * xi - cd12 * eta) / det + self.crpix1
        y = (-cd21 * xi + cd11 * eta) / det + self.crpix2
        return x, y, good

    # -- the shape it covers ------------------------------------------------
    def corners(self) -> list[tuple[float, float]]:
        """The four corners of the frame on the sky, degrees, clockwise-ish.

        The outer corners of the outer pixels rather than their centres, so
        that a footprint contains the frame rather than being inscribed in it.
        """
        xs = np.array([0.5, self.width + 0.5, self.width + 0.5, 0.5])
        ys = np.array([0.5, 0.5, self.height + 0.5, self.height + 0.5])
        ra, dec = self.to_world(xs, ys)
        return [(float(a), float(d)) for a, d in zip(ra, dec)]

    def footprint(self) -> dict[str, float]:
        """The frame as a sky rectangle, in the shape the collaboration uses.

        `ra` and `dec` are the centre in degrees and `width`/`height` are
        **degrees of sky**, not of the right ascension coordinate — the same
        convention as `collab.Region`, so the two can be handed to each other
        without a conversion nobody remembers. `rotation` is the position
        angle the camera was really at.

        The extent is measured as the bounding box of the four corners, taken
        on the sky rather than in coordinates: a field at +60 spans twice as
        much right ascension as it does sky, and a footprint that reported the
        coordinate span would claim a rig covered twice the sky it did.
        """
        ra_centre, dec_centre = self.centre
        corners = self.corners()
        cosine = max(math.cos(math.radians(dec_centre)), 1e-6)
        # Right ascension wraps, so every corner is measured as an offset from
        # the centre taken the short way round.
        east = [((a - ra_centre + 180.0) % 360.0 - 180.0) * cosine
                for a, _ in corners]
        north = [d - dec_centre for _, d in corners]
        return {
            "ra": ra_centre,
            "dec": dec_centre,
            "width": max(east) - min(east),
            "height": max(north) - min(north),
            "rotation": self.rotation,
        }

    # -- carrying it -------------------------------------------------------
    def header(self) -> dict[str, Any]:
        """The solution as FITS cards, in the form every reader understands.

        Written as a CD matrix rather than as `CDELT` and `CROTA2`, because
        the matrix is exact for a frame with any skew in it and because a
        frame that leaves here should never need dialect 4 again. The old
        keywords go alongside it for the benefit of readers that only know
        those; they agree with the matrix to within the skew, which for a
        real solve is nothing.
        """
        cd11, cd12, cd21, cd22 = self.cd
        rotation = self.rotation
        sign = 1.0 if self.mirrored else -1.0
        scale = self.scale / 3600.0
        return {
            "CTYPE1": ("RA---TAN", "gnomonic projection"),
            "CTYPE2": ("DEC--TAN", "gnomonic projection"),
            "CUNIT1": ("deg", ""),
            "CUNIT2": ("deg", ""),
            "EQUINOX": (2000.0, "J2000"),
            "RADESYS": ("ICRS", ""),
            "CRVAL1": (round(self.crval1, 9), "RA of reference pixel, degrees"),
            "CRVAL2": (round(self.crval2, 9), "Dec of reference pixel, degrees"),
            "CRPIX1": (round(self.crpix1, 4), "reference pixel, one-based"),
            "CRPIX2": (round(self.crpix2, 4), "reference pixel, one-based"),
            "CD1_1": (cd11, "degrees per pixel"),
            "CD1_2": (cd12, "degrees per pixel"),
            "CD2_1": (cd21, "degrees per pixel"),
            "CD2_2": (cd22, "degrees per pixel"),
            "CDELT1": (sign * scale, "degrees per pixel"),
            "CDELT2": (scale, "degrees per pixel"),
            "CROTA2": (round(rotation, 6), "position angle of +y, east of north"),
            "SECPIX": (round(self.scale, 5), "arcseconds per pixel"),
        }

    def payload(self) -> dict[str, Any]:
        """The solution as plain data, for the wire and for a settings file."""
        return {
            "crval1": self.crval1, "crval2": self.crval2,
            "crpix1": self.crpix1, "crpix2": self.crpix2,
            "cd": list(self.cd), "width": self.width, "height": self.height,
            "source": self.source, "assumptions": list(self.assumptions),
            "scale": round(self.scale, 5), "rotation": round(self.rotation, 4),
            "mirrored": self.mirrored,
        }

    @classmethod
    def read(cls, data: dict[str, Any]) -> "Wcs":
        cd = [float(v) for v in (data.get("cd") or [])]
        if len(cd) != 4:
            raise WcsError("a stored plate solution needs four CD values")
        return cls(crval1=float(data["crval1"]), crval2=float(data["crval2"]),
                   crpix1=float(data["crpix1"]), crpix2=float(data["crpix2"]),
                   cd=(cd[0], cd[1], cd[2], cd[3]),
                   width=int(data.get("width") or 0),
                   height=int(data.get("height") or 0),
                   source=str(data.get("source") or "cd"),
                   assumptions=tuple(data.get("assumptions") or ()))

    def describe(self) -> str:
        """One line a person can check a registration against."""
        ra, dec = self.centre
        across, down = self.field
        return (f'RA {ra / 15.0:.5f}h Dec {dec:+.4f}deg  {self.scale:.3f}"/px  '
                f"rot {self.rotation:.2f}deg"
                + ("  mirrored" if self.mirrored else "")
                + f"  {across:.3f}x{down:.3f}deg  [{self.source}]")


# ---------------------------------------------------------------------------
# Building one from a header
# ---------------------------------------------------------------------------

def _cd_from_angle(scale_x: float, scale_y: float, rotation: float
                   ) -> tuple[float, float, float, float]:
    """A CD matrix from two signed scales and a position angle, in degrees.

    The standard relations, which are worth writing out because getting a sign
    wrong here is a stack that cancels itself out:

        CD1_1 =  CDELT1 cos t     CD1_2 = -CDELT2 sin t
        CD2_1 =  CDELT1 sin t     CD2_2 =  CDELT2 cos t

    `t` is measured east of north, and `scale_x` is negative on an ordinary
    sky orientation because right ascension increases to the left.
    """
    angle = math.radians(rotation)
    cosine, sine = math.cos(angle), math.sin(angle)
    return (scale_x * cosine, -scale_y * sine,
            scale_x * sine, scale_y * cosine)


def _check_projection(header: dict[str, Any]) -> None:
    """Refuse a projection this cannot do, by name.

    A frame in a Zenithal Equal Area projection put through tangent-plane
    arithmetic produces coordinates that are wrong by degrees at the edges and
    right in the middle, which is the hardest kind of wrong to notice.
    """
    for key in ("CTYPE1", "CTYPE2"):
        raw = str(card(header, key) or "").strip().upper()
        if not raw:
            continue
        if raw not in TANGENT:
            raise WcsError(
                f"{key} is {raw!r}; this registers gnomonic (TAN) frames only. "
                "Re-solve the frame with ASTAP or astrometry.net, which write "
                "TAN.")


def from_header(header: dict[str, Any], width: int = 0, height: int = 0,
                binning: int = 0, allow_derived: bool = True) -> Wcs:
    """The plate solution a frame carries, however its program spelled it.

    `width` and `height` default to `NAXIS1`/`NAXIS2`. They are needed for the
    centre, the footprint and the derived reference pixel, so a header without
    them and a caller that does not say is refused rather than guessed at.

    `allow_derived` turns off dialect 4. Worth doing when the answer will be
    used to decide whether a frame *needs* solving: a pointing and an angle
    from the mount is not a plate solution, and a derived WCS built from one
    is only as good as the mount's model. Frames off this program's own
    cameras always carry a real solve, so this costs nothing where it matters.
    """
    _check_projection(header)
    width = int(width or _number(card(header, "NAXIS1")) or 0)
    height = int(height or _number(card(header, "NAXIS2")) or 0)
    if width <= 0 or height <= 0:
        raise WcsError("the frame's size is not known, so its corners and "
                       "centre cannot be worked out")

    crval1 = _number(card(header, "CRVAL1"))
    crval2 = _number(card(header, "CRVAL2"))
    crpix1 = _number(card(header, "CRPIX1"))
    crpix2 = _number(card(header, "CRPIX2"))

    # -- dialect 1: the CD matrix, complete on its own ---------------------
    cd11 = _number(card(header, "CD1_1"))
    cd12 = _number(card(header, "CD1_2"))
    cd21 = _number(card(header, "CD2_1"))
    cd22 = _number(card(header, "CD2_2"))
    if None not in (crval1, crval2, cd11, cd12, cd21, cd22):
        # A CD matrix with no CRPIX is not quite legal but does turn up; the
        # reference point is then the middle of the frame, which is where a
        # solver would have put it.
        return Wcs(crval1=crval1 % 360.0, crval2=crval2,
                   crpix1=crpix1 if crpix1 is not None else (width + 1) / 2.0,
                   crpix2=crpix2 if crpix2 is not None else (height + 1) / 2.0,
                   cd=(cd11, cd12, cd21, cd22),
                   width=width, height=height, source="cd")

    cdelt1 = _number(card(header, "CDELT1"))
    cdelt2 = _number(card(header, "CDELT2"))

    # -- dialect 2: PC matrix times CDELT ---------------------------------
    pc11 = _number(card(header, "PC1_1"))
    pc12 = _number(card(header, "PC1_2"))
    pc21 = _number(card(header, "PC2_1"))
    pc22 = _number(card(header, "PC2_2"))
    if (None not in (crval1, crval2, cdelt1, cdelt2)
            and any(v is not None for v in (pc11, pc12, pc21, pc22))):
        # The standard's own defaults: a PC matrix left partly unwritten is
        # the identity in the parts that are missing.
        pc11 = 1.0 if pc11 is None else pc11
        pc12 = 0.0 if pc12 is None else pc12
        pc21 = 0.0 if pc21 is None else pc21
        pc22 = 1.0 if pc22 is None else pc22
        return Wcs(crval1=crval1 % 360.0, crval2=crval2,
                   crpix1=crpix1 if crpix1 is not None else (width + 1) / 2.0,
                   crpix2=crpix2 if crpix2 is not None else (height + 1) / 2.0,
                   cd=(cdelt1 * pc11, cdelt1 * pc12,
                       cdelt2 * pc21, cdelt2 * pc22),
                   width=width, height=height, source="pc")

    # -- dialect 3: CDELT and CROTA2 --------------------------------------
    if None not in (crval1, crval2) and (cdelt1 is not None or cdelt2 is not None):
        # One CDELT stands for both when only one was written, which happens.
        scale_x = cdelt1 if cdelt1 is not None else -abs(cdelt2)
        scale_y = cdelt2 if cdelt2 is not None else abs(cdelt1)
        rotation = _number(card(header, "CROTA2"))
        if rotation is None:
            rotation = _number(card(header, "CROTA1")) or 0.0
        return Wcs(crval1=crval1 % 360.0, crval2=crval2,
                   crpix1=crpix1 if crpix1 is not None else (width + 1) / 2.0,
                   crpix2=crpix2 if crpix2 is not None else (height + 1) / 2.0,
                   cd=_cd_from_angle(scale_x, scale_y, rotation),
                   width=width, height=height, source="cdelt")

    if not allow_derived:
        raise WcsError("the frame carries no plate solution (no CD matrix, no "
                       "PC matrix and no CDELT)")

    # -- dialect 4: a pointing, an angle and a scale ----------------------
    return _derive(header, width, height, binning, crpix1, crpix2)


def _derive(header: dict[str, Any], width: int, height: int, binning: int,
            crpix1: float | None, crpix2: float | None) -> Wcs:
    """Build a solution out of a pointing, a rotation and a scale.

    This is what N.I.N.A. and Sequence Generator Pro leave behind after a
    successful plate solve, and it is a real solution in every respect except
    that it is not written in the standard's keywords. Every assumption made
    on the way is recorded on the result, because the difference between "we
    read this frame's solution" and "we guessed this frame's geometry from the
    mount" has to survive as far as the log.
    """
    assumptions: list[str] = []

    centre_ra = _number(card(header, "CRVAL1"))
    centre_dec = _number(card(header, "CRVAL2"))
    if centre_ra is None or centre_dec is None:
        centre_ra = sexagesimal(card(header, "OBJCTRA"), hours=True)
        centre_dec = sexagesimal(card(header, "OBJCTDEC"), hours=False)
    if centre_ra is None or centre_dec is None:
        # `RA`/`DEC` are conventionally degrees and are the mount's idea of
        # where it is pointing, so they are the last resort rather than the
        # first: on a frame that was solved they agree with the solution, and
        # on one that was not they are as good as the pointing model.
        centre_ra = sexagesimal(card(header, "RA"), hours=False)
        centre_dec = sexagesimal(card(header, "DEC"), hours=False)
        if centre_ra is not None:
            assumptions.append("pointing taken from RA/DEC, which may be the "
                               "mount's rather than a solve's")
    if centre_ra is None or centre_dec is None:
        raise WcsError(
            "the frame does not say where it points: none of CRVAL1/CRVAL2, "
            "OBJCTRA/OBJCTDEC or RA/DEC is in the header. Plate solve it.")

    scale = _first(header, SCALE_KEYS)
    if scale is None:
        focal = _number(card(header, "FOCALLEN"))
        pixel = (_number(card(header, "XPIXSZ"))
                 or _number(card(header, "PIXSIZE1")))
        binning = int(binning or _number(card(header, "XBINNING")) or 1)
        if not focal or not pixel:
            raise WcsError(
                "the frame does not say what scale it was shot at: no PIXSCALE, "
                "and no focal length and pixel size to work one out from. Plate "
                "solve it, or fill in the optics.")
        # The small-angle formula: 206.265 arcseconds per micron per millimetre.
        # `XPIXSZ` is conventionally the *binned* pixel, but not every program
        # agrees, so binning is applied only where the header says bin > 1 and
        # the pixel size looks unbinned.
        scale = 206.265 * pixel * max(1, binning) / focal
        assumptions.append(f'scale {scale:.3f}"/px worked out from a '
                           f"{focal:g} mm focal length and a {pixel:g} um pixel, "
                           "not measured")

    rotation = _first(header, ROTATION_KEYS)
    if rotation is None:
        rotation = 0.0
        assumptions.append("no rotation in the header, so the camera is assumed "
                           "square to the sky - if it is not, this frame will "
                           "register badly")

    # Start from the standard reading: north up at the stated angle, east to
    # the left, counted from the bottom of the array as FITS requires.
    scale_degrees = abs(scale) / 3600.0
    cd = list(_cd_from_angle(-scale_degrees, scale_degrees, rotation))

    def mirror() -> None:
        """Swap east and west: negate the right-ascension row of the matrix."""
        cd[0] = -cd[0]
        cd[1] = -cd[1]

    # Row order. A solver that wrote a CD matrix already accounted for which
    # way up the array is stored, which is why this is reached only here.
    #
    # Measured rather than reasoned: two frames of NGC 7380 from different
    # telescopes, cross-matched on 328 stars to 0.63 pixels, settle what a
    # top-down frame's angle really means. It is a **mirror in right
    # ascension**, not the flip of the y axis that the name suggests — those
    # two differ by twice the rotation angle, which on that pair was twelve
    # degrees, and twelve degrees over a five-degree field is hundreds of
    # pixels at the corners.
    order = str(card(header, "ROWORDER") or "").strip().upper()
    if order.startswith("TOP"):
        mirror()
        assumptions.append("ROWORDER is TOP-DOWN, so the stored array is "
                           "mirrored in right ascension")

    # An explicit mirror flag is the optics — a diagonal, or an odd number of
    # them — and applies on top of however the rows are stored.
    if any(_truthy(card(header, key)) for key in MIRROR_KEYS if key in header):
        mirror()
        assumptions.append("the header says the frame is mirrored")

    # Deliberately no table of per-program quirks beyond the row order. It was
    # tried: a rule that turned Sequence Generator Pro's `ANGLE` half round
    # was written here, on the strength of an analysis that turned out to be
    # the analysis' own sign error, and the star match then had to undo it.
    # The same measurement showed SGP's angle is about three and a half
    # degrees from the truth anyway — rotator slop, not convention — which is
    # a thing no table can fix and the star match fixes without being told.
    # So: read what the header says, say what was assumed, and let `align`
    # settle it.
    assumptions.append("this is a solution read out of a program's own keywords "
                       "rather than a WCS, so it is a starting guess - refine "
                       "it against the stars before registering anything to it")

    return Wcs(crval1=centre_ra % 360.0, crval2=centre_dec,
               crpix1=crpix1 if crpix1 is not None else (width + 1) / 2.0,
               crpix2=crpix2 if crpix2 is not None else (height + 1) / 2.0,
               cd=(cd[0], cd[1], cd[2], cd[3]),
               width=width, height=height, source="derived",
               assumptions=tuple(assumptions))


def solved(header: dict[str, Any]) -> bool:
    """Whether a frame carries a solution rather than only a pointing.

    The question the pipeline asks before it spends twenty seconds on ASTAP.
    A rotation *and* a scale from a plate solve count, because that is what
    N.I.N.A. and SGP leave behind; a mount's RA and Dec on their own do not.
    """
    if any(card(header, key) is not None
           for key in ("CD1_1", "PC1_1", "CDELT1", "CDELT2")):
        return (_number(card(header, "CRVAL1")) is not None
                or _number(card(header, "CRVAL2")) is not None)
    has_centre = (_number(card(header, "CRVAL1")) is not None
                  or card(header, "OBJCTRA") is not None)
    has_angle = _first(header, ROTATION_KEYS) is not None
    has_scale = (_first(header, SCALE_KEYS) is not None
                 or (_number(card(header, "FOCALLEN")) is not None
                     and _number(card(header, "XPIXSZ")) is not None))
    return bool(has_centre and has_angle and has_scale)


# ---------------------------------------------------------------------------
# Making a grid to stack onto
# ---------------------------------------------------------------------------

def grid(ra: float, dec: float, width_degrees: float, height_degrees: float,
         scale_arcsec: float, rotation: float = 0.0,
         max_pixels: int = 0) -> Wcs:
    """A canvas: the shared tangent plane every contributor reprojects onto.

    This is the single most important object in a community stack, because a
    livestack is only a stack if everybody agrees, to the pixel, on what the
    grid is. So it is defined by five numbers a coordinator can read — where,
    how big, how fine, which way up — and derived identically on every machine
    from those numbers rather than negotiated.

    `width_degrees` and `height_degrees` are **degrees of sky**, matching
    `collab.Region`. `rotation` is the position angle of the canvas's up
    direction; zero is north up, east left, which is what everybody wants
    unless a long thin object is lying at an angle.

    `max_pixels` caps the long side. A live view nobody can wait for is not a
    live view: an eight-degree region at half an arcsecond a pixel is a
    57,000-pixel canvas and thirteen gigabytes of accumulators, and the honest
    thing is to coarsen the grid and say so rather than to try. The returned
    scale is whatever the cap allowed, and the caller can read it back off the
    result.
    """
    if scale_arcsec <= 0:
        raise WcsError("a canvas needs a scale in arcseconds per pixel")
    if width_degrees <= 0 or height_degrees <= 0:
        raise WcsError("a canvas needs a size in degrees")

    scale_degrees = scale_arcsec / 3600.0
    columns = max(1, int(math.ceil(width_degrees / scale_degrees)))
    rows = max(1, int(math.ceil(height_degrees / scale_degrees)))

    if max_pixels and max(columns, rows) > max_pixels:
        # Coarsen rather than crop: a stack that quietly covered less sky than
        # the project asked for would be a hole nobody notices until the end.
        shrink = max(columns, rows) / float(max_pixels)
        scale_degrees *= shrink
        columns = max(1, int(math.ceil(width_degrees / scale_degrees)))
        rows = max(1, int(math.ceil(height_degrees / scale_degrees)))

    # North up, east left, before any rotation: CDELT1 negative.
    cd = _cd_from_angle(-scale_degrees, scale_degrees, rotation)
    return Wcs(crval1=float(ra) % 360.0, crval2=float(dec),
               crpix1=(columns + 1) / 2.0, crpix2=(rows + 1) / 2.0,
               cd=cd, width=columns, height=rows, source="canvas")


def bounds_on(canvas: Wcs, frame: Wcs, margin: int = 2
              ) -> tuple[int, int, int, int] | None:
    """Where a frame lands on a canvas, as a pixel box, or None if nowhere.

    Returned as zero-based half-open `(x0, y0, x1, y1)` ready to slice a NumPy
    array with, clipped to the canvas. The margin covers the difference
    between a straight line in pixels and the slightly curved line it really
    is on a tangent plane; two pixels is more than enough over any field a
    telescope has.

    Working out the box first is what makes an incremental stack cheap. A
    contribution touches a few per cent of a mosaic canvas, and resampling
    only that part is the difference between a stack that keeps up with a
    camera and one that does not.
    """
    ra = np.array([c[0] for c in frame.corners()])
    dec = np.array([c[1] for c in frame.corners()])
    x, y, good = canvas.to_pixel(ra, dec)
    if not bool(np.all(good)):
        return None
    x0 = int(math.floor(float(np.min(x)) - 1.0)) - margin
    x1 = int(math.ceil(float(np.max(x)) - 1.0)) + 1 + margin
    y0 = int(math.floor(float(np.min(y)) - 1.0)) - margin
    y1 = int(math.ceil(float(np.max(y)) - 1.0)) + 1 + margin
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(canvas.width, x1), min(canvas.height, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1
