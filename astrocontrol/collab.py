"""What a collaboration is, in one place that both sides import.

The program and the server have to agree exactly on what a task is, what a
contribution is, and whether a night's data is acceptable.  Two implementations
of that would drift, and the first symptom of drift is a ledger that quietly
reports hours nobody can use — so there is one implementation and both import
it.

Deliberately dependency-free: standard library only, no numpy, no devices, no
FastAPI.  The server should be able to import this without dragging in a
telescope control program, and this module should be able to run anywhere.

**The unit of work is an area of sky, not a panel.**  Panels are an artifact of
one particular camera on one particular telescope; a project several rigs
contribute to cannot be defined in them.  So a project is a rectangle, a task is
a smaller rectangle inside it, and each rig works out its own panels to cover
what it was given.  Depth is integration time *at a point on the sky*, which is
the only definition that means the same thing to a 300 mm refractor and a
2000 mm reflector.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .filters import canonical as _canonical


def _key(name: Any) -> str:
    """A filter name as it is compared: the one spelling, lowered.

    "Ha", "H-alpha" and "H" are one filter, and a coordinator who typed any
    of them meant the same thing as a rig whose wheel says another.
    """
    return _canonical(name).lower()

#: Bumped when the wire format changes in a way old agents cannot read. The
#: agent sends what it speaks and the server can refuse politely rather than
#: handing back something that will be misunderstood.
PROTOCOL = 1

#: The collaboration server everybody uses, baked in so that joining is one
#: button and nobody types a URL. Overridable in Advanced for anybody running
#: their own.
DEFAULT_SERVER = "https://starfront-bray.duckdns.org"

#: States a task moves through. `offered` is waiting for the operator to accept
#: it — an assignment that silently rewrote what somebody's mount did tonight
#: would be the software going rogue, however well meant.
TASK_STATES = ("offered", "accepted", "declined", "complete", "cancelled")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Sky geometry
# ---------------------------------------------------------------------------

@dataclass
class Region:
    """A rectangle of sky: where it is, how big, and which way up.

    Degrees throughout, and `ra` is degrees rather than hours — this crosses a
    wire and "which unit is this" is not a question worth having at three in the
    morning. The program converts at its own edges.

    **`width` and `height` are degrees of sky**, the angle you would measure
    across the rectangle, not degrees of the RA coordinate. At +60 those differ
    by a factor of two, and the coordinate reading is never the one anybody
    means: a rectangle dragged out on a picture, a field of view, and a mosaic's
    extent are all angles on the sky. The convergence of the meridians is dealt
    with where RA offsets are actually computed — in `chunk` — rather than being
    baked into the stored number where every reader has to remember it.
    """

    #: `rotation` is the position angle contributing **cameras** should shoot
    #: at, so that panels from different telescopes stack the same way up. It is
    #: not the orientation of the rectangle: the rectangle is north-up, because
    #: `chunk` lays its tiles out along right ascension and declination without
    #: turning the grid. A box that claimed one orientation while being filled
    #: in at another would look right and be wrong, which is the worse failure.
    ra: float
    dec: float
    width: float
    height: float
    rotation: float = 0.0

    def area(self) -> float:
        """Square degrees."""
        return abs(self.width * self.height)

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def read(cls, data: dict[str, Any]) -> "Region":
        return cls(ra=float(data["ra"]), dec=float(data["dec"]),
                   width=float(data["width"]), height=float(data["height"]),
                   rotation=float(data.get("rotation") or 0.0))


def tiles_across(span: float, field: float, overlap: float = 0.1) -> int:
    """How many frames it takes to cover `span` degrees with a `field`-wide one.

    The obvious `ceil(span / step)` is wrong at the small end, and wrong in the
    way that costs the most: a chunk exactly one field across came out as *two*
    panels, because the overlap shrinks the step below the field and one field
    no longer appears to fit inside itself. Every single-frame chunk in a
    collaboration would have been shot twice.

    The right question is the inverse of what the panels actually cover. `n`
    panels reach `step * (n - 1) + field`, so the first panel covers a whole
    field and only what is left over needs stepping across.
    """
    if field <= 0:
        return 1
    if span <= field:
        return 1
    step = field * (1.0 - min(max(overlap, 0.0), 0.9))
    if step <= 0:
        return 1
    return 1 + int(math.ceil((span - field) / step - 1e-9))


def chunk(region: Region, field_width: float, field_height: float,
          overlap: float = 0.1) -> list[Region]:
    """Cover a region with tiles of a given field of view.

    The arithmetic behind "draw a rectangle and hand it out". A coordinator
    should not be typing coordinates for twelve chunks by hand, and doing it by
    hand is how a mosaic ends up with a seam nobody notices until it is stacked.

    Sizes are sky degrees on both sides — the region's and the field's — so the
    tiling itself is plain arithmetic. The convergence of the meridians enters
    in exactly one place: turning a tile's offset *along* the sky into an RA
    coordinate, which takes a division by the cosine of that tile's own
    declination. Doing it per row matters on a tall region, where the top row
    converges more than the bottom one and a single cosine for the whole thing
    leaves a seam.

    Tiles are spread evenly across the region rather than laid from one corner
    with a ragged strip left over, and `overlap` is a fraction of the field kept
    in common between neighbours, because two panels that merely touch do not
    register against each other.
    """
    if field_width <= 0 or field_height <= 0:
        return [region]
    overlap = min(max(overlap, 0.0), 0.9)

    sky_width = abs(region.width)
    columns = tiles_across(sky_width, field_width, overlap)
    rows = tiles_across(abs(region.height), field_height, overlap)

    span_x = sky_width / columns
    span_y = abs(region.height) / rows
    tiles: list[Region] = []
    for row in range(rows):
        dec = region.dec - abs(region.height) / 2.0 + span_y * (row + 0.5)
        local = max(math.cos(math.radians(dec)), 1e-6)
        for column in range(columns):
            offset = -sky_width / 2.0 + span_x * (column + 0.5)
            tiles.append(Region(
                # The one conversion: a sky offset becomes an RA offset by
                # dividing by the cosine. The size below does not, because it
                # is already the angle on the sky that it claims to be.
                ra=(region.ra + offset / local) % 360.0,
                dec=dec,
                width=field_width,
                height=field_height,
                rotation=region.rotation,
            ))
    return tiles


def share_out(tiles: list[Region], agents: list[str],
              weights: dict[str, float] | None = None) -> dict[str, list[Region]]:
    """Deal tiles between telescopes.

    Interleaved rather than one contiguous block each: rigs drop out, and a
    collaboration where one rig owned an entire edge loses that edge completely
    when it does. Interleaved, the same loss is a thinner mosaic everywhere,
    which is a far easier thing to finish.

    `weights` is how much of a night each rig gives the collaboration. A rig
    with four hours a night is dealt twice what a rig with two is, because the
    alternative — an equal share regardless — hands the same work to somebody
    who cannot start it until Friday, and the project then waits on them.

    A rig that has *not* said is dealt the average of those who have, never
    nothing. Zero does not mean zero here: in the settings it means "as much of
    the night as the target is up for", and a coordinator who deliberately
    picked a telescope and saw it handed no work at all would reasonably think
    the program was broken. When nobody has said, the deal is equal.

    The deal is by *largest remainder*: each rig's exact entitlement is worked
    out, everyone takes their whole tiles, and the tiles left over go to whoever
    was closest to earning another. Dealing by repeated rounding instead loses a
    tile off the end, and a missing tile in a mosaic is a hole nobody notices
    until it is stacked.
    """
    if not agents:
        return {}
    shares: dict[str, list[Region]] = {agent: [] for agent in agents}
    if not tiles:
        return shares

    stated = {agent: max(0.0, float((weights or {}).get(agent) or 0.0))
              for agent in agents}
    spoken = [value for value in stated.values() if value > 0]
    if not spoken:
        share_of = {agent: 1.0 for agent in agents}
    else:
        average = sum(spoken) / len(spoken)
        share_of = {agent: (value if value > 0 else average)
                    for agent, value in stated.items()}

    total = sum(share_of.values())
    exact = {agent: len(tiles) * share_of[agent] / total for agent in agents}
    counts = {agent: int(math.floor(exact[agent])) for agent in agents}
    # Whoever was closest to another whole tile gets the remainders.
    left = len(tiles) - sum(counts.values())
    for agent in sorted(agents, key=lambda a: exact[a] - counts[a], reverse=True):
        if left <= 0:
            break
        counts[agent] += 1
        left -= 1

    # Now interleave, so the busiest rig does not end up owning one whole edge.
    order = [agent for agent in agents if counts[agent] > 0]
    remaining = dict(counts)
    index = 0
    for tile in tiles:
        for _ in range(len(order)):
            agent = order[index % len(order)]
            index += 1
            if remaining[agent] > 0:
                shares[agent].append(tile)
                remaining[agent] -= 1
                break
    return shares


def overlap_area(first: Region, second: Region) -> float:
    """Square degrees two rectangles of sky share.

    A small-angle approximation: both are flattened onto a plane whose x axis
    is right ascension scaled by the cosine of the mean declination. Over the
    few degrees a chunk spans that is worth a fraction of a percent, and it is
    used only to answer "which part of this project is least covered" — a
    comparison between candidates, where a consistent small error cancels.

    Anything that needed real spherical area would deserve a real projection,
    and this deliberately is not that.
    """
    if not first or not second:
        return 0.0
    high = min(first.dec + first.height / 2.0, second.dec + second.height / 2.0)
    low = max(first.dec - first.height / 2.0, second.dec - second.height / 2.0)
    if high <= low:
        return 0.0

    cosine = max(math.cos(math.radians((first.dec + second.dec) / 2.0)), 1e-6)
    # Right ascension wraps, so the separation is taken the short way round.
    delta = abs(((first.ra - second.ra + 180.0) % 360.0) - 180.0) * cosine
    shared = (first.width + second.width) / 2.0 - delta
    if shared <= 0:
        return 0.0
    return shared * (high - low)


def footprint(width: float, height: float,
              rotation: float | None) -> tuple[float, float]:
    """How much east-west and north-south sky a turned camera actually spans.

    A field of 5.3 by 3.5 degrees at a position angle of 268 covers 3.5 degrees
    of right ascension and 5.3 of declination — the long axis runs north-south
    — and tiling a north-up region with the raw numbers puts the columns where
    the rows should be. This is the bounding box of the turned rectangle, which
    is exact at the four cardinal angles and a modest over-estimate between
    them; a rig with a rotator reports no angle and gets its field back as it
    is, because it can turn to suit the grid.
    """
    if rotation is None:
        return width, height
    angle = math.radians(rotation)
    cosine, sine = abs(math.cos(angle)), abs(math.sin(angle))
    return (width * cosine + height * sine, width * sine + height * cosine)


def camera_frame(region: Region, rotation: float) -> tuple[float, float]:
    """A north-up region's extent measured along a turned camera's own axes.

    The other half of `footprint`. A mosaic laid out by the program steps along
    the camera's axes, so to cover a north-up rectangle it has to be as wide as
    that rectangle is *in the camera's frame* — which, for a camera at 268
    degrees over a 16 by 8 degree region, is about 8.6 across and 16.3 down.
    The composite then circumscribes the region; the corners it adds are the
    price of a camera that cannot turn.
    """
    angle = math.radians(rotation)
    cosine, sine = abs(math.cos(angle)), abs(math.sin(angle))
    return (abs(region.width) * cosine + abs(region.height) * sine,
            abs(region.width) * sine + abs(region.height) * cosine)


def single_cell(region: Region, field_width: float,
                field_height: float) -> list[dict[str, Any]]:
    """The one cell of a single-target project, for any camera.

    A single-target collaboration is one object, and everybody points at it.
    A narrower camera does not mosaic it - the object is what is wanted, at
    whatever field each telescope has - so the cell is the rig's own frame
    centred where the project is, and the depth map is built from the
    footprints that really come home.
    """
    return [{"row": 0, "column": 0,
             **Region(ra=region.ra, dec=region.dec,
                      width=field_width or region.width,
                      height=field_height or region.height,
                      rotation=region.rotation).payload()}]


def grid(region: Region, field_width: float, field_height: float,
         overlap: float = 0.1) -> list[dict[str, Any]]:
    """A rig's own tiling of a region, each cell knowing where it sits.

    The same tiles `chunk` makes, with their row and column attached, because a
    share of a mosaic is a set of *cells* and a cell has to be nameable. Rows
    run south to north and columns west to east, which is `chunk`'s order; the
    program's own mosaic numbers its panels differently, and the two are
    matched by where their centres fall on the sky rather than by index — a
    convention nobody has to remember cannot be got wrong.
    """
    columns = tiles_across(abs(region.width), field_width, overlap)
    cells = []
    for index, tile in enumerate(chunk(region, field_width, field_height, overlap)):
        cells.append({"row": index // columns, "column": index % columns,
                      **tile.payload()})
    return cells


def claim(cells: list[dict[str, Any]], taken: list[Region],
          fraction: float) -> list[int]:
    """Which of a rig's cells it should take: the least covered, until it holds
    its `fraction` of its own tiling.

    How a collaboration divides itself up without anybody dealing the cards.
    Each rig tiles the region with its own camera and takes the cells least
    trodden by what is already spoken for — other rigs' claims and the frames
    that have actually come in — until it holds its share.  The share is a
    fraction of the rig's *own* cells' area rather than of the region's, and the
    difference is not academic: tiles overlap by ten percent and run past the
    region's edges, so twelve cells add up to more sky than the region they
    cover, and a rig alone on a project that was told "take the region's area"
    stopped at seven of them.  A fraction of one is all of them, exactly.

    Always at least one cell: a rig that qualified and asked is not sent away
    with nothing because the arithmetic rounded down.
    """
    if not cells:
        return []
    if fraction >= 1.0:
        return list(range(len(cells)))
    regions = [Region.read(cell) for cell in cells]
    wanted = sum(region.area() for region in regions) * max(0.0, fraction)
    scored = sorted(
        range(len(cells)),
        key=lambda i: (sum(overlap_area(regions[i], other) for other in taken), i))
    chosen: list[int] = []
    held = 0.0
    for index in scored:
        chosen.append(index)
        held += regions[index].area()
        if held >= wanted - 1e-9:
            break
    return chosen


#: What a visit to a panel costs beyond its frames: the slew, the centring
#: and the settle. The same figure the program's own budget uses.
VISIT_OVERHEAD_SECONDS = 90.0


def coverage(cells: list[dict[str, Any]],
             contributions: list[dict[str, Any]]) -> tuple[
                 dict[int, dict[str, float]], dict[str, dict[int, float]]]:
    """How deep each cell of a rig's tiling already is, from what has come in.

    The depth map, at the resolution of one rig's own cells. Every accepted
    contribution carries the footprint it was really shot over and how many
    seconds went into it; each cell is credited with the seconds times the
    fraction of the cell that footprint covers. Two things fall out of that at
    once — what the whole collaboration has on each cell, per filter, and what
    *each rig* has on each cell — and both are needed: the first says where the
    field is thinnest, the second says where this rig has not yet been.

    Footprints from other cameras do not line up with these cells and need
    not: overlap is overlap.
    """
    regions = [Region.read(cell) for cell in cells]
    depth: dict[int, dict[str, float]] = {i: {} for i in range(len(cells))}
    mine: dict[str, dict[int, float]] = {}
    for row in contributions:
        footprint = row.get("footprint")
        seconds = float(row.get("seconds") or 0.0)
        if not footprint or seconds <= 0:
            continue
        shot = Region.read(footprint)
        name = _key(row.get("filterName"))
        agent = str(row.get("agent") or "")
        for index, cell in enumerate(regions):
            area = cell.area()
            if area <= 0:
                continue
            fraction = min(1.0, overlap_area(shot, cell) / area)
            if fraction <= 0:
                continue
            depth[index][name] = depth[index].get(name, 0.0) + seconds * fraction
            # A rig's own record of where it has been counts visits, not
            # slivers: neighbouring panels overlap by a tenth of a frame on
            # purpose, and a night on one panel must not read as a tenth of
            # a night on each of its neighbours, or "where have I been least"
            # is answered by how many neighbours a panel has.
            if fraction >= SUBSTANTIAL:
                mine.setdefault(agent, {})[index] = (
                    mine.get(agent, {}).get(index, 0.0) + seconds)
    return depth, mine


#: The fraction of a cell a footprint must cover to count as having been
#: *on* that cell, as against brushing its edge.
SUBSTANTIAL = 0.25


def remaining_seconds(cell_depth: dict[str, float],
                      goals: dict[str, float]) -> float:
    """Seconds a cell still wants across the project's filters, or zero."""
    if not goals:
        return 0.0
    left = 0.0
    for name, hours in goals.items():
        have = cell_depth.get(_key(name), 0.0)
        left += max(0.0, float(hours) * 3600.0 - have)
    return left


