"""The stack everybody is watching: frames going in, a picture coming out.

A live stack is not a stack that happens to be shown while it is being built.
It is a different algorithm, and the difference is the constraint that you may
not keep the frames. A night is forty gigabytes; a collaboration's night is
several hundred across six observatories. Nothing here may ever hold more than
one frame at a time, which rules out the median, rules out sigma clipping as
it is normally done, and rules out going back to fix anything.

What it leaves is a **running weighted mean with running variance** — five
accumulators per pixel, updated in one pass, from which the mean, the noise,
the depth and a rejection test all fall out. That is Welford's method with
weights, and it is exact rather than approximate: the answer after a hundred
frames is the same number a batch mean of those hundred frames would give.

Five things are true of this stack that are not true of a simple average:

**Frames arrive on different grids.** Six telescopes at six focal lengths
contribute to one canvas, so every frame is resampled onto it through its own
plate solution before it is added. That is `imaging.reproject`'s job; what
arrives here is already on the grid.

**Frames arrive sitting on their own sky, and it is not flat.** Light
pollution from one direction, the Moon, twilight: on a wide field all of that
is a ramp across the frame rather than a level, and it belongs to the
observatory rather than to the object. So a **tilted plane** is fitted and
subtracted from every frame as it arrives — `imaging.gradient` — because
averaging two observatories' different ramps produces one that is nobody's
and that no later gradient removal can find, since it is no longer a plane.

**Frames arrive on different photometric scales.** Two telescopes through
nominally the same filter produce numbers with no common unit — different
aperture, different bandpass, different sky, different exposure. The exposure
is divided out on arrival, because that part is known exactly and
inverse-variance weighting is only correct for frames already on a common
scale. The rest is not in any header and cannot be computed; it is
*measured*, by fitting each incoming frame against what the stack already
holds where the two overlap. The first frame defines the scale and everything
after is brought onto it.

**Frames arrive with satellites in them.** With the frames gone, rejection has
to be done on the way in: a pixel whose value is far from what the stack has
already settled on is refused, once the stack has enough depth to have an
opinion. That kills aeroplanes, satellites and cosmic rays, and it costs one
comparison per pixel. It deliberately does *not* kill anything in the first
few frames, because a stack of three has no idea what is normal.

**Frames arrive out of order, twice, and from machines that then go offline.**
So a contribution carries an identity, the stack remembers what it has taken,
and adding the same frame twice is a no-op rather than a doubling. A ledger
that quietly counts the same hour twice is worse than one that loses it.

The accumulators are plain NumPy arrays saved next to each other, which means
a stack can be closed and reopened, backed up by copying a folder, and read by
anybody with NumPy and no knowledge of this program.
"""

from __future__ import annotations

import json
import math
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .imaging import gradient, png, render, reproject
from .imaging.wcs import Wcs, WcsError, bounds_on, grid

#: The wire format for one contributed tile. Bumped when a reader of an older
#: one would misunderstand a newer one rather than merely miss a field.
TILE_FORMAT = 1

#: How many frames a pixel needs before the stack is allowed to reject
#: anything there. Below this the "expected" value is one or two frames'
#: opinion, and rejecting against it throws away the real data in favour of
#: whatever arrived first.
REJECT_AFTER = 4

#: How far from the running mean a pixel may be, in the stack's own measured
#: noise at that pixel, before it is refused. Loose on purpose: this is
#: rejecting aeroplanes, not trimming the distribution, and a stack that
#: clips at two sigma is a stack that quietly eats the wings of every star.
REJECT_SIGMA = 4.0

#: The most the photometric fit may stretch a frame before it is refused. A
#: gain of thirty means the fit has locked onto noise or the frame is not of
#: this field at all, and stacking it would swamp everything already there.
MAX_GAIN = 30.0
MIN_GAIN = 1.0 / MAX_GAIN

#: How well an incoming frame must correlate with the stack over their
#: overlap to be taken as the same sky. The measurement is made on a binned
#: overlap where noise is no longer the dominant term, so a real frame scores
#: well over 0.9 and a misregistered or mislabelled one scores nothing;
#: `reproject.MIN_CORRELATION` is the floor and this is the stack's own,
#: which may be stricter for a shared canvas that many people are watching.
MIN_CORRELATION = reproject.MIN_CORRELATION

#: How close two stars must be, in canvas pixels, to be taken for the same
#: star when a frame's stars are merged into the stack's reference.
REFERENCE_MERGE = 1.5

