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

from .filters import (CHANNELS, canonical as _canonical, channels as _channels,
                      is_narrowband as _is_narrowband, is_osc as _is_osc,
                      normal_channel as _normal)


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
DEFAULT_SERVER = "https://collab.starfront.space"

#: Addresses the server used to live at. A rig that typed one of these into
#: Advanced is pointed at the current one rather than left on a name that
#: will one day stop answering.
LEGACY_SERVERS = ("https://starfront-bray.duckdns.org",)

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


def _clip(polygon: list[tuple[float, float]], axis: int, limit: float,
          keep_below: bool) -> list[tuple[float, float]]:
    """Cut a convex polygon by one axis-aligned half-plane (Sutherland-Hodgman)."""
    out: list[tuple[float, float]] = []
    if not polygon:
        return out
    inside = (lambda p: p[axis] <= limit) if keep_below else (lambda p: p[axis] >= limit)
    previous = polygon[-1]
    for point in polygon:
        if inside(point):
            if not inside(previous):
                t = (limit - previous[axis]) / (point[axis] - previous[axis])
                out.append((previous[0] + (point[0] - previous[0]) * t,
                            previous[1] + (point[1] - previous[1]) * t))
            out.append(point)
        elif inside(previous):
            t = (limit - previous[axis]) / (point[axis] - previous[axis])
            out.append((previous[0] + (point[0] - previous[0]) * t,
                        previous[1] + (point[1] - previous[1]) * t))
        previous = point
    return out


def frame_corners(shot: Region, about: Region) -> list[tuple[float, float]]:
    """The four corners of a turned frame on the flat sky about another
    rectangle's centre: x east in degrees of sky, y north. Position angle
    is the frame's up, measured from north through east."""
    cosine = max(math.cos(math.radians((shot.dec + about.dec) / 2.0)), 1e-6)
    cx = (((shot.ra - about.ra + 180.0) % 360.0) - 180.0) * cosine
    cy = shot.dec - about.dec
    angle = math.radians(shot.rotation or 0.0)
    up = (math.sin(angle), math.cos(angle))
    across = (math.cos(angle), -math.sin(angle))
    w, h = abs(shot.width) / 2.0, abs(shot.height) / 2.0
    return [(cx + u * across[0] + v * up[0], cy + u * across[1] + v * up[1])
            for u, v in ((-w, -h), (w, -h), (w, h), (-w, h))]


def shot_overlap_area(shot: Region, cell: Region) -> float:
    """Square degrees a frame shot at an angle shares with a north-up cell.

    `overlap_area` treats both rectangles as north-up, which a cell is and
    a frame is not: a 2 by 1 degree camera at a position angle of 90 covers
    1 by 2 degrees of sky, and credited north-up it put its seconds on the
    cells beside the field instead of the ones above and below. The frame's
    real corners are cut by the cell's four edges and the piece left is
    measured, so the depth map shows the union of the fields of view as they
    were really turned. The cardinal angles take the cheap path.
    """
    turn = (shot.rotation or 0.0) % 180.0
    if min(turn, 180.0 - turn) < 0.5:
        return overlap_area(shot, cell)
    if abs(turn - 90.0) < 0.5:
        return overlap_area(Region(ra=shot.ra, dec=shot.dec, width=shot.height,
                                   height=shot.width), cell)
    polygon = frame_corners(shot, cell)
    half_w, half_h = abs(cell.width) / 2.0, abs(cell.height) / 2.0
    polygon = _clip(polygon, 0, half_w, True)
    polygon = _clip(polygon, 0, -half_w, False)
    polygon = _clip(polygon, 1, half_h, True)
    polygon = _clip(polygon, 1, -half_h, False)
    if len(polygon) < 3:
        return 0.0
    area = 0.0
    for i, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(i + 1) % len(polygon)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


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


