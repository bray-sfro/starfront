"""Correcting a plate solution against the stars in the frame.

A solution read out of a header is a starting guess. That is not pessimism,
it is a measurement: two frames of NGC 7380 taken by two telescopes on the
same field, cross-matched on 328 stars to 0.63 pixels, disagree with what
their own headers claim by nearly three degrees of rotation — and Sequence
Generator Pro's angle runs half a turn from N.I.N.A.'s besides. Three degrees
over a five-degree field is several hundred pixels at the corners. A stack
registered on that is sharp in the middle and smeared at the edges, which
looks exactly like a night of bad seeing and is therefore the kind of bug
that never gets reported.

So every frame's position is *measured* before anything is resampled through
it. The measurement is a star match against a reference: a set of positions
on the sky that are already trusted — the stars of the frame that anchored
the stack, or of everything stacked so far. No catalogue, no network, no
index files. The stack is its own reference frame, which is the only
arrangement that works at a dark site on a laptop.

**How the match is found.** The guess is nearly right — usually to a few
arcminutes and a few degrees — so this is not a blind solve and does not need
to behave like one. Candidate transforms are enumerated cheaply (the guess,
the guess turned half round, and both of those mirrored, because those are
the four ways a header dialect goes wrong), and for each one the offsets
between every frame star and every reference star are histogrammed. A real
match piles up in one bin; noise spreads out. That is the whole trick, and it
is robust to more than half the stars having no counterpart at all — which is
the normal case when a 300 mm astrograph and a 2000 mm reflector look at the
same patch of sky.

**Then it is fitted properly.** The histogram gives a shift good to a few
pixels; from there, pairs are matched nearest-neighbour and a full linear
solution — six parameters, so scale, rotation, skew and handedness all at
once — is least-squared over them, tightening the tolerance each pass. Six
parameters rather than four because two telescopes at different scales really
do disagree slightly in aspect: a focal reducer is not perfectly telecentric
and a fitted skew of a tenth of a percent is a real thing rather than noise.

What comes out is a WCS, not a transform. Downstream nothing knows or cares
that the frame's header was wrong.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .wcs import Wcs

#: Stars used from each side. More is better for the fit and worse for the
#: O(n^2) offset histogram; a few hundred is where those cross, and a few
#: hundred stars pin a six-parameter fit to a small fraction of a pixel.
MAX_STARS = 400

#: Bins for the coarse offset search, over the full extent of the frame. One
#: bin is a few tens of pixels, which is far coarser than the answer needs to
#: be — the fit that follows does the precision.
SEARCH_BINS = 128

#: A match is believed when this many stars land in one bin. Below it the peak
#: is as likely to be the tallest pile of noise as a real alignment, and
#: refusing to align is always better than aligning wrongly: an unregistered
#: frame is one frame missing from a stack, a wrongly registered one spoils
#: every pixel it touches.
MIN_MATCHES = 8

#: Turns tried either side of the header's stated angle, in degrees. A plate
#: solve is right to a hundredth; a program's own rotation keyword is right to
#: a few degrees at best, and on a rig whose rotator was never calibrated it
#: can be worse. Ten degrees covers everything short of a header that is
#: simply describing a different night, and the step is fine enough that what
#: is left over is well inside one bin of the offset histogram.
ROTATION_SEARCH = tuple(round(0.5 * step, 2) for step in range(-20, 21))

#: The tightest the pair matching is walked down to, in reference pixels. The
#: ladder above it is worked out from how coarse the offset search was, rather
#: than fixed: a wide field searched in a hundred-odd bins knows its shift to
#: tens of pixels, and a first tolerance tighter than that throws away every
#: real pair along with the false ones. That was not a hypothetical — it is
#: what made the first version of this refuse a match it had already found.
FINEST_TOLERANCE = 2.0

#: Above this the fit is not believed however many stars went into it. A
#: genuine match on real frames settles well under a pixel; anything at two is
#: fitting noise.
MAX_RESIDUAL = 2.0


class AlignError(ValueError):
    """A frame that could not be placed against the reference."""


def _thin(points: np.ndarray, limit: int = MAX_STARS) -> np.ndarray:
    """The first `limit` rows. Star lists arrive brightest first, so this
    keeps the stars most likely to have a counterpart on the other side."""
    return points[:limit] if len(points) > limit else points


def _similarity(source: np.ndarray, target: np.ndarray
                ) -> tuple[np.ndarray, np.ndarray]:
    """The full linear map taking `source` onto `target`, and its residuals.

    Six parameters — a 2x2 matrix and a translation — solved by ordinary least
    squares on the augmented coordinates. Not constrained to a rotation and a
    scale on purpose: the difference between two telescopes includes a little
    real skew, and a constrained fit puts that into the residuals where it
    looks like a worse match than it is.
    """
    ones = np.ones((len(source), 1))
    design = np.hstack([source, ones])
    solution, *_ = np.linalg.lstsq(design, target, rcond=None)
    mapped = design @ solution
    return solution, np.hypot(*(target - mapped).T)


def _apply(solution: np.ndarray, points: np.ndarray) -> np.ndarray:
    return np.hstack([points, np.ones((len(points), 1))]) @ solution


def _peak_offset(source: np.ndarray, target: np.ndarray, span: float
                 ) -> tuple[float, float, int, float]:
    """The commonest offset between two sets of points, and how well it is known.

    Every source point is compared with every target point and the difference
    binned. A genuine alignment puts one bin far above the rest; a wrong
    candidate leaves a flat field of ones and twos. Returned with the bin
    width, because that is exactly how well the answer is known and the pair
    matching that follows has to open at least that wide.
    """
    dx = (target[:, None, 0] - source[None, :, 0]).ravel()
    dy = (target[:, None, 1] - source[None, :, 1]).ravel()
    inside = (np.abs(dx) <= span) & (np.abs(dy) <= span)
    width = 2.0 * span / SEARCH_BINS
    if not inside.any():
        return 0.0, 0.0, 0, width
    counts, x_edges, y_edges = np.histogram2d(
        dx[inside], dy[inside], bins=SEARCH_BINS,
        range=[[-span, span], [-span, span]])
    i, j = np.unravel_index(counts.argmax(), counts.shape)
    return ((x_edges[i] + x_edges[i + 1]) / 2.0,
            (y_edges[j] + y_edges[j + 1]) / 2.0,
            int(counts[i, j]), width)


def _turned(solution: Wcs, degrees: float) -> Wcs:
    """The same solution with the sky turned by `degrees` under it.

    Composed into the matrix rather than re-derived from an angle, so it works
    on a mirrored frame and on one with skew without any special cases.
    """
    if not degrees:
        return solution
    angle = math.radians(degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    rotation = np.array([[cosine, -sine], [sine, cosine]])
    cd11, cd12, cd21, cd22 = solution.cd
    turned = rotation @ np.array([[cd11, cd12], [cd21, cd22]])
    return Wcs(solution.crval1, solution.crval2, solution.crpix1,
               solution.crpix2,
               (float(turned[0, 0]), float(turned[0, 1]),
                float(turned[1, 0]), float(turned[1, 1])),
               solution.width, solution.height, solution.source,
               solution.assumptions)


def _candidates(guess: Wcs) -> list[tuple[str, Wcs]]:
    """The four ways a header's angle goes wrong, as four starting guesses.

    Half a turn, because two programs that both write "the angle" mean
    opposite ends of the same axis; and a mirror, because whether a frame is
    stored top-down is not always said and is sometimes said wrongly. Trying
    all four costs four histograms and removes the need for a table of
    per-program quirks that could never be complete.
    """
    cd11, cd12, cd21, cd22 = guess.cd

    def turned(cd: tuple[float, float, float, float]) -> tuple[float, ...]:
        return tuple(-value for value in cd)

    def mirrored(cd: tuple[float, float, float, float]) -> tuple[float, ...]:
        return (-cd[0], -cd[1], cd[2], cd[3])

    straight = (cd11, cd12, cd21, cd22)
    variants = [
        ("as written", straight),
        ("turned half round", turned(straight)),
        ("mirrored", mirrored(straight)),
        ("mirrored and turned half round", turned(mirrored(straight))),
    ]
    out = []
    for name, cd in variants:
        out.append((name, Wcs(guess.crval1, guess.crval2, guess.crpix1,
                              guess.crpix2, tuple(cd), guess.width,
                              guess.height, guess.source, guess.assumptions)))
    return out


def refine(guess: Wcs, frame_stars: np.ndarray,
           reference_sky: np.ndarray,
           reference: Wcs | None = None) -> tuple[Wcs, dict[str, Any]]:
    """Measure where a frame really points, from its stars and a reference.

    `frame_stars` is `(x, y)` in the frame's own **one-based FITS pixels**,
    brightest first — the convention `imaging.stars` produces once its row and
    column are swapped and shifted. `reference_sky` is `(ra, dec)` in degrees:
    the stars of whatever is already trusted, which for a live stack is the
    stack itself.

    `reference` is the grid those sky positions are most naturally compared
    on. The match is done in *reference pixels* rather than on the sphere
    because a histogram of offsets needs a flat space with a uniform scale,
    and a tangent plane is exactly that. It defaults to the guess itself,
    which is fine when no canvas is in hand.

    Returns a corrected WCS and a report. Raises `AlignError` when there is no
    match to be had, which is a normal outcome — a frame through cloud, or one
    pointing somewhere else entirely — and is why the caller gets an exception
    rather than a silently unchanged guess.
    """
    if len(frame_stars) < MIN_MATCHES:
        raise AlignError(f"only {len(frame_stars)} stars were found in the "
                         "frame, too few to measure its position")
    if len(reference_sky) < MIN_MATCHES:
        raise AlignError(f"the reference has only {len(reference_sky)} stars, "
                         "too few to measure anything against")

    frame_stars = _thin(np.asarray(frame_stars, dtype=np.float64))
    reference_sky = _thin(np.asarray(reference_sky, dtype=np.float64))

    plane = reference if reference is not None else guess
    ref_x, ref_y, ahead = plane.to_pixel(reference_sky[:, 0], reference_sky[:, 1])
    reference_pixels = np.column_stack([ref_x[ahead], ref_y[ahead]])
    if len(reference_pixels) < MIN_MATCHES:
        raise AlignError("the reference stars are not on the same part of the "
                         "sky as this frame")

    # How far the search should look: the frame's own diagonal. A guess that
    # is out by more than the frame is wide is not a guess about this frame.
    span = math.hypot(guess.width, guess.height) * (guess.scale / plane.scale)
    span = max(span, 64.0)

    # The search is over a turn as well as a shift, because a header's angle
    # is wrong by degrees rather than by arcminutes — Sequence Generator Pro's
    # was nearly three degrees out on the frames this was measured against.
    # A shift-only search cannot absorb that: three degrees across a frame
    # half a diagonal wide scatters the offsets over hundreds of pixels, so
    # there is no single shift to find and the histogram never piles up.
    # A frame that carries a real WCS is searched narrowly. A plate solver's
    # angle is right to a hundredth of a degree and its handedness is not in
    # doubt, so trying four parities across forty-one turns apiece would be
    # a hundred and sixty histograms to rediscover what the header already
    # said — seconds per sub, every sub, all night. A frame that carries only
    # a program's own keywords gets the full search, because that is exactly
    # where the header is worth several degrees of nothing.
    trustworthy = guess.source in ("cd", "pc", "cdelt", "aligned", "canvas")
    turns = (0.0,) if trustworthy else ROTATION_SEARCH
    candidates = _candidates(guess)[:1] if trustworthy else _candidates(guess)

    best: dict[str, Any] | None = None
    for name, candidate in candidates:
        for degrees in turns:
            trial = _turned(candidate, degrees)
            ra, dec = trial.to_world(frame_stars[:, 0], frame_stars[:, 1])
            x, y, good = plane.to_pixel(ra, dec)
            if not good.any():
                continue
            moved = np.column_stack([x[good], y[good]])
            dx, dy, votes, precision = _peak_offset(moved, reference_pixels, span)
            if best is None or votes > best["votes"]:
                best = {"name": name, "candidate": trial, "votes": votes,
                        "shift": (dx, dy), "pixels": moved, "turn": degrees,
                        "frame": frame_stars[good], "precision": precision}

    if best is None or best["votes"] < MIN_MATCHES:
        raise AlignError(
            "the frame's stars do not match the reference — it is not the sky "
            "the stack is of, or the frame is too poor to measure "
            f"({0 if best is None else best['votes']} stars agreed, "
            f"{MIN_MATCHES} are needed)")

    # -- from a shift to a full solution -----------------------------------
    moved = best["pixels"] + np.array(best["shift"])
    source = best["frame"]
    solution: np.ndarray | None = None
    residual = float("inf")
    used = 0
    # Open as wide as the offset search's own precision and halve down. The
    # first pass has to admit every pair the search could not distinguish, or
    # the fit never gets the chance to tighten.
    ladder: list[float] = []
    tolerance = max(best["precision"] * 3.0, FINEST_TOLERANCE * 2.0)
    while tolerance > FINEST_TOLERANCE:
        ladder.append(tolerance)
        tolerance /= 2.0
    ladder.append(FINEST_TOLERANCE)

    for tolerance in ladder:
        distance = np.hypot(moved[:, None, 0] - reference_pixels[None, :, 0],
                            moved[:, None, 1] - reference_pixels[None, :, 1])
        nearest = distance.argmin(axis=1)
        closest = distance[np.arange(len(moved)), nearest]
        keep = closest < tolerance
        if int(keep.sum()) < MIN_MATCHES:
            break
        # Fit the frame's own pixels straight onto the reference's, so the
        # result absorbs whatever the candidate transform got wrong rather
        # than being a correction applied on top of it.
        solution, residuals = _similarity(source[keep], reference_pixels[nearest[keep]])
        residual = float(np.median(residuals))
        used = int(keep.sum())
        moved = _apply(solution, source)

    if solution is None or used < MIN_MATCHES:
        raise AlignError("the star match fell apart when it was tightened; "
                         "the frame and the reference are probably not the "
                         "same field")
    if residual > MAX_RESIDUAL:
        raise AlignError(f"the best alignment still leaves the stars "
                         f"{residual:.1f} pixels out, which is not a match")

    return _as_wcs(solution, plane, guess), {
        "aligned": True,
        "candidate": best["name"],
        "stars": used,
        "residual": round(residual, 3),
        "votes": best["votes"],
        "detail": (f"{used} stars matched the reference to "
                   f"{residual:.2f} reference pixels"
                   + ("" if best["name"] == "as written"
                      else f" (the header's angle was {best['name']})")),
    }


def _as_wcs(solution: np.ndarray, plane: Wcs, guess: Wcs) -> Wcs:
    """Turn a fitted frame-pixels-to-reference-pixels map into a plate solution.

    Composition, done exactly rather than by re-deriving an angle and a scale:
    the frame's pixels go through the fitted matrix into the reference's
    pixels, and the reference's pixels go through its own CD matrix onto the
    sky. Multiplying the two matrices carries scale, rotation, skew and
    handedness through in one step, and there is no trigonometry to get a sign
    wrong in.

    The result is pinned at the reference's own sky point, which keeps every
    frame in a stack on one tangent plane — the canvas's — so that resampling
    them is a comparison of like with like.
    """
    matrix = solution[:2, :]              # frame pixel -> reference pixel
    offset = solution[2, :]
    cd11, cd12, cd21, cd22 = plane.cd
    plane_cd = np.array([[cd11, cd12], [cd21, cd22]])

    # d(world)/d(frame pixel) = d(world)/d(reference pixel) . d(reference)/d(frame)
    combined = plane_cd @ matrix.T

    # Where the reference's own reference pixel falls in the frame's pixels,
    # so the new solution can keep the reference's CRVAL: solve
    # matrix^T . p + offset = (crpix1, crpix2).
    wanted = np.array([plane.crpix1, plane.crpix2]) - offset
    try:
        crpix = np.linalg.solve(matrix.T, wanted)
    except np.linalg.LinAlgError as exc:            # pragma: no cover - degenerate
        raise AlignError("the fitted alignment is degenerate") from exc

    return Wcs(crval1=plane.crval1, crval2=plane.crval2,
               crpix1=float(crpix[0]), crpix2=float(crpix[1]),
               cd=(float(combined[0, 0]), float(combined[0, 1]),
                   float(combined[1, 0]), float(combined[1, 1])),
               width=guess.width, height=guess.height,
               source="aligned")


# ---------------------------------------------------------------------------
# Star lists, in the one shape everything here expects
# ---------------------------------------------------------------------------

def star_pixels(found: list[tuple[float, float, float]]) -> np.ndarray:
    """`imaging.stars.detect`'s output as one-based `(x, y)`, brightest first.

    `detect` returns `(row, column, radius)` in zero-based array coordinates
    because that is what measuring a star wants; everything astrometric wants
    `(x, y)` one-based because that is what a WCS is defined on. One place to
    convert beats a `+ 1.0` scattered through four modules, one of which will
    eventually be forgotten.
    """
    if not found:
        return np.empty((0, 2), dtype=np.float64)
    return np.array([[star[1] + 1.0, star[0] + 1.0] for star in found],
                    dtype=np.float64)


def star_sky(found: list[tuple[float, float, float]],
             solution: Wcs) -> np.ndarray:
    """The same stars as `(ra, dec)` in degrees, for use as a reference."""
    pixels = star_pixels(found)
    if not len(pixels):
        return np.empty((0, 2), dtype=np.float64)
    ra, dec = solution.to_world(pixels[:, 0], pixels[:, 1])
    return np.column_stack([ra, dec])