#: The most reference stars a stack keeps. A whole mosaic's worth, which is
#: plenty to align any one frame against and small enough to hold, search and
#: save without thinking about it.
MAX_REFERENCE_STARS = 40_000


class StackError(ValueError):
    """A contribution that cannot be added, with the reason a person needs."""


# ---------------------------------------------------------------------------
# What a stack is
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    """The definition of a canvas, in the five numbers that produce it.

    Distributed rather than negotiated. Every machine builds the identical
    grid from these, so a tile resampled in Texas lands on the same pixels as
    one resampled on the server, and nobody has to transmit a WCS and hope.
    """

    ra: float                      # degrees, centre of the canvas
    dec: float
    width: float                   # degrees of sky
    height: float
    scale: float                   # arcseconds per pixel
    rotation: float = 0.0          # position angle of the canvas's up axis
    maxPixels: int = 4096          # the long side is never allowed past this

    def canvas(self) -> Wcs:
        return grid(self.ra, self.dec, self.width, self.height, self.scale,
                    self.rotation, self.maxPixels)

    def payload(self) -> dict[str, Any]:
        canvas = self.canvas()
        return {"ra": self.ra, "dec": self.dec, "width": self.width,
                "height": self.height, "scale": self.scale,
                "rotation": self.rotation, "maxPixels": self.maxPixels,
                "pixelWidth": canvas.width, "pixelHeight": canvas.height,
                "actualScale": round(canvas.scale, 5)}

    @classmethod
    def read(cls, data: dict[str, Any]) -> "Plan":
        return cls(ra=float(data["ra"]), dec=float(data["dec"]),
                   width=float(data["width"]), height=float(data["height"]),
                   scale=float(data["scale"]),
                   rotation=float(data.get("rotation") or 0.0),
                   maxPixels=int(data.get("maxPixels") or 4096))

    @classmethod
    def for_region(cls, ra: float, dec: float, width: float, height: float,
                   scales: list[float] | None = None,
                   max_pixels: int = 4096) -> "Plan":
        """A canvas for a project, at a scale its contributors can actually use.

        The **median** of the contributing telescopes' scales, not the finest
        and not the coarsest. The finest makes every other rig's frames a
        blur of interpolated pixels and the canvas enormous; the coarsest
        throws away the best data anybody has. The median is the scale at
        which half the contributors are being slightly upsampled and half
        slightly downsampled, which is the least damage available.

        With nobody yet signed up there is nothing to take a median of, and
        two arcseconds a pixel is a reasonable guess for the kind of rig that
        joins a wide-field collaboration.
        """
        usable = sorted(value for value in (scales or []) if value and value > 0)
        chosen = float(np.median(usable)) if usable else 2.0
        return cls(ra=ra, dec=dec, width=width, height=height,
                   scale=chosen, maxPixels=max_pixels)