def camera_grid(region: Region, field_width: float, field_height: float,
                rotation: float, overlap: float = 0.1) -> list[dict[str, Any]]:
    """A fixed camera's tiling of a region: the grid its program will shoot.

    A camera with no rotator shoots every panel at the angle it sits at, so
    its program lays the mosaic *along the camera's axes*, big enough to
    cover the north-up region measured in that frame. The server used to
    tile the same region north-up with the turned frame's bounding box
    instead, which gave a different grid - twelve cells against the
    program's fifteen panels - and matching the two by nearest centre then
    sent a share of three neighbouring cells to three panels that were not
    neighbours, with a gap in the walk and sky the deal did not mean.

    So the server lays the grid exactly as the program does, with the same
    arithmetic, in the same order: cell `i` here is panel `i + 1` there.
    Each cell is stored with the bounding box the turned frame really spans,
    which is what the depth map and the "not where somebody else is" rule
    measure overlap with.
    """
    from . import framing
    across, down = camera_frame(region, rotation)
    columns = tiles_across(across, field_width, overlap)
    rows = tiles_across(down, field_height, overlap)
    span_x, span_y = footprint(field_width, field_height, rotation)
    cells = []
    for panel in framing.mosaic_panels(region.ra, region.dec, field_width, field_height,
                                       rows=rows, columns=columns, overlap=overlap,
                                       position_angle=rotation, align="fixed"):
        cells.append({"row": int(panel["row"]), "column": int(panel["column"]),
                      **Region(ra=float(panel["ra"]) * 15.0, dec=float(panel["dec"]),
                               width=span_x, height=span_y,
                               rotation=float(rotation) % 360.0).payload()})
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
            fraction = min(1.0, shot_overlap_area(shot, cell) / area)
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
                 min_frames: int,
                 need: dict[str, Any] | None = None) -> dict[str, int]:
    """How one visit's seconds fall across the filters, as frames.

    Without `need`, split in proportion to how deep the project wants each
    filter, and never fewer than `min_frames` of any — a stack is per
    filter, and a filter given two subs on a panel has nothing to stack.

    With `need` — per canonical filter name, the seconds still wanted
    (`left`) and the goal in seconds (`goal`), after what the field already
    has and what the other rigs are putting in tonight — the visit is
    poured into the thinnest filter first. Thinnest is measured against the
    goal: a filter at a tenth of what was asked is thinner than one at a
    third, whatever the hours say, so a project that asks five hundred
    hours of Ha and a hundred of blue is kept in that proportion. The
    filter furthest behind gets the seconds until it is level with the
    next, then the two share them, and so on down. A filter the pour never
    reaches is not shot tonight and is not named; one reached for less than
    `min_frames` gets `min_frames`, since fewer will not stack. The frames
    come back in the order they should be shot, thinnest filter first, so a
    night that is cut short loses frames from the deepest filter and not
    from the one everybody is short of. A single-field collaboration that
    split every night evenly by the goals and shot the filters in the
    project's order sat at a third of the depth in blue, which came last
    in every rig's night and was the first thing dawn took away.
    """
    usable = [f for f in filters if f.exposure > 0]
    frames: dict[str, int] = {}
    if need is not None:
        left_for: dict[str, float] = {}
        goal_of: dict[str, float] = {}
        for f in usable:
            entry = need.get(_key(f.filter))
            if isinstance(entry, dict):
                left_for[f.filter] = max(0.0, float(entry.get("left") or 0.0))
                goal_of[f.filter] = max(float(entry.get("goal") or 0.0), left_for[f.filter], 1.0)
            else:
                left_for[f.filter] = max(0.0, float(entry or 0.0))
                goal_of[f.filter] = max(left_for[f.filter], 1.0)
        order = sorted((f for f in usable if left_for[f.filter] > 0),
                       key=lambda f: (-left_for[f.filter] / goal_of[f.filter], usable.index(f)))
        while order:
            given = _pour(order, left_for, goal_of, max(0.0, seconds))
            # A filter the pour reached for less than a stack's worth is
            # dropped and the pour run again without it, so its seconds go
            # to the filters above it rather than being rounded up into
            # frames the night does not have. The last filter standing gets
            # a stack's worth whatever the budget.
            slivers = [f for f in order
                       if 0 < given[f.filter] < min_frames * f.exposure - 1e-9]
            if slivers and len(order) > 1:
                order.remove(slivers[-1])
                continue
            for f in order:
                if given[f.filter] <= 0:
                    continue
                frames[f.filter] = max(min_frames, int(math.floor(given[f.filter] / f.exposure)))
            return frames
    total = sum(max(0.0, f.hours) for f in usable)
    for f in usable:
        weight = (f.hours / total) if total > 0 else (1.0 / max(1, len(usable)))
        want = int(math.floor(seconds * weight / f.exposure))
        frames[f.filter] = max(min_frames, want)
    return frames