def visit_frames(filters: list["FilterTask"], seconds: float,
                 min_frames: int) -> dict[str, int]:
    """How one visit's seconds fall across the filters, as frames.

    Split in proportion to how deep the project wants each filter, and never
    fewer than `min_frames` of any — a stack is per filter, and a filter given
    two subs on a panel has nothing to stack.
    """
    frames: dict[str, int] = {}
    total = sum(max(0.0, f.hours) for f in filters)
    for f in filters:
        if f.exposure <= 0:
            continue
        weight = (f.hours / total) if total > 0 else (1.0 / max(1, len(filters)))
        want = int(math.floor(seconds * weight / f.exposure))
        frames[f.filter] = max(min_frames, want)
    return frames


def assign(cells: list[dict[str, Any]], goals: dict[str, float],
           depth: dict[int, dict[str, float]], mine: dict[int, float],
           claimed: list[Region], hours: float, filters: list["FilterTask"],
           min_frames: int) -> tuple[list[int], dict[str, Any]]:
    """Tonight's panels for one rig, and how long to give each.

    The night is cut into as many visits as it holds — greedy on the whole
    field: a night that could give ten frames each to seven panels does that
    rather than seventy to one, because a mosaic that is seven panels further
    on is worth more than one panel finished. But never so many that a visit
    is under `min_frames` in any filter: a scope stacks its own frames before
    anything is blended, and three subs on a panel do not stack.

    Which panels, in this order of pull:

      * **Not where somebody else is tonight.** A panel two rigs shoot the same
        night is a panel one of them wasted, so cells already spoken for
        tonight go last.
      * **Where this rig has been least.** Every rig should touch every part of
        the field over the season, so that no region is one camera's alone —
        one telescope's optics, one night's seeing, one set of gradients
        printed on a patch of the mosaic is exactly the artifact a
        collaboration exists to average out.
      * **Where the field is thinnest.** With everything else equal, the cell
        the most still wanted goes first, so the mosaic advances everywhere
        rather than being polished in one corner.

    Cells already at the project's depth are not visited. A visit is never
    longer than the project's full depth on a panel. The list is never empty:
    a rig that qualified and asked is given at least one panel.
    """
    if not cells or hours <= 0:
        return [], {"seconds": 0.0, "frames": {}}
    regions = [Region.read(cell) for cell in cells]
    wanted = {i: remaining_seconds(depth.get(i, {}), goals) for i in range(len(cells))}
    open_cells = [i for i in range(len(cells)) if wanted[i] > 0] or list(range(len(cells)))

    def elsewhere(index: int) -> float:
        # Sky somebody else is really on tonight. The tenth-of-a-frame
        # slivers neighbouring panels share are left out for the same reason
        # as in `coverage`: otherwise a free panel hemmed in by four claimed
        # ones scores worse than one somebody is actually shooting.
        cell = regions[index]
        total = 0.0
        for other in claimed:
            common = overlap_area(cell, other)
            if common >= SUBSTANTIAL * min(cell.area(), other.area()):
                total += common
        return total

    order = sorted(open_cells, key=lambda i: (round(elsewhere(i), 6),
                                              round(mine.get(i, 0.0), 1),
                                              -round(wanted[i], 1), i))

    budget = hours * 3600.0
    full = sum(max(0.0, f.hours) for f in filters) * 3600.0 or budget
    floor = sum(min_frames * f.exposure for f in filters if f.exposure > 0)
    floor = min(floor, full) if floor > 0 else min(full, budget)
    visits = int(budget // (floor + VISIT_OVERHEAD_SECONDS))
    visits = max(1, min(len(order), visits))
    seconds = max(floor, min(full, budget / visits - VISIT_OVERHEAD_SECONDS))
    frames = visit_frames(filters, seconds, min_frames)
    seconds = sum(n * f.exposure for f in filters for name, n in frames.items()
                  if name == f.filter)
    return order[:visits], {"seconds": round(seconds, 1), "frames": frames}


def least_covered(candidates: list[Region], taken: list[Region]) -> Region | None:
    """The candidate chunk least trodden by what has already been handed out.

    How a collaboration spreads itself without a coordinator dealing the cards.
    Rigs join at different times with different cameras, so their chunks do not
    line up in a grid and cannot be ticked off a list — but "how much of this
    one is already spoken for" is answerable whatever shape anything is, and
    picking the least-spoken-for is what stops six people photographing the
    middle of the nebula and nobody photographing the edges.

    Ties go to the earliest candidate, which keeps the choice repeatable.
    """
    if not candidates:
        return None
    if not taken:
        return candidates[0]
    best = None
    best_score = None
    for candidate in candidates:
        score = sum(overlap_area(candidate, other) for other in taken)
        if best_score is None or score < best_score - 1e-12:
            best, best_score = candidate, score
    return best


# ---------------------------------------------------------------------------
# What a project will accept
# ---------------------------------------------------------------------------

@dataclass
class Requirements:
    """What the coordinator will take, and what they will not.

    Everything here is in units that mean the same thing on every rig. That is
    the whole design constraint: **star size is in arcseconds, never pixels**,
    because 2.5 px is superb at 0.5"/px and unusable at 3"/px, and a ledger that
    compared them directly would be worse than no ledger at all.
    """

    #: Focal length in millimetres the project will accept. None means no
    #: limit. This is the number people know about their own telescope —
    #: "a 530" means something to everybody, "1.46 arcseconds a pixel" to
    #: few — so it is what a coordinator sets and what a rig is told.
    minFocalLength: float | None = None
    maxFocalLength: float | None = None
    #: Arcseconds per pixel, the same limit in the unit that actually decides
    #: resolution. Still honoured on projects that set it; no longer offered
    #: in the form.
    minScale: float | None = None
    maxScale: float | None = None
    #: Whether one-shot colour cameras may take part. A colour camera has no
    #: filter wheel, no narrowband, and is broadband whatever it does — so on
    #: a bright night it has nothing usable to give. `colourMaxMoon` is the
    #: Moon illumination (0-1) above which a colour camera's night is refused;
    #: None means the project's ordinary Moon rule, if any, is the only one.
    acceptColour: bool = True
    colourMaxMoon: float | None = None
    #: Mean star size across a night, in arcseconds.
    maxHfr: float | None = None
    #: Mean guiding error across a night, in arcseconds.
    maxGuideRms: float | None = None
    #: Sub length, seconds.
    minExposure: float | None = None
    maxExposure: float | None = None
    #: Filters wanted, and the widest bandpass acceptable for each in
    #: nanometres. None as a value means "any bandpass". A 3 nm Ha and a 7 nm Ha
    #: are not the same data and a project is entitled to say so.
    filters: dict[str, float | None] = field(default_factory=dict)
    maxMoonIllumination: float | None = None
    minMoonSeparation: float | None = None
    #: The lowest altitude, in degrees, a rig may shoot this project at. Set
    #: by whoever started it, like the Moon rules: they are the project's
    #: standards, not a choice each participant makes. None means every rig's
    #: own observatory floor applies and nothing more.
    minAltitude: float | None = None
    requireCalibrated: bool = False
    #: The fewest frames a visit to one panel is worth. A scope stacks its
    #: own frames before anything is blended, and three subs on a panel do
    #: not stack; so a night is never carved so finely that a rig is sent to
    #: a panel for less than this. Ten is a working floor for sigma clipping.
    minFramesPerVisit: int = 10

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def read(cls, data: dict[str, Any]) -> "Requirements":
        data = data or {}
        return cls(
            minFramesPerVisit=max(1, _int(data.get("minFramesPerVisit")) or 10),
            minFocalLength=_number(data.get("minFocalLength")),
            maxFocalLength=_number(data.get("maxFocalLength")),
            minScale=_number(data.get("minScale")),
            maxScale=_number(data.get("maxScale")),
            acceptColour=bool(data.get("acceptColour", True)),
            colourMaxMoon=_number(data.get("colourMaxMoon")),
            maxHfr=_number(data.get("maxHfr")),
            maxGuideRms=_number(data.get("maxGuideRms")),
            minExposure=_number(data.get("minExposure")),
            maxExposure=_number(data.get("maxExposure")),
            filters={(_canonical(name) or str(name)): _number(value)
                     for name, value in (data.get("filters") or {}).items()},
            maxMoonIllumination=_number(data.get("maxMoonIllumination")),
            minMoonSeparation=_number(data.get("minMoonSeparation")),
            minAltitude=_number(data.get("minAltitude")),
            requireCalibrated=bool(data.get("requireCalibrated", False)),
        )

    def rules(self) -> dict[str, float]:
        """What the project imposes on the night, as plan-entry options.

        The sequencer and the planner already honour an altitude floor and a
        Moon distance on a plan entry; a collaboration's are the project's,
        so they are written onto the entry when it is adopted and again on
        every sync. Absent ones are written as 0 - "nothing beyond the
        observatory's own" - rather than left, so a project that relaxes a
        rule relaxes it on every rig.
        """
        return {"minAltitude": float(self.minAltitude or 0.0),
                "moonAvoidance": float(self.minMoonSeparation or 0.0)}


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ---------------------------------------------------------------------------
# What a rig is, for deciding whether it can help
# ---------------------------------------------------------------------------

@dataclass
class RigProfile:
    """Enough about a telescope to answer "can this contribute?".

    Sent by the agent when it says hello. The point of it is the pre-flight
    check: telling somebody their 7 nm Ha will be rejected *before* they spend a
    night on it, rather than after.
    """

    name: str = ""
    focalLength: float | None = None
    pixelSize: float | None = None          # microns, unbinned
    sensorWidth: int | None = None          # pixels
    sensorHeight: int | None = None
    binning: int = 1
    #: Filter name -> bandpass in nm, or None where it is not known.
    filters: dict[str, float | None] = field(default_factory=dict)
    #: A one-shot colour camera: a Bayer matrix and, usually, no wheel. Its
    #: frames are broadband RGB whatever is in front of it, which is what a
    #: project has to know before it lets one in.
    colour: bool = False
    #: The camera's position angle on the sky, degrees, when the rig cannot
    #: change it. A rig with a rotator reports None: it can turn to whatever a
    #: project asks. One without has its sensor at whatever angle it sits in
    #: the focuser, and at 268 degrees a 5.3 by 3.5 degree field covers 3.5
    #: degrees of RA and 5.3 of declination — the rows and columns of any
    #: mosaic laid over it swap, and a server that tiled with the raw numbers
    #: would hand out cells the camera cannot cover.
    rotation: float | None = None

    #: What this rig usually achieves, from its own history, in arcseconds.
    typicalHfr: float | None = None
    typicalGuideRms: float | None = None
    #: The sub length this rig shoots each filter at, by name - the exposure
    #: its dark library is built for. A project's share is dealt at these,
    #: because a light with no dark of its own length cannot be calibrated,
    #: and a rig is assumed to have chosen its exposures well.
    exposures: dict[str, float] = field(default_factory=dict)

    #: How much of a night this rig will give the collaboration, in hours.
    #: None means "as much as the target is up for".
    #:
    #: This is the number that decides how much work it is fair to hand out.
    #: Capability says whether a rig *can* contribute; this says how much, and
    #: delegating twenty hours to somebody who gives the collaboration two hours
    #: a weeknight is not an assignment, it is a way of never finishing.
    hoursPerNight: float | None = None
    #: The part of the night it is available, as local clock times ("21:00").
    #: Local to the observatory, and deliberately not converted: the server
    #: cannot know the rig's timezone, its horizon or its trees, and the only
    #: machine that can turn "after nine" into a moment is the one standing
    #: under that sky. It travels so a coordinator can read it.
    windowFrom: str = ""
    windowTo: str = ""

    def scale(self) -> float | None:
        """Arcseconds per pixel, by the small-angle formula."""
        if not self.focalLength or not self.pixelSize:
            return None
        return 206.265 * self.pixelSize * max(1, self.binning) / self.focalLength

    def field(self) -> tuple[float, float] | None:
        """Degrees covered, width by height."""
        scale = self.scale()
        if scale is None or not self.sensorWidth or not self.sensorHeight:
            return None
        binning = max(1, self.binning)
        return (scale * (self.sensorWidth / binning) / 3600.0,
                scale * (self.sensorHeight / binning) / 3600.0)

    def payload(self) -> dict[str, Any]:
        return {**asdict(self), "scale": self.scale(), "field": self.field()}

    @classmethod
    def read(cls, data: dict[str, Any]) -> "RigProfile":
        data = data or {}
        return cls(
            name=str(data.get("name") or ""),
            focalLength=_number(data.get("focalLength")),
            pixelSize=_number(data.get("pixelSize")),
            sensorWidth=_int(data.get("sensorWidth")),
            sensorHeight=_int(data.get("sensorHeight")),
            binning=_int(data.get("binning")) or 1,
            filters={(_canonical(name) or str(name)): _number(value)
                     for name, value in (data.get("filters") or {}).items()},
            colour=bool(data.get("colour", False)),
            typicalHfr=_number(data.get("typicalHfr")),
            typicalGuideRms=_number(data.get("typicalGuideRms")),
            exposures={(_canonical(name) or str(name)): float(value)
                       for name, value in (data.get("exposures") or {}).items()
                       if _number(value) and float(value) > 0},
            rotation=_number(data.get("rotation")),
            hoursPerNight=_number(data.get("hoursPerNight")),
            windowFrom=str(data.get("windowFrom") or ""),
            windowTo=str(data.get("windowTo") or ""),
        )


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: The names a project lists to say it takes one-shot colour data. A colour
#: camera's frame is all three channels at once; "RGB" is what everybody
#: calls that, and the others are the ways people spell it.
COLOUR_FILTERS = {"rgb", "osc", "colour", "color", "one-shot colour", "one-shot color"}


def compatibility(profile: RigProfile, wants: Requirements) -> dict[str, Any]:
    """Whether this rig can usefully contribute, and what stops it.

    The most valuable thing in this module: it answers "should I join?" before
    anybody spends a night finding out. Every check reports rather than throwing,
    because a rig that fails one of five is still worth telling about the other
    four — "your scale is fine, your Ha is too wide" is actionable and "no" is
    not.
    """
    checks: list[dict[str, Any]] = []

    def note(name: str, ok: bool | None, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    # Focal length is the limit a coordinator sets, in the number everybody
    # knows about their own telescope.
    focal = profile.focalLength
    if wants.minFocalLength is not None or wants.maxFocalLength is not None:
        if not focal:
            note("focal length", None, "unknown — set the focal length in "
                                       "Site & Optics")
        elif wants.minFocalLength is not None and focal < wants.minFocalLength:
            note("focal length", False,
                 f"{focal:g} mm is shorter than the {wants.minFocalLength:g} mm "
                 "the project wants")
        elif wants.maxFocalLength is not None and focal > wants.maxFocalLength:
            note("focal length", False,
                 f"{focal:g} mm is longer than the {wants.maxFocalLength:g} mm "
                 "the project wants")
        else:
            note("focal length", True, f"{focal:g} mm")

    scale = profile.scale()
    if scale is None:
        note("scale", None, "unknown — set the focal length, sensor and pixel "
                            "size in Site & Optics")
    elif wants.minScale is not None and scale < wants.minScale:
        note("scale", False, f'{scale:.2f}"/px is finer than the {wants.minScale:g} '
                             "the project wants")
    elif wants.maxScale is not None and scale > wants.maxScale:
        note("scale", False, f'{scale:.2f}"/px is coarser than the {wants.maxScale:g} '
                             "the project wants")
    else:
        note("scale", True, f'{scale:.2f}"/px')

    # A one-shot colour camera is broadband whatever it does. A project can
    # decline them outright, or take them on dark nights only.
    if profile.colour:
        if not wants.acceptColour:
            note("camera", False, "a one-shot colour camera; this project wants "
                                  "mono cameras with filters")
        elif wants.colourMaxMoon is not None:
            note("camera", True, "one-shot colour: nights count only with the "
                                 f"Moon under {wants.colourMaxMoon * 100:.0f}% lit")
        else:
            note("camera", True, "one-shot colour")

    # Filters: a project asking for Ha will not take a rig without one, and a
    # bandpass limit is not a suggestion.
    #
    # Matched without regard to case. "Ha", "ha" and "HA" are one filter, and
    # a coordinator who typed it in lowercase was refusing every rig on the
    # server for a difference nobody could see.
    wanted = set(wants.filters)
    if wanted and profile.colour and not profile.filters:
        # A colour camera with nothing in front of it shoots RGB, and says so
        # without anybody typing a filter name for a wheel it does not have.
        # A project that wants one lists "RGB" among its filters.
        if any(str(name).strip().lower() in COLOUR_FILTERS for name in wanted):  # noqa: E501 - colour names are not filters
            note("filters", True, "can shoot RGB")
        else:
            note("filters", False,
                 "a one-shot colour camera shoots RGB, and this project asks for "
                 + ", ".join(sorted(wanted)) + " only")
    elif wanted and not profile.filters:
        # Not "no Ha; no R; no G; no B" - the rig has said nothing at all, and
        # the fix is one setting, not four filters. A wheel that is switched
        # off cannot be asked, so the names have to be typed in once.
        note("filters", False,
             "this telescope lists no filters - fill in its filter slots under "
             "Equipment (the wheel cannot be asked while it is off)")
    elif wanted:
        carried = {_key(name): value for name, value in profile.filters.items()}
        usable, why = [], []
        for name, limit in wants.filters.items():
            key = _key(name)
            if key not in carried:
                why.append(f"no {name}")
                continue
            have = carried.get(key)
            if limit is not None and have is not None and have > limit:
                why.append(f"{name} is {have:g} nm, wants {limit:g} nm or narrower")
                continue
            if limit is not None and have is None:
                why.append(f"{name} bandpass not set, wants {limit:g} nm or narrower")
                continue
            usable.append(name)
        if usable:
            note("filters", not why,
                 "can shoot " + ", ".join(usable)
                 + (f" — but {'; '.join(why)}" if why else ""))
        else:
            note("filters", False, "; ".join(why) or "none of the wanted filters")

    # The rig shoots each filter at its own length, and a share is dealt at
    # those lengths, so a project that only takes subs of a certain length
    # has to be told now rather than after a night of the wrong ones.
    if (wants.minExposure is not None or wants.maxExposure is not None) \
            and profile.exposures:
        wrong = []
        for name in (wants.filters or profile.exposures):
            seconds = profile.exposures.get(_canonical(name) or str(name))
            if seconds is None:
                continue
            if wants.minExposure is not None and seconds < wants.minExposure:
                wrong.append(f"{name} at {seconds:g}s is shorter than the "
                             f"{wants.minExposure:g}s the project wants")
            elif wants.maxExposure is not None and seconds > wants.maxExposure:
                wrong.append(f"{name} at {seconds:g}s is longer than the "
                             f"{wants.maxExposure:g}s the project wants")
        if wrong:
            note("exposures", False, "; ".join(wrong)
                 + " - change the default exposure under Equipment")
        else:
            note("exposures", True, "the default exposures suit the project")

    for label, have, limit, unit in (
            ("stars", profile.typicalHfr, wants.maxHfr, '"'),
            ("guiding", profile.typicalGuideRms, wants.maxGuideRms, '"')):
        if limit is None:
            continue
        if have is None:
            note(label, None, f"not measured yet; the project wants "
                              f"{limit:g}{unit} or better")
        else:
            note(label, have <= limit,
                 f"usually {have:.2f}{unit}, project wants {limit:g}{unit} or better")

    failed = [c for c in checks if c["ok"] is False]
    unknown = [c for c in checks if c["ok"] is None]
    return {
        "ok": not failed,
        "certain": not failed and not unknown,
        "checks": checks,
        "summary": ("cannot contribute: " + "; ".join(c["detail"] for c in failed))
                   if failed
                   else ("can contribute, but some of it is unverified"
                         if unknown else "can contribute"),
    }


# ---------------------------------------------------------------------------
# What an agent is told to do
# ---------------------------------------------------------------------------

@dataclass
class FilterTask:
    """One filter's share of a task: how long a sub, and how much of it."""

    filter: str
    exposure: float
    hours: float

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def read(cls, data: dict[str, Any]) -> "FilterTask":
        return cls(filter=_canonical(data["filter"]) or str(data["filter"]),
                   exposure=float(data.get("exposure") or 0.0),
                   hours=float(data.get("hours") or 0.0))

    def frames(self) -> int:
        """How many subs that is, rounded up: a part-frame is not a frame."""
        if self.exposure <= 0:
            return 0
        return int(math.ceil(self.hours * 3600.0 / self.exposure))


@dataclass
class Task:
    """A chunk of sky, some filters, and how deep to go on it."""

    id: str
    project: str
    projectName: str
    agent: str
    #: The *whole* project's sky. A task used to be one camera-sized cell of
    #: it, which made a rig that was the only one on a forty-panel mosaic the
    #: owner of exactly one panel, and its target on the plan a single frame
    #: with no sign of the other thirty-nine.
    region: Region
    filters: list[FilterTask]
    state: str = "offered"
    version: int = 1
    issued: float = 0.0
    note: str = ""
    #: This rig's own tiling of the region, and which of those cells are its
    #: to shoot. The share moves as others join and as frames come in, and a
    #: change to it bumps the version so the rig's plan follows.
    cells: list[dict[str, Any]] = field(default_factory=list)
    share: list[int] = field(default_factory=list)
    #: "single" or "mosaic". A single-target project is one object and every
    #: telescope points at it - nobody tiles it, however narrow their camera -
    #: so the rig's program makes one frame of it, not a grid.
    kind: str = "mosaic"

    def seconds(self) -> float:
        return sum(entry.hours for entry in self.filters) * 3600.0

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id, "project": self.project,
            "projectName": self.projectName, "agent": self.agent,
            "region": self.region.payload(),
            "filters": [entry.payload() for entry in self.filters],
            "state": self.state, "version": self.version,
            "issued": self.issued, "note": self.note,
            "seconds": self.seconds(),
            "cells": list(self.cells), "share": list(self.share),
            "kind": self.kind,
        }

    @classmethod
    def read(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=str(data["id"]), project=str(data["project"]),
            projectName=str(data.get("projectName") or ""),
            agent=str(data.get("agent") or ""),
            region=Region.read(data["region"]),
            filters=[FilterTask.read(entry) for entry in (data.get("filters") or [])],
            state=str(data.get("state") or "offered"),
            version=int(data.get("version") or 1),
            issued=float(data.get("issued") or 0.0),
            note=str(data.get("note") or ""),
            cells=list(data.get("cells") or []),
            share=[int(i) for i in (data.get("share") or [])],
            kind=("single" if data.get("kind") == "single" else "mosaic"),
        )


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------

@dataclass
class Contribution:
    """One night's work on one filter, as measured rather than as intended.

    The footprint is the *solved* one — where the telescope actually pointed and
    how the camera was actually turned — so depth is accumulated over the sky
    that was really covered rather than the sky that was planned.
    """

    agent: str = ""
    project: str = ""
    task: str = ""
    night: str = ""
    filterName: str = ""
    #: Which panel of the rig's mosaic this is, as the rig numbers them. Blank
    #: for a single-frame chunk. Part of what makes a night's report unique:
    #: twelve panels shot through Ha on one night are twelve contributions, not
    #: one and eleven duplicates — and the depth map is built from their
    #: footprints, which are different sky.
    panel: str = ""
    frames: int = 0
    seconds: float = 0.0
    exposure: float = 0.0
    footprint: Region | None = None
    scale: float | None = None          # arcsec/px
    focalLength: float | None = None    # mm
    hfr: float | None = None            # arcseconds, mean over the night
    guideRms: float | None = None       # arcseconds, mean over the night
    moonIllumination: float | None = None
    moonSeparation: float | None = None
    calibrated: bool = False
    bandpass: float | None = None       # nm, of the filter used
    #: Shot on a one-shot colour camera: broadband RGB, whatever the filter
    #: name says, and judged against the project's colour-camera Moon rule.
    colour: bool = False

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["footprint"] = self.footprint.payload() if self.footprint else None
        return data

    @classmethod
    def read(cls, data: dict[str, Any]) -> "Contribution":
        footprint = data.get("footprint")
        return cls(
            agent=str(data.get("agent") or ""),
            project=str(data.get("project") or ""),
            task=str(data.get("task") or ""),
            night=str(data.get("night") or ""),
            filterName=_canonical(data.get("filterName") or data.get("filter") or ""),
            panel=str(data.get("panel") if data.get("panel") is not None else ""),
            frames=_int(data.get("frames")) or 0,
            seconds=float(data.get("seconds") or 0.0),
            exposure=float(data.get("exposure") or 0.0),
            footprint=Region.read(footprint) if footprint else None,
            scale=_number(data.get("scale")),
            focalLength=_number(data.get("focalLength")),
            hfr=_number(data.get("hfr")),
            guideRms=_number(data.get("guideRms")),
            moonIllumination=_number(data.get("moonIllumination")),
            moonSeparation=_number(data.get("moonSeparation")),
            calibrated=bool(data.get("calibrated", False)),
            bandpass=_number(data.get("bandpass")),
            colour=bool(data.get("colour", False)),
        )


def judge(contribution: Contribution, wants: Requirements) -> dict[str, Any]:
    """Accept or reject one night's data, and say why.

    Advisory. A coordinator can overrule it, and should be able to: seeing
    varies, and a night the numbers reject may be the only data anybody has on
    that patch of sky. A verdict that could not be overruled would make the
    rules more authoritative than the person who wrote them.
    """
    reasons: list[str] = []

    def check(ok: bool, reason: str) -> None:
        if not ok:
            reasons.append(reason)

    # The same comparison as `compatibility`, and for the same reason: "Ha" and
    # "ha" are one filter, and a night is not thrown away over a capital.
    asked = {_key(name): limit for name, limit in wants.filters.items()}
    shot = _key(contribution.filterName)
    if contribution.colour and not wants.acceptColour:
        reasons.append("shot on a one-shot colour camera; this project wants "
                       "mono cameras with filters")
    if (contribution.colour and wants.colourMaxMoon is not None
            and contribution.moonIllumination is not None):
        check(contribution.moonIllumination <= wants.colourMaxMoon,
              f"the Moon was {contribution.moonIllumination * 100:.0f}% lit, and "
              "a one-shot colour camera's night counts only under "
              f"{wants.colourMaxMoon * 100:.0f}%")
    if wants.filters and shot not in asked:
        reasons.append(f"{contribution.filterName} is not a filter this project wants")
    else:
        limit = asked.get(shot)
        if limit is not None and contribution.bandpass is not None:
            check(contribution.bandpass <= limit,
                  f"{contribution.filterName} is {contribution.bandpass:g} nm, "
                  f"project wants {limit:g} nm or narrower")

    if wants.minFocalLength is not None or wants.maxFocalLength is not None:
        focal = contribution.focalLength
        if focal:
            if wants.minFocalLength is not None:
                check(focal >= wants.minFocalLength,
                      f"{focal:g} mm is shorter than the {wants.minFocalLength:g} mm "
                      "the project wants")
            if wants.maxFocalLength is not None:
                check(focal <= wants.maxFocalLength,
                      f"{focal:g} mm is longer than the {wants.maxFocalLength:g} mm "
                      "the project wants")

    if contribution.scale is not None:
        if wants.minScale is not None:
            check(contribution.scale >= wants.minScale,
                  f'{contribution.scale:.2f}"/px is finer than {wants.minScale:g}')
        if wants.maxScale is not None:
            check(contribution.scale <= wants.maxScale,
                  f'{contribution.scale:.2f}"/px is coarser than {wants.maxScale:g}')

    if wants.maxHfr is not None and contribution.hfr is not None:
        check(contribution.hfr <= wants.maxHfr,
              f'stars averaged {contribution.hfr:.2f}", project wants '
              f'{wants.maxHfr:g}" or better')
    if wants.maxGuideRms is not None and contribution.guideRms is not None:
        check(contribution.guideRms <= wants.maxGuideRms,
              f'guiding averaged {contribution.guideRms:.2f}", project wants '
              f'{wants.maxGuideRms:g}" or better')

    if wants.minExposure is not None and contribution.exposure:
        check(contribution.exposure >= wants.minExposure,
              f"{contribution.exposure:g}s subs are shorter than the "
              f"{wants.minExposure:g}s minimum")
    if wants.maxExposure is not None and contribution.exposure:
        check(contribution.exposure <= wants.maxExposure,
              f"{contribution.exposure:g}s subs are longer than the "
              f"{wants.maxExposure:g}s maximum")

    if wants.maxMoonIllumination is not None and contribution.moonIllumination is not None:
        check(contribution.moonIllumination <= wants.maxMoonIllumination,
              f"the Moon was {contribution.moonIllumination * 100:.0f}% lit, "
              f"project wants {wants.maxMoonIllumination * 100:.0f}% or less")
    if wants.minMoonSeparation is not None and contribution.moonSeparation is not None:
        check(contribution.moonSeparation >= wants.minMoonSeparation,
              f"the Moon was {contribution.moonSeparation:.0f}° away, project "
              f"wants {wants.minMoonSeparation:g}° or more")

    if wants.requireCalibrated:
        check(contribution.calibrated, "the frames are not calibrated")

    # Nothing measured is not the same as nothing wrong. A night with no star
    # measurement at all cannot be said to have passed a star-size rule.
    unknown = []
    if wants.maxHfr is not None and contribution.hfr is None:
        unknown.append("star size was not measured")
    if wants.maxGuideRms is not None and contribution.guideRms is None:
        unknown.append("guiding was not measured")
    if wants.minScale is not None or wants.maxScale is not None:
        if contribution.scale is None:
            unknown.append("the image scale is not known — was it plate solved?")

    return {
        "accepted": not reasons,
        "reasons": reasons,
        "unverified": unknown,
        "summary": "; ".join(reasons) if reasons
                   else ("accepted, but " + "; ".join(unknown) if unknown
                         else "accepted"),
    }