@dataclass
class Tile:
    """One frame's contribution, resampled onto the canvas and cropped to it.

    A tile rather than a frame, and this is the decision that makes a
    community stack possible over a domestic connection. A sixty-megapixel
    sub is a hundred and twenty megabytes; the same sub reprojected onto a
    four-thousand-pixel canvas covers a few hundred thousand pixels of it, and
    compresses to a couple of megabytes. Nothing is lost that the canvas could
    have represented.

    It carries its own weight rather than only its values, because the weight
    is what the accumulator needs and because it encodes where the frame
    actually reached: a pixel with zero weight is one this frame did not see,
    which is a different thing from one it saw as black.
    """

    #: Unique per frame per contributor, so the same frame arriving twice is
    #: recognised. Built from the rig, the night and the file, never random.
    id: str
    agent: str = ""
    filterName: str = ""
    night: str = ""
    #: The canvas box this covers, zero-based half-open.
    box: tuple[int, int, int, int] = (0, 0, 0, 0)
    values: np.ndarray | None = None       # float32, the box's shape
    weight: np.ndarray | None = None       # float32, zero where not covered
    #: Seconds of exposure behind it, for the depth map. Not the same as the
    #: weight: a long sub through cloud is worth less than its length.
    seconds: float = 0.0
    #: What the frame measured about itself, carried for the report.
    note: dict[str, Any] = field(default_factory=dict)

    def encode(self) -> bytes:
        """The tile as bytes: a JSON header, then two compressed planes.

        Deliberately not FITS and not PNG. FITS cannot carry a float weight
        plane beside a float value plane without extensions this program's
        reader does not do, and PNG cannot carry floats at all. This is thirty
        lines, reads in any language, and compresses a mostly-empty weight
        plane to almost nothing.

        **Half precision, on purpose.** A sixteen-bit float keeps about three
        significant figures, which sounds alarming until it is compared with
        what is actually in the data: the value is sky-subtracted, so near
        zero — where the faint signal is — half precision is exact to a
        thousandth of an ADU, and up at a star's sixty thousand its error is
        thirty ADU against a photon noise of two hundred and fifty. In other
        words the quantisation tracks the shot noise, which is the one error
        curve worth matching. It halves what every observatory has to push up
        a domestic connection, every sub, all night.

        The weight plane is scaled by its own maximum before being stored, so
        a frame whose noise happens to be a fraction of an ADU cannot overflow
        the format's modest range.
        """
        if self.values is None or self.weight is None:
            raise StackError("an empty tile cannot be sent")
        values = np.ascontiguousarray(self.values, dtype=np.float16)
        peak = float(np.max(self.weight)) if self.weight.size else 0.0
        weight = np.ascontiguousarray(
            self.weight / peak if peak > 0 else self.weight, dtype=np.float16)
        header = json.dumps({
            "format": TILE_FORMAT, "id": self.id, "agent": self.agent,
            "filter": self.filterName, "night": self.night,
            "box": list(self.box), "seconds": self.seconds,
            "weightPeak": peak,
            "shape": list(values.shape), "note": self.note,
        }).encode("utf-8")
        body = zlib.compress(values.tobytes(), 6)
        tail = zlib.compress(weight.tobytes(), 6)
        return (b"SFTILE01"
                + len(header).to_bytes(4, "little")
                + len(body).to_bytes(4, "little")
                + len(tail).to_bytes(4, "little")
                + header + body + tail)

    @classmethod
    def decode(cls, raw: bytes) -> "Tile":
        if len(raw) < 20 or raw[:8] != b"SFTILE01":
            raise StackError("that is not a Starfront tile")
        header_length = int.from_bytes(raw[8:12], "little")
        body_length = int.from_bytes(raw[12:16], "little")
        tail_length = int.from_bytes(raw[16:20], "little")
        at = 20
        header = json.loads(raw[at:at + header_length].decode("utf-8"))
        at += header_length
        body = zlib.decompress(raw[at:at + body_length])
        at += body_length
        tail = zlib.decompress(raw[at:at + tail_length])
        if int(header.get("format") or 0) > TILE_FORMAT:
            raise StackError(
                f"this tile is in format {header.get('format')} and this "
                f"program reads {TILE_FORMAT} — update it")
        shape = tuple(int(value) for value in header["shape"])
        peak = float(header.get("weightPeak") or 1.0) or 1.0
        return cls(
            id=str(header["id"]), agent=str(header.get("agent") or ""),
            filterName=str(header.get("filter") or ""),
            night=str(header.get("night") or ""),
            box=tuple(int(value) for value in header["box"]),
            values=np.frombuffer(body, dtype=np.float16)
                     .reshape(shape).astype(np.float32),
            weight=(np.frombuffer(tail, dtype=np.float16)
                      .reshape(shape).astype(np.float32) * peak),
            seconds=float(header.get("seconds") or 0.0),
            note=dict(header.get("note") or {}))


# ---------------------------------------------------------------------------
# Making a tile out of a calibrated, solved frame
# ---------------------------------------------------------------------------