def _pour(order: list["FilterTask"], left_for: dict[str, float],
          goal_of: dict[str, float], budget: float) -> dict[str, float]:
    """Water-fill `budget` seconds into filters sorted by how far behind
    their goal each is, furthest first. The level is the fraction of the
    goal still wanted; lowering a filter's level by some amount costs that
    amount times its goal, so a filter with a big goal takes more seconds
    to move. The first takes seconds until it is level with the second,
    then both share until level with the third, and so on."""
    given = {f.filter: 0.0 for f in order}
    group: list["FilterTask"] = []
    waiting = list(order)
    level = lambda f: (left_for[f.filter] - given[f.filter]) / goal_of[f.filter]   # noqa: E731
    while budget > 1e-9 and (group or waiting):
        if not group or (waiting and level(waiting[0]) >= level(group[0]) - 1e-12):
            group.append(waiting.pop(0))
            continue
        step = max(0.0, level(group[0]) - (level(waiting[0]) if waiting else 0.0))
        weight = sum(goal_of[f.filter] for f in group)
        portion = min(budget, step * weight) if step > 0 else budget
        drop = portion / weight
        for f in group:
            given[f.filter] += drop * goal_of[f.filter]
        budget -= portion
        if waiting:
            group.append(waiting.pop(0))
        else:
            break
    return given


def filter_demand(cells: list[dict[str, Any]], goals: dict[str, float],
                  depth: dict[int, dict[str, float]]) -> dict[str, float]:
    """Seconds still wanted in each filter, summed over a rig's tiling.

    Keyed by canonical filter name. The depth map is at the resolution of
    this rig's cells, so the figure is in this rig's cell-seconds - which is
    what matters, since this rig is the one about to spend a night on it.
    """
    demand: dict[str, float] = {}
    for name, hours in goals.items():
        key = _key(name)
        left = 0.0
        for index in range(len(cells)):
            left += max(0.0, float(hours) * 3600.0 - depth.get(index, {}).get(key, 0.0))
        demand[key] = demand.get(key, 0.0) + left
    return demand


#: Filters that shoot through moonlight: the red narrowband lines. Everything
#: else - luminance, the colour filters, OIII - is washed out by a bright
#: Moon and is saved for the dark nights.
MOON_TOLERANT = frozenset({"h", "s", "n"})       # as `_key` spells them

#: How much Moon makes a night a narrowband night: the fraction of the dark
#: hours it is up, times how much of it is lit. A half Moon up half the night
#: is 0.25; a thin crescent setting at dusk is nearly nothing.
MOON_BRIGHT = 0.2


def moon_badness(moon: dict[str, Any] | None) -> float | None:
    """One number for how much the Moon spoils tonight, or None if unknown."""
    if not moon:
        return None
    try:
        lit = float(moon.get("illumination"))
        up = float(moon.get("upFraction"))
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, lit)) * max(0.0, min(1.0, up))