def make_tile(frame: np.ndarray, solution: Wcs, canvas: Wcs, tile_id: str,
              seconds: float = 0.0, agent: str = "", filter_name: str = "",
              night: str = "", degree: int = 1,
              normalise: bool = True) -> Tile:
    """Resample one frame onto the canvas, flatten it, normalise it, weigh it.

    The frame is expected calibrated and its solution measured — everything
    upstream of here. What this adds is the four things the accumulator
    cannot work out for itself.

    **The background comes off, tilted.** A frame's background is its own
    site, its own Moon and its own light pollution, and on a wide field none
    of that is flat — it is a ramp across the frame, brightest towards the
    town. Subtracting a *plane* rather than a level is what lets a rig under
    a town and a rig at a dark site add up to something rather than to a
    ramp that is nobody's. `degree=0` takes a level instead, which is all
    that is safe on a frame filled edge to edge with nebula; `imaging.gradient`
    falls back to one by itself when the fit says the plane has found the
    object rather than the sky.

    Done **before** the resampling, on the frame's own pixels. A panel that
    hangs off the edge of the canvas is clipped by the reprojection, and a
    plane fitted to the clipped part would be extrapolated across the rest;
    fitting first uses all of the frame there is. A plane stays a plane
    through a tangent-plane reprojection over the few degrees a telescope
    sees, so nothing is lost by the order.

    **The exposure is divided out.** Not a matter of taste: inverse-variance
    weighting is only correct for frames already on a common flux scale, and
    a 600-second sub and a 300-second one are not. Dividing by the exposure
    puts both in ADU per second, where the longer sub genuinely has the
    lower noise and therefore genuinely earns the greater weight. Without
    it, the right answer depends on the photometric match finding an overlap
    to rediscover a number the header already knew exactly.

    **The frame is weighed by how good it is.** Inverse variance, which for
    frames on a common flux scale is the optimal weighting — it is what makes
    thirty mediocre subs beat five good ones and five good ones beat thirty
    poor ones, automatically, with nobody grading anything. The noise is
    measured on the frame rather than predicted from the exposure, so cloud,
    a bad guide star and a warm sensor are all accounted for by the one
    number that actually matters.
    """
    box = bounds_on(canvas, solution)
    if box is None:
        raise StackError("this frame is not on the stack's patch of sky at all")

    flattened, surface = gradient.remove(frame, degree=degree)
    values, covered, note = reproject.resample(flattened, solution, canvas, box)
    del flattened
    if not covered.any():
        raise StackError("this frame lands on the canvas but covers none of it")

    # Whatever the plane fit left behind. It should be near zero; it is not
    # exactly zero, because the fit was made on the whole frame and this is
    # the part of it that landed on the canvas.
    residual_sky, noise = reproject.background(values, covered)
    if noise <= 0:
        raise StackError("this frame has no measurable noise, so it is blank")

    values = values - np.float32(residual_sky)

    # Per second, so that frames of different length are on one scale before
    # anything is weighed. A frame that never said how long it was is left
    # as it is and says so, rather than being silently divided by one.
    rate = float(seconds) if (normalise and seconds and seconds > 0) else 0.0
    if rate:
        values = values / np.float32(rate)
        noise /= rate

    # Inverse variance. Zero where the frame does not reach: a weight of zero
    # is how "no data" is told from "no signal", which is a distinction the
    # accumulator depends on.
    weight = np.where(covered, np.float32(1.0 / (noise * noise)), np.float32(0.0))
    return Tile(
        id=tile_id, agent=agent, filterName=filter_name, night=night,
        box=tuple(box), values=values.astype(np.float32),
        weight=weight.astype(np.float32), seconds=float(seconds),
        note={**note, "background": surface.payload(),
              "residualSky": round(residual_sky, 4),
              "noise": round(noise, 6),
              "perSecond": bool(rate),
              "solution": solution.payload()})


# ---------------------------------------------------------------------------
# The accumulator
# ---------------------------------------------------------------------------