def choose_filter(filters: list["FilterTask"], goals: dict[str, float],
                  depth: dict[int, dict[str, float]], cells: list[dict[str, Any]],
                  spoken: dict[str, float], hours: float,
                  moon: dict[str, Any] | None = None) -> "FilterTask | None":
    """The one filter a rig shoots tonight on a mosaic.

    One filter a night per telescope: every panel it visits gets a stack in
    that filter, the wheel never turns between panels, and a night's frames
    all calibrate with one set of flats. Which filter is the collaboration's
    choice, not the rig's, and it is made in two steps.

    **The Moon first.** A bright Moon up for most of the dark hours makes it a
    night for Ha or SII, which shoot through moonlight; a dark night is spent
    on what cannot be shot any other time - luminance, the colour filters,
    OIII - and the narrowband is kept for the moonlit nights to come. Only
    filters still wanting work are considered, and when nothing of the
    preferred kind is left the other kind is used rather than nothing.

    **Then the least progress.** Among those, the filter with the most work
    still outstanding across the field once what the other telescopes are
    already putting in tonight is taken off it. Three rigs on a project
    wanting equal Ha, OIII and SII under no Moon are sent OIII, then Ha, then
    SII; when OIII is done nobody is sent to polish it while Ha is thin.

    `spoken` is the seconds already committed tonight per filter by the rigs
    dealt before this one. `moon` is the asking rig's own sky tonight, as it
    reported it; without it the Moon is not considered. Ties go to the
    project's own filter order.
    """
    usable = [f for f in filters if f.exposure > 0]
    if not usable:
        return filters[0] if filters else None
    if len(usable) == 1:
        return usable[0]
    wanted = _goals_for(usable, goals)
    demand = filter_demand(cells, wanted, depth)
    goal_seconds = {_key(name): float(hours) * 3600.0 * max(1, len(cells))
                    for name, hours in wanted.items()}
    # Behind is measured against the goal, as on a single field: a filter
    # at a tenth of what was asked is thinner than one at a third, so a
    # project asking five times the Ha keeps its Ha five times as deep. A
    # filter that delivers two channels is as far behind as the one of them
    # that is furthest behind.
    left: dict[str, float] = {}
    for f in usable:
        want, goal = _filter_need(f, demand, spoken, goal_seconds)
        left[f.filter] = want / goal if goal > 0 else want

    candidates = [f for f in usable if left[f.filter] > 0] or list(usable)
    badness = moon_badness(moon)
    if badness is not None:
        tolerant = [f for f in candidates if _moon_tolerant(f)]
        sensitive = [f for f in candidates if not _moon_tolerant(f)]
        preferred = tolerant if badness >= MOON_BRIGHT else sensitive
        if preferred:
            candidates = preferred

    best = None
    for f in candidates:
        if best is None or left[f.filter] > left[best.filter] + 1e-6:
            best = f
    return best


def _goals_for(filters: list["FilterTask"], goals: dict[str, float]) -> dict[str, float]:
    """The project's goals, plus a task's own hours for any channel it
    delivers that the project set no goal for."""
    wanted = dict(goals)
    have = {_key(name) for name in wanted}
    for f in filters:
        for key in f.keys():
            if key not in have:
                wanted[_normal(key) or f.filter] = f.hours
                have.add(key)
    return wanted


def _filter_need(f: "FilterTask", demand: dict[str, float], spoken: dict[str, float],
                 goal_seconds: dict[str, float]) -> tuple[float, float]:
    """Seconds a filter still wants and the goal they are measured against:
    of the channels it delivers, the one furthest behind its goal."""
    best: tuple[float, float] | None = None
    for key in f.keys():
        want = max(0.0, demand.get(key, 0.0) - spoken.get(key, 0.0))
        goal = goal_seconds.get(key, 0.0)
        fraction = want / goal if goal > 0 else want
        if best is None or fraction > (best[0] / best[1] if best[1] > 0 else best[0]) + 1e-9:
            best = (want, goal)
    return best or (0.0, 0.0)


def _moon_tolerant(f: "FilterTask") -> bool:
    """Whether a filter can be shot under a bright Moon: any narrowband line
    it passes is, and a dual-band's Ha half is worth the night."""
    return any(key in MOON_TOLERANT or _is_narrowband(key) for key in f.keys())