class LiveStack:
    """A canvas, four accumulators over it, and the record of what went in.

    Held in memory while it is being added to and written to disk on demand,
    because a stack being watched is added to every few minutes and read every
    few seconds, and a design that wrote four canvas-sized arrays on every
    contribution would spend the night doing that instead.
    """

    def __init__(self, root: Path | str, plan: Plan) -> None:
        self.root = Path(root)
        self.plan = plan
        self.canvas = plan.canvas()
        self._lock = threading.RLock()

        shape = (self.canvas.height, self.canvas.width)
        #: Sum of weights, sum of weight*value, sum of weight*value^2 — the
        #: three running sums a weighted mean and variance come out of — plus
        #: a count of contributing frames and the exposure behind each pixel.
        self.weight = np.zeros(shape, dtype=np.float64)
        self.total = np.zeros(shape, dtype=np.float64)
        self.squares = np.zeros(shape, dtype=np.float64)
        self.count = np.zeros(shape, dtype=np.uint16)
        self.exposure = np.zeros(shape, dtype=np.float32)

        #: Tile ids already taken, so a retry is not a doubling.
        self.taken: dict[str, dict[str, Any]] = {}
        #: Stars the stack has seen, as `(ra, dec, rank)` in degrees. This is
        #: the stack's own astrometric reference: the thing every later frame
        #: is aligned against, which is what makes registration work with no
        #: catalogue, no index files and no network. It grows as the mosaic
        #: does — a panel landing on sky nobody has covered contributes its
        #: stars, and the panel after it has something to align to.
        self.reference = np.empty((0, 3), dtype=np.float64)
        #: The photometric reference: set by the first tile that is accepted,
        #: and every later one is brought onto it.
        self.anchored = False
        self.updated = 0.0
        self.rejected_pixels = 0

    # -- reading it --------------------------------------------------------
    @property
    def frames(self) -> int:
        return len(self.taken)

    def mean(self) -> np.ndarray:
        """The stack as it stands: the weighted mean where there is data.

        Zero where nothing has been contributed, which is the one place a zero
        in this program means "nothing" rather than "dark" — and the coverage
        map beside it is how anything that cares tells them apart.
        """
        with self._lock:
            safe = np.where(self.weight > 0, self.weight, 1.0)
            return np.where(self.weight > 0, self.total / safe, 0.0)

    def noise(self) -> np.ndarray:
        """The measured scatter of the contributions at each pixel.

        The weighted standard deviation, which is what the rejection test
        compares against and what makes "how deep is this" answerable from the
        data rather than from a promise about exposure times.
        """
        with self._lock:
            safe = np.where(self.weight > 0, self.weight, 1.0)
            mean = self.total / safe
            variance = self.squares / safe - mean * mean
            return np.sqrt(np.maximum(variance, 0.0))

    def coverage(self) -> np.ndarray:
        """How many frames have contributed to each pixel."""
        with self._lock:
            return self.count.copy()

    # -- the astrometric reference -----------------------------------------
    def remember_stars(self, sky: np.ndarray) -> int:
        """Add a frame's stars to what the stack aligns things against.

        `sky` is `(ra, dec)` in degrees, brightest first — which is the order
        `imaging.stars` produces, and the only ranking available without
        photometry nobody needs here. A star already known is not added again:
        two frames of the same field would otherwise double the reference on
        every sub and turn the match into a search through a hundred thousand
        duplicates by dawn.

        Sameness is judged at a fraction of a canvas pixel, in canvas pixels
        rather than in degrees, because a tolerance in degrees means something
        different at the pole than at the equator and this is a grid.
        """
        if sky is None or not len(sky):
            return 0
        incoming = np.asarray(sky, dtype=np.float64)[:, :2]
        ranks = 1.0 / (np.arange(len(incoming), dtype=np.float64) + 1.0)
        with self._lock:
            if len(self.reference):
                known_x, known_y, _ = self.canvas.to_pixel(
                    self.reference[:, 0], self.reference[:, 1])
                new_x, new_y, ahead = self.canvas.to_pixel(
                    incoming[:, 0], incoming[:, 1])
                fresh = np.ones(len(incoming), dtype=bool)
                for index in np.nonzero(ahead)[0]:
                    near = ((np.abs(known_x - new_x[index]) < REFERENCE_MERGE)
                            & (np.abs(known_y - new_y[index]) < REFERENCE_MERGE))
                    if near.any():
                        fresh[index] = False
                keep = fresh & ahead
            else:
                _, _, ahead = self.canvas.to_pixel(incoming[:, 0], incoming[:, 1])
                keep = ahead
            if not keep.any():
                return 0
            added = np.column_stack([incoming[keep], ranks[keep]])
            self.reference = np.vstack([self.reference, added])
            if len(self.reference) > MAX_REFERENCE_STARS:
                # Keep the brightest, which are the ones another telescope is
                # most likely to have detected too.
                order = np.argsort(-self.reference[:, 2])
                self.reference = self.reference[order[:MAX_REFERENCE_STARS]]
            return int(keep.sum())

    def reference_near(self, guess: Wcs, limit: int = 400) -> np.ndarray:
        """The reference stars a frame could plausibly contain, brightest first.

        Handing the whole reference to the matcher would be correct and far
        too slow: the offset search compares every frame star with every
        reference star, so a mosaic's twenty thousand stars against a frame's
        four hundred is eight million pairs per candidate transform and a
        hundred and sixty candidates. Cutting to the frame's own footprint
        first, with room for the guess to be wrong, brings that back to what
        it was for a single field.
        """
        with self._lock:
            if not len(self.reference):
                return np.empty((0, 2), dtype=np.float64)
            stored = self.reference.copy()
        x, y, ahead = guess.to_pixel(stored[:, 0], stored[:, 1])
        # Half a frame of slack around the frame itself: a guess is out by
        # arcminutes, never by half a field.
        margin_x = guess.width * 0.5
        margin_y = guess.height * 0.5
        inside = (ahead & (x > -margin_x) & (x < guess.width + margin_x)
                  & (y > -margin_y) & (y < guess.height + margin_y))
        if not inside.any():
            return np.empty((0, 2), dtype=np.float64)
        chosen = stored[inside]
        order = np.argsort(-chosen[:, 2])
        return chosen[order[:limit], :2]

    # -- adding to it ------------------------------------------------------
    def add(self, tile: Tile) -> dict[str, Any]:
        """Fold one tile into the stack, and say what happened to it.

        The return value is the point as much as the side effect: a
        contributor needs to be told that their frame was scaled by 0.31 and
        had four thousand pixels rejected, because that is how somebody
        notices that their flat is wrong while there is still a night left to
        fix it.
        """
        if tile.values is None or tile.weight is None:
            raise StackError("an empty tile cannot be added")
        with self._lock:
            if tile.id in self.taken:
                # The original report first, then the verdict — never the
                # other way round. Spread last, the stored report's own
                # `added: True` overwrites the `added: False` being set here,
                # and a caller checking `added` is told that a frame it just
                # had refused as a duplicate was in fact added, which is the
                # one answer that makes a ledger silently double-count.
                return {**self.taken[tile.id], "added": False,
                        "duplicate": True,
                        "detail": "this frame is already in the stack"}

            x0, y0, x1, y1 = tile.box
            x0, y0 = max(0, x0), max(0, y0)
            x1 = min(self.canvas.width, x1)
            y1 = min(self.canvas.height, y1)
            if x1 <= x0 or y1 <= y0:
                raise StackError("this tile's box is not on the canvas")
            values = tile.values[:y1 - y0, :x1 - x0].astype(np.float64)
            weight = tile.weight[:y1 - y0, :x1 - x0].astype(np.float64)
            here = (slice(y0, y1), slice(x0, x1))
            covered = weight > 0
            if not covered.any():
                raise StackError("this tile covers nothing")

            report = self._fold(values, weight, covered, here, tile)
            self.taken[tile.id] = report
            self.updated = time.time()
            return report

    @staticmethod
    def _typical_noise(weight: np.ndarray, where: np.ndarray) -> float:
        """The noise a set of inverse-variance weights stands for, as one number.

        The median rather than the mean, and of the *noise* rather than of the
        weight: a weight is one over a variance, so averaging weights and
        inverting gives the harmonic mean of the variances, which is dragged
        towards whichever pixel happens to be noisiest. The median of the
        noises is the number a person would call "the noise here".
        """
        usable = weight[where & (weight > 0)]
        if not usable.size:
            return 0.0
        if usable.size > 200_000:
            usable = usable[:: usable.size // 200_000]
        return float(np.median(1.0 / np.sqrt(usable)))

    def _fold(self, values: np.ndarray, weight: np.ndarray,
              covered: np.ndarray, here: tuple[slice, slice],
              tile: Tile) -> dict[str, Any]:
        """The arithmetic of one contribution, under the lock."""
        existing_weight = self.weight[here]
        existing_total = self.total[here]
        established = existing_weight > 0

        # -- put it on the stack's photometric scale -----------------------
        match: dict[str, Any] = {"matched": False, "detail": "first frame in "
                                                            "the stack"}
        gain = 1.0
        if self.anchored:
            shared = established & covered & (self.count[here] >= 1)
            safe = np.where(existing_weight > 0, existing_weight, 1.0)
            reference = existing_total / safe
            # Both sides' noise is known here exactly rather than estimated.
            # Every weight in this stack is an inverse variance, so the
            # variance of the weighted mean is one over the summed weight,
            # and the tile's own is one over its weight. `scale_to` needs
            # precisely those two numbers to take the noise out of its
            # statistics, and measuring them back off the pixels — which is
            # what it falls back to — would be a worse answer for no reason.
            reference_noise = self._typical_noise(existing_weight, shared)
            incoming_noise = self._typical_noise(weight, shared)
            gain, offset, match = reproject.scale_to(
                reference, values, shared,
                reference_noise=reference_noise,
                incoming_noise=incoming_noise)
            # A frame that could not be measured against the stack is added
            # unscaled rather than refused: a panel landing on virgin sky at
            # the edge of a mosaic has nothing to overlap with, and that is
            # the normal way a mosaic grows rather than a fault. It is a
            # refusal only when the measurement was made and came out wrong.
            if match["matched"]:
                if not (MIN_GAIN <= gain <= MAX_GAIN):
                    raise StackError(
                        f"this frame would have to be scaled by {gain:.3g} to "
                        "match the stack, which means it is not the same sky, "
                        "not the same filter, or badly registered")
                values = (values - offset) / gain
                # The weight is an inverse variance, so rescaling the values
                # by 1/gain scales the variance by 1/gain^2 and the weight by
                # gain^2. Forgetting this is the subtle way a stack ends up
                # dominated by whichever telescope happens to record the
                # largest numbers rather than by the one with the best data.
                weight = weight * (gain * gain)

        # -- reject what disagrees with what is already known --------------
        keep = covered
        rejected = 0
        ready = established & (self.count[here] >= REJECT_AFTER)
        if ready.any():
            safe = np.where(existing_weight > 0, existing_weight, 1.0)
            mean = existing_total / safe
            variance = self.squares[here] / safe - mean * mean
            spread = np.sqrt(np.maximum(variance, 0.0))
            # A pixel whose scatter has not yet been established — every frame
            # agreed exactly — gets the stack's own median scatter rather than
            # zero, or the first frame to differ by an ADU would be thrown out.
            floor = float(np.median(spread[ready])) if ready.any() else 0.0
            limit = REJECT_SIGMA * np.maximum(spread, max(floor, 1e-6))
            wild = ready & (np.abs(values - mean) > limit)
            rejected = int(wild.sum())
            keep = covered & ~wild

        # -- fold it in -----------------------------------------------------
        contribution = np.where(keep, weight, 0.0)
        self.weight[here] += contribution
        self.total[here] += contribution * np.where(keep, values, 0.0)
        self.squares[here] += contribution * np.where(keep, values * values, 0.0)
        self.count[here] += keep.astype(np.uint16)
        if tile.seconds:
            self.exposure[here] += (keep * np.float32(tile.seconds)).astype(np.float32)

        self.anchored = True
        self.rejected_pixels += rejected
        pixels = int(keep.sum())
        return {
            "added": True,
            "duplicate": False,
            "id": tile.id,
            "agent": tile.agent,
            "filter": tile.filterName,
            "pixels": pixels,
            "rejected": rejected,
            "gain": round(float(gain), 5),
            "match": match,
            "seconds": tile.seconds,
            "detail": (f"{pixels} pixels added"
                       + (f", {rejected} rejected as outliers" if rejected else "")
                       + (f", scaled by {gain:.3f} to match the stack"
                          if match.get("matched") else "")),
        }

    # -- what it looks like ------------------------------------------------
    def preview(self, max_dim: int = 1400,
                stretch: dict[str, float] | None = None) -> tuple[bytes, dict[str, Any]]:
        """A PNG of the stack as it stands, and what was done to make it.

        The stretch is derived from the covered part only. A mosaic that is a
        third finished is two thirds empty, and empty is not black — letting
        those pixels into the histogram drags the black point to zero and
        renders the finished third almost invisible, which is precisely the
        moment somebody is most interested in looking at it.
        """
        data = self.mean()
        covered = self.coverage() > 0
        if not covered.any():
            blank = np.zeros((16, 16), dtype=np.uint8)
            return png.encode(blank), {"frames": 0, "empty": True,
                                       "width": 16, "height": 16}

        # Into the 16-bit range the renderer thinks in, using the covered
        # pixels' own spread so the numbers mean the same thing whatever
        # units the anchoring telescope happened to work in.
        seen = data[covered]
        low = float(np.percentile(seen, 0.5))
        high = float(np.percentile(seen, 99.9))
        if high - low < 1e-9:
            high = low + 1.0
        scaled = np.clip((data - low) / (high - low), 0.0, 1.0) * 65535.0
        frame = np.where(covered, scaled, 0.0).astype(np.uint16)

        params = stretch or render.auto_stretch(frame[covered].reshape(1, -1))
        image, info = render.render_png_array(frame, params, max_dim=max_dim)
        return png.encode(image), {
            "frames": self.frames,
            "empty": False,
            "width": info["width"],
            "height": info["height"],
            "factor": info["factor"],
            "stretch": info["stretch"],
            "covered": round(float(covered.mean()), 4),
            "updated": self.updated,
        }

    def summary(self) -> dict[str, Any]:
        """Everything a person or a page wants to know, without the pixels."""
        with self._lock:
            covered = self.count > 0
            any_covered = bool(covered.any())
            depth = self.exposure[covered] if any_covered else np.zeros(1)
            return {
                "plan": self.plan.payload(),
                "frames": self.frames,
                "agents": sorted({row.get("agent", "") for row in self.taken.values()
                                  if row.get("agent")}),
                "filters": sorted({row.get("filter", "") for row in self.taken.values()
                                   if row.get("filter")}),
                "seconds": round(sum(float(row.get("seconds") or 0.0)
                                     for row in self.taken.values()), 1),
                "covered": round(float(covered.mean()), 4) if covered.size else 0.0,
                "deepestSeconds": round(float(depth.max()), 1) if any_covered else 0.0,
                "medianSeconds": round(float(np.median(depth)), 1) if any_covered else 0.0,
                "maxFramesDeep": int(self.count.max()),
                "rejectedPixels": self.rejected_pixels,
                "referenceStars": int(len(self.reference)),
                "updated": self.updated or None,
            }

    # -- on disk -----------------------------------------------------------
    def save(self) -> Path:
        """Write the accumulators and the ledger out.

        Plain `.npy` files plus one JSON, in a folder. A stack is a thing a
        coordinator will want to back up, move to another machine, or open in
        their own script six months later, and every one of those is a file
        copy rather than an export.

        Written to a temporary name and moved into place, because the machine
        doing this is also driving a mount and the power going off mid-write
        must cost the last few minutes rather than the whole stack.
        """
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            for name, array in (("weight", self.weight), ("total", self.total),
                                ("squares", self.squares), ("count", self.count),
                                ("exposure", self.exposure),
                                ("reference", self.reference)):
                temporary = self.root / f"{name}.part"
                # Written through an open handle rather than by path: `np.save`
                # appends `.npy` to any path that does not already end in it,
                # so saving to `weight.npy.part` silently produces
                # `weight.npy.part.npy` and the rename then fails.
                with open(temporary, "wb") as handle:
                    np.save(handle, array)
                temporary.replace(self.root / f"{name}.npy")
            ledger = {
                "plan": self.plan.payload(),
                "taken": self.taken,
                "anchored": self.anchored,
                "updated": self.updated,
                "rejectedPixels": self.rejected_pixels,
            }
            temporary = self.root / "stack.json.part"
            temporary.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
            temporary.replace(self.root / "stack.json")
            return self.root

    @classmethod
    def open(cls, root: Path | str, plan: Plan | None = None) -> "LiveStack":
        """Reopen a stack from disk, or start a new one where there is none.

        A stack whose plan has changed is started again rather than carried
        over. Its accumulators are pixels of a particular grid and mean
        nothing on a different one; keeping them would be the kind of silent
        corruption that shows as a mosaic with a seam through it.
        """
        root = Path(root)
        ledger_path = root / "stack.json"
        if ledger_path.is_file():
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            stored = Plan.read(ledger["plan"])
            if plan is None or stored.payload() == plan.payload():
                stack = cls(root, stored)
                try:
                    stack.weight = np.load(root / "weight.npy")
                    stack.total = np.load(root / "total.npy")
                    stack.squares = np.load(root / "squares.npy")
                    stack.count = np.load(root / "count.npy")
                    stack.exposure = np.load(root / "exposure.npy")
                    stars_path = root / "reference.npy"
                    if stars_path.is_file():
                        stack.reference = np.load(stars_path)
                except (OSError, ValueError) as exc:
                    raise StackError(
                        f"the stack in {root} is damaged and cannot be "
                        f"reopened ({exc}); move it aside to start again"
                    ) from exc
                stack.taken = dict(ledger.get("taken") or {})
                stack.anchored = bool(ledger.get("anchored"))
                stack.updated = float(ledger.get("updated") or 0.0)
                stack.rejected_pixels = int(ledger.get("rejectedPixels") or 0)
                return stack
        if plan is None:
            raise StackError(f"there is no stack in {root} and no plan to start one")
        return cls(root, plan)


# ---------------------------------------------------------------------------
# Where a stack lives
# ---------------------------------------------------------------------------

def stored_plan(root: Path | str) -> Plan | None:
    """The canvas a stack on disk was built on, or None if there is none.

    The question to ask *before* deciding what canvas to use. A stack's
    accumulators are pixels of one particular grid and are meaningless on any
    other, so a canvas that drifts between one sub and the next — because a
    field size was recomputed, or a rig's scale was edited at midnight —
    silently starts the night again. Asking the folder first means the answer
    is decided once, by whatever came first, and then kept.
    """
    ledger = Path(root) / "stack.json"
    if not ledger.is_file():
        return None
    try:
        return Plan.read(json.loads(ledger.read_text(encoding="utf-8"))["plan"])
    except (OSError, ValueError, KeyError):
        return None


def stack_root(base: Path | str, project: str, filter_name: str) -> Path:
    """One stack per project per filter, in a folder named after both.

    Per filter because a stack is a stack of one thing: Ha and OIII of the
    same nebula are different pictures that happen to share a canvas, and
    averaging them together produces neither. The canvas is shared, so the
    channels line up exactly when somebody combines them later — which is the
    whole reason to define the grid centrally.
    """
    safe = "".join(character if character.isalnum() or character in "-_" else "_"
                   for character in (filter_name or "none")) or "none"
    return Path(base) / str(project) / safe