def assign(cells: list[dict[str, Any]], goals: dict[str, float],
           depth: dict[int, dict[str, float]], mine: dict[int, float],
           claimed: list[Region], hours: float, filters: list["FilterTask"],
           min_frames: int,
           need: dict[str, float] | None = None) -> tuple[list[int], dict[str, Any]]:
    """Tonight's panels for one rig, and how long to give each.

    `need`, when given, is the seconds each filter still wants once the
    field's depth and tonight's other rigs are counted; the visit is then
    poured into the thinnest filters first (see `visit_frames`).

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
    if need is None:
        floor = sum(min_frames * f.exposure for f in filters if f.exposure > 0)
    else:
        # The night is poured into the filters that want it, not spread
        # over all of them, so the least a visit can be is one stack of the
        # cheapest filter - not a stack of every filter the rig carries.
        floor = min((min_frames * f.exposure for f in filters if f.exposure > 0), default=0.0)
    floor = min(floor, full) if floor > 0 else min(full, budget)
    visits = int(budget // (floor + VISIT_OVERHEAD_SECONDS))
    visits = max(1, min(len(order), visits))
    seconds = max(floor, min(full, budget / visits - VISIT_OVERHEAD_SECONDS))
    frames = visit_frames(filters, seconds, min_frames, need)
    seconds = sum(n * f.exposure for f in filters for name, n in frames.items()
                  if name == f.filter)
    return order[:visits], {"seconds": round(seconds, 1), "frames": frames}


def night_need(filters: list["FilterTask"], goals: dict[str, float],
               depth: dict[int, dict[str, float]], cells: list[dict[str, Any]],
               spoken: dict[str, float],
               moon: dict[str, Any] | None = None) -> dict[str, float]:
    """Seconds each of a rig's filters still wants tonight, for a night that
    shoots several filters on one field.

    What the project wants of each, less what the field already has, less
    what the rigs dealt before this one are putting in tonight. The Moon
    rule is the same as `choose_filter`'s: a bright Moon leaves only the
    narrowband filters in want, a dark night the broadband ones, and when
    nothing of the preferred kind is left the rest stand. A filter the
    project sets no goal for wants the task's own hours of it.
    """
    wanted = _goals_for(filters, goals)
    demand = filter_demand(cells, wanted, depth)
    goal_seconds = {_key(name): float(hours) * 3600.0 * max(1, len(cells))
                    for name, hours in wanted.items()}
    need: dict[str, Any] = {}
    tolerant_keys: set[str] = set()
    for f in filters:
        if f.exposure <= 0:
            continue
        key = _key(f.filter)
        left, goal = _filter_need(f, demand, spoken, goal_seconds)
        need[key] = {"left": left, "goal": goal}
        if _moon_tolerant(f):
            tolerant_keys.add(key)
    badness = moon_badness(moon)
    if badness is not None:
        wanting = [key for key, value in need.items() if value["left"] > 0]
        tolerant = [key for key in wanting if key in tolerant_keys]
        sensitive = [key for key in wanting if key not in tolerant_keys]
        preferred = tolerant if badness >= MOON_BRIGHT else sensitive
        if preferred:
            for key, value in need.items():
                if key not in preferred:
                    value["left"] = 0.0
    return need


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
    #: Whether one-shot colour cameras may take part, and with what. A colour
    #: camera's data is its own band - OSC-RGB behind a clear filter,
    #: OSC-Ha+OIII behind a dual-band - kept apart from the mono channels,
    #: because a Bayer matrix's red is not a red filter's and a dual-band
    #: frame is two lines on one sensor. `acceptColour` is the old master
    #: switch; `acceptOscRgb` and `acceptOscNarrowband` say which bands, and
    #: a project takes a band only when it also lists it among its filters.
    #: `colourMaxMoon` is the Moon illumination (0-1) above which a colour
    #: camera's night is refused; None means the project's ordinary Moon
    #: rule, if any, is the only one.
    acceptColour: bool = True
    acceptOscRgb: bool = True
    acceptOscNarrowband: bool = True
    #: The worst Bortle class a contributing site may have, 1 to 9; None is
    #: any sky. A rig that has not rated its sky is told, not refused.
    maxBortle: int | None = None
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
            acceptOscRgb=bool(data.get("acceptOscRgb", True)),
            acceptOscNarrowband=bool(data.get("acceptOscNarrowband", True)),
            maxBortle=_bortle(data.get("maxBortle")),
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
    #: Filter name -> the channels a sub through it delivers. A mono Ha
    #: delivers ["H"]; a colour camera's clear filter ["R", "G", "B"]; an
    #: L-eXtreme on a colour camera ["H", "O"], and a night through it is
    #: credited to both. Sent by a program whose operator chose, or read
    #: from the name here for one that did not.
    channels: dict[str, list[str]] = field(default_factory=dict)
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
    #: The sky's Bortle class at the site, 1 (darkest) to 9, as the operator
    #: rates it; None when they have not said. A project may set a limit.
    bortle: int | None = None

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

    def delivers(self, name: str) -> list[str]:
        """The channels a sub through one of this rig's filters gives."""
        key = _key(name)
        for own, letters in self.channels.items():
            if _key(own) == key:
                return list(letters)
        return _channels(name, self.colour)

    @classmethod
    def read(cls, data: dict[str, Any]) -> "RigProfile":
        data = data or {}
        colour = bool(data.get("colour", False))
        filters = {(_canonical(name) or str(name)): _number(value)
                   for name, value in (data.get("filters") or {}).items()}
        channels: dict[str, list[str]] = {}
        for name, letters in (data.get("channels") or {}).items():
            clean = [_normal(c) for c in (letters or []) if _normal(c)]
            channels[_canonical(name) or str(name)] = clean
        for name in filters:
            if name not in channels:
                channels[name] = _channels(name, colour)
        return cls(
            name=str(data.get("name") or ""),
            focalLength=_number(data.get("focalLength")),
            pixelSize=_number(data.get("pixelSize")),
            sensorWidth=_int(data.get("sensorWidth")),
            sensorHeight=_int(data.get("sensorHeight")),
            binning=_int(data.get("binning")) or 1,
            filters=filters,
            channels=channels,
            colour=colour,
            typicalHfr=_number(data.get("typicalHfr")),
            typicalGuideRms=_number(data.get("typicalGuideRms")),
            exposures={(_canonical(name) or str(name)): float(value)
                       for name, value in (data.get("exposures") or {}).items()
                       if _number(value) and float(value) > 0},
            rotation=_number(data.get("rotation")),
            hoursPerNight=_number(data.get("hoursPerNight")),
            windowFrom=str(data.get("windowFrom") or ""),
            windowTo=str(data.get("windowTo") or ""),
            bortle=_bortle(data.get("bortle")),
        )


def _bortle(value: Any) -> int | None:
    """A Bortle class, 1 to 9, or None for unset or nonsense."""
    number = _int(value)
    return number if number is not None and 1 <= number <= 9 else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: The names a project lists to say it takes one-shot colour data. A colour
#: camera's frame is all three channels at once; "RGB" is what everybody
#: calls that, and the others are the ways people spell it.
COLOUR_FILTERS = {"rgb", "osc", "colour", "color", "one-shot colour", "one-shot color"}


def filter_channels(name: str, colour: bool = False) -> list[str]:
    """The channels a filter delivers, read from its name; see `filters.channels`."""
    return _channels(name, colour)


def wanted_channels(wants: "Requirements") -> dict[str, float | None]:
    """The channels a project asks for, as compared, each with its bandpass
    limit. "RGB" in a project's list is the three colour channels."""
    out: dict[str, float | None] = {}
    for name, limit in (wants.filters or {}).items():
        if str(name).strip().lower() in COLOUR_FILTERS:
            for letter in ("R", "G", "B"):
                out.setdefault(letter.lower(), limit)
            continue
        key = _key(name)
        # A colour-camera band listed by a project that has since switched
        # that kind of data off is not wanted; the switches win.
        if _is_osc(key):
            narrow = "+" in key
            if not wants.acceptColour:
                continue
            if narrow and not wants.acceptOscNarrowband:
                continue
            if not narrow and not wants.acceptOscRgb:
                continue
        out[key] = limit
    return out


def _delivered(profile: "RigProfile") -> dict[str, list[tuple[str, float | None]]]:
    """Channel (as compared) -> the rig's filters that deliver it, with bandpass."""
    out: dict[str, list[tuple[str, float | None]]] = {}
    if profile.colour and not profile.filters:
        # A colour camera with nothing in front of it shoots RGB, and says so
        # without anybody typing a filter name for a wheel it does not have.
        for letter in ("R", "G", "B"):
            out.setdefault(letter.lower(), []).append(("the camera", None))
    for name, band in profile.filters.items():
        for letter in profile.delivers(name):
            out.setdefault(_key(letter), []).append((name, band))
    return out


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

    # A one-shot colour camera's data is its own band. A project can decline
    # colour cameras outright, take only their broadband or only their
    # narrowband, or take them on dark nights only.
    if profile.colour:
        takes = wants.acceptColour and (wants.acceptOscRgb or wants.acceptOscNarrowband)
        if not takes:
            note("camera", False, "a one-shot colour camera; this project wants "
                                  "mono cameras with filters")
        elif wants.colourMaxMoon is not None:
            note("camera", True, "one-shot colour: nights count only with the "
                                 f"Moon under {wants.colourMaxMoon * 100:.0f}% lit")
        else:
            note("camera", True, "one-shot colour")

    # The sky. A project may ask for a dark one; a rig that has not rated
    # its sky is told what is wanted rather than turned away unheard.
    if wants.maxBortle is not None:
        if profile.bortle is None:
            note("sky", None, f"Bortle class not set; the project wants Bortle "
                              f"{wants.maxBortle} or darker")
        elif profile.bortle > wants.maxBortle:
            note("sky", False, f"Bortle {profile.bortle} sky, project wants Bortle "
                               f"{wants.maxBortle} or darker")
        else:
            note("sky", True, f"Bortle {profile.bortle}")

    # Filters: a rig takes part in whichever of the project's filters it
    # carries, and is dealt frames in those alone. A project asking for H, O,
    # R, G and B is not refusing a telescope that has only R, G and B - a
    # broadband-only rig is most of the hours a wide-field collaboration ever
    # gets, and three RASAs with one fixed filter each were being turned away
    # from a project that wanted exactly what they had. What does stop a rig
    # is carrying none of the filters wanted, and a bandpass limit is not a
    # suggestion: a filter too wide for the project is simply not one of the
    # usable ones.
    #
    # Matched without regard to case. "Ha", "ha" and "HA" are one filter, and
    # a coordinator who typed it in lowercase was refusing every rig on the
    # server for a difference nobody could see.
    wanted = wanted_channels(wants)
    usable: list[str] = []
    if wants.filters and profile.colour and not any(_is_osc(key) for key in wanted):
        # The project lists mono channels only, or switched its colour
        # bands off. A colour camera's band is never a mono channel, so
        # there is nothing here for it, and the person to tell is the
        # coordinator.
        note("filters", False,
             "this project takes no one-shot colour data; its coordinator can "
             "add OSC-RGB or OSC-Ha+OIII to its filters")
    elif wants.filters and not profile.filters and not profile.colour:
        # Not "no Ha; no R; no G; no B" - the rig has said nothing at all, and
        # the fix is one setting, not four filters. A wheel that is switched
        # off cannot be asked, so the names have to be typed in once; a
        # telescope with no wheel has to be told what is in its drawer.
        note("filters", False,
             "this telescope lists no filters - name the wheel's slots under "
             "Equipment, or for a telescope with no wheel say which filter is "
             "in its light path (Equipment > Filters > What is in each "
             "telescope; Run asks the same question every night)")
    elif wanted:
        # What the project wants are channels; what the rig has are filters,
        # each delivering one or more of them. A mono wheel's Ha delivers Ha.
        # A colour camera's clear filter delivers red, green and blue in one
        # sub, and its L-eXtreme delivers Ha and OIII at once, so an OSC rig
        # on a project wanting the lot can give it five of seven - which is
        # what the compatibility used to deny it, having no idea what an
        # "L-eXtreme" was. A filter nobody could name delivers nothing, and
        # the fix is the delivery table in the program's options.
        delivered = _delivered(profile)
        missing, why = [], []
        through: dict[str, list[str]] = {}
        for key, limit in wanted.items():
            shown = _normal(key) or key.upper()
            options = delivered.get(key) or []
            if not options:
                missing.append(shown)
                continue
            fit = [name for name, band in options
                   if limit is None or (band is not None and band <= limit)]
            if fit:
                usable.append(shown)
                if not any(_key(name) == key for name in fit):
                    through.setdefault(fit[0], []).append(shown)
                continue
            wide = [f"{name} is {band:g} nm" for name, band in options if band is not None]
            unknown = [name for name, band in options if band is None]
            if wide:
                why.append(f"{shown}: {'; '.join(wide)}, wants {limit:g} nm or narrower")
            elif unknown:
                why.append(f"{shown}: {unknown[0]} bandpass not set, wants {limit:g} nm or narrower")
        if missing:
            why.insert(0, "no " + ", ".join(missing))
        unnamed = [name for name in profile.filters if not profile.delivers(name)]
        if unnamed and not usable:
            why.append("nothing known about " + ", ".join(unnamed)
                       + " - say what each delivers under the program's filter settings")
        # A rig that carries some of what the project wants can contribute
        # those: the server deals a rig only the filters it carries, on a
        # single field as on a mosaic, so an RGB rig on a project that also
        # wants Ha and OIII shoots the R, G and B and leaves the narrowband
        # to rigs that have it. It used to be refused outright for the two
        # filters it lacked, which turned "can help with three of five" into
        # "cannot help".
        if usable:
            via = "; ".join(f"{name} gives {', '.join(letters)}" for name, letters in through.items())
            note("filters", True,
                 "can shoot " + ", ".join(usable)
                 + (f" ({via})" if via else "")
                 + (f" ({'; '.join(why)}: left to the others)" if why else ""))
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
        # The project's filters this rig can be dealt, in the project's own
        # spelling; every filter when the project names none.
        "usable": usable if wanted else list(wants.filters),
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
    """One filter's share of a task: how long a sub, and how much of it.

    The filter is the rig's own - "H" on a mono wheel, "L-eXtreme" on a
    colour camera - and `channels` says what the project gets from a sub
    through it. Empty means the one channel the name is.
    """

    filter: str
    exposure: float
    hours: float
    channels: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def read(cls, data: dict[str, Any]) -> "FilterTask":
        name = _canonical(data["filter"]) or str(data["filter"])
        letters = [_normal(c) for c in (data.get("channels") or []) if _normal(c)]
        if not letters:
            letters = _channels(name)
        return cls(filter=name,
                   exposure=float(data.get("exposure") or 0.0),
                   hours=float(data.get("hours") or 0.0),
                   channels=letters)

    def keys(self) -> list[str]:
        """The channels this filter's seconds count towards, as compared."""
        return [c.lower() for c in self.channels] or [_key(self.filter)]

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
    #: The channels the frames carry. A mono Ha night is ["H"]; a colour
    #: camera's night through an L-eXtreme is ["H", "O"], and the server
    #: records it once per channel, each with the whole night's seconds,
    #: with `sourceFilter` naming the filter it really came through.
    channels: list[str] = field(default_factory=list)
    sourceFilter: str = ""
    #: The site's Bortle class the night was shot under, as the rig rates it.
    bortle: int | None = None
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
            channels=[_normal(c) for c in (data.get("channels") or []) if _normal(c)],
            sourceFilter=str(data.get("sourceFilter") or ""),
            bortle=_bortle(data.get("bortle")),
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
    # "ha" are one filter, and a night is not thrown away over a capital. A
    # project's filters are channels; a night carries one or more of them.
    asked = wanted_channels(wants)
    carried = [_key(c) for c in contribution.channels] or [_key(contribution.filterName)]
    shown = contribution.sourceFilter or contribution.filterName
    if contribution.colour and not wants.acceptColour:
        reasons.append("shot on a one-shot colour camera; this project wants "
                       "mono cameras with filters")
    if (contribution.colour and wants.colourMaxMoon is not None
            and contribution.moonIllumination is not None):
        check(contribution.moonIllumination <= wants.colourMaxMoon,
              f"the Moon was {contribution.moonIllumination * 100:.0f}% lit, and "
              "a one-shot colour camera's night counts only under "
              f"{wants.colourMaxMoon * 100:.0f}%")
    for key in carried:
        if _is_osc(key) and "+" not in key and not wants.acceptOscRgb:
            reasons.append("one-shot colour broadband; this project does not take OSC-RGB")
        elif _is_osc(key) and "+" in key and not wants.acceptOscNarrowband:
            reasons.append("one-shot colour narrowband; this project does not take it")
    if wants.maxBortle is not None and contribution.bortle is not None:
        check(contribution.bortle <= wants.maxBortle,
              f"shot under a Bortle {contribution.bortle} sky, project wants "
              f"Bortle {wants.maxBortle} or darker")
    if asked and not any(key in asked for key in carried):
        reasons.append(f"{shown} is not a filter this project wants")
    else:
        limits = [asked[key] for key in carried if asked.get(key) is not None]
        limit = min(limits) if limits else None
        if limit is not None and contribution.bandpass is not None:
            check(contribution.bandpass <= limit,
                  f"{shown} is {contribution.bandpass:g} nm, "
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
    if wants.maxBortle is not None and contribution.bortle is None:
        unknown.append("the site's Bortle class is not set")
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
