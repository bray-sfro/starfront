"""Exercise the coordinator's half: tiling a region, and administering a server.

    python tools/check_coordinator.py

Two things are checked here, and they fail in very different ways.

The **tiling** is arithmetic, and the way arithmetic on the sky goes wrong is
quietly: a mosaic laid out without allowing for the convergence of the meridians
has a gap that nobody sees until six telescopes have spent a season filling in
around it. So the coverage check is the real one — every corner of the region
has to fall inside some tile, at a declination high enough that getting the
cosine wrong shows.

The **client** is a credential boundary. A coordinator token that could drive a
mount, or an agent token that could rewrite a project, would be the one failure
in this design that actually matters, so both directions are tried against a
real server over real HTTP.

No test framework, for the same reason as the other checks here.
"""

import datetime as _dt
import json
import math
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = Path(tempfile.mkdtemp())
ADMIN = "coordinator-token-for-the-check"
os.environ["ASTROCOLLAB_DATA"] = str(DATA)
os.environ["ASTROCOLLAB_ADMIN_TOKEN"] = ADMIN
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol import collab                                   # noqa: E402
from astrocontrol.collabadmin import CollabAdmin, CollabError     # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


def close(a, b, tolerance=1e-6):
    return abs(a - b) <= tolerance


# ===========================================================================
# Tiling a region
# ===========================================================================

print("\n-- covering a region --")

# A region exactly one field across wants exactly one tile. An off-by-one here
# would double the work of every collaboration that ever ran.
one = collab.chunk(collab.Region(ra=100.0, dec=0.0, width=2.0, height=1.5),
                   2.0, 1.5, overlap=0.0)
case("a region the size of the field is one chunk", len(one) == 1,
     f"got {len(one)}")
case("and the chunk sits where the region does",
     close(one[0].ra, 100.0, 1e-9) and close(one[0].dec, 0.0, 1e-9))

grid = collab.chunk(collab.Region(ra=100.0, dec=0.0, width=4.0, height=3.0),
                    2.0, 1.5, overlap=0.0)
case("four fields of sky is four chunks", len(grid) == 4, f"got {len(grid)}")

lapped = collab.chunk(collab.Region(ra=100.0, dec=0.0, width=4.0, height=3.0),
                      2.0, 1.5, overlap=0.2)
case("asking for overlap asks for more chunks", len(lapped) > len(grid),
     f"{len(grid)} without, {len(lapped)} with")

# The one that matters. At +60 degrees a degree of RA is half a degree of sky,
# so a tiling that treats `width` as sky leaves a third of the region bare.
HIGH = collab.Region(ra=90.0, dec=60.0, width=8.0, height=2.0)
tiles = collab.chunk(HIGH, 1.0, 1.0, overlap=0.1)


def inside(tile, ra, dec):
    """Is this point within the tile?

    Both sides in sky degrees: the RA difference is turned into sky by the
    cosine, and the tile's width already is sky, which is the whole point of
    the units on Region.
    """
    if abs(dec - tile.dec) > tile.height / 2 + 1e-9:
        return False
    cosine = math.cos(math.radians(dec))
    delta = ((ra - tile.ra + 180.0) % 360.0) - 180.0
    return abs(delta) * cosine <= tile.width / 2 + 1e-9


def covered(region, tiles, steps=25):
    """Every point on a grid across the region falls inside some tile."""
    missed = []
    for i in range(steps + 1):
        for j in range(steps + 1):
            ra = region.ra - region.width / 2 + region.width * i / steps
            dec = region.dec - region.height / 2 + region.height * j / steps
            if not any(inside(tile, ra, dec) for tile in tiles):
                missed.append((round(ra, 3), round(dec, 3)))
    return missed


missed = covered(HIGH, tiles)
case("a region at +60 declination is covered with no gaps", not missed,
     f"{len(missed)} points uncovered, first {missed[0] if missed else ''}")

# The next two cases look like the same check twice. They are not, and both were
# confirmed against deliberately broken tilings before being left here:
#
#   * Coverage catches a tiling whose spacing and size disagree about units -
#     212 of these 676 points fall in the gaps.
#   * Coverage cannot catch a tiling that gets the units wrong *consistently*,
#     because scaling spacing and size together still covers the region; that
#     one merely wastes half of every frame, and is caught by the size below.
#
# Remove either and one of those two goes unnoticed.
case("a chunk's size is the field's size, in sky degrees",
     close(tiles[0].width, 1.0, 1e-9) and close(tiles[0].height, 1.0, 1e-9),
     f"{tiles[0].width:.3f} x {tiles[0].height:.3f} for a 1 degree field")

# ...and the cosine turns up where it belongs: in the *spacing* of the RA
# centres. At +60 a degree of sky is two degrees of RA, so consecutive chunks
# in a row sit about twice as far apart in RA as they are wide.
row = [tile for tile in tiles if close(tile.dec, tiles[0].dec, 1e-9)]
gap = abs(((row[1].ra - row[0].ra + 180.0) % 360.0) - 180.0)
case("and chunks up there are spaced further apart in RA than they are wide",
     1.6 < gap / row[0].width < 2.4, f"{gap:.3f} degrees of RA apart")

equator = collab.chunk(collab.Region(ra=90.0, dec=0.0, width=2.0, height=2.0),
                       1.0, 1.0, overlap=0.0)
case("and at the equator the spacing matches the size",
     close(abs(equator[1].ra - equator[0].ra), 1.0, 1e-3),
     f"{abs(equator[1].ra - equator[0].ra):.3f}")

case("a region is covered at the equator too",
     not covered(collab.Region(ra=90.0, dec=0.0, width=2.0, height=2.0), equator))

# A region's size is now the same number however high it sits: eight degrees of
# sky is eight degrees of sky. The stored width being a coordinate reading was
# a real bug - a project drawn as 6 degrees wide at +30 was stored as 6 and
# shown as 6 while covering 5.2 of actual sky.
case("the same rectangle needs the same chunks at any declination",
     len(collab.chunk(collab.Region(ra=90.0, dec=0.0, width=4.0, height=2.0),
                      1.0, 1.0, 0.0))
     == len(collab.chunk(collab.Region(ra=90.0, dec=60.0, width=4.0, height=2.0),
                         1.0, 1.0, 0.0)))

# Crossing 0h has to come out as a real coordinate rather than a negative one.
wrapped = collab.chunk(collab.Region(ra=0.5, dec=0.0, width=3.0, height=1.0),
                       1.0, 1.0, overlap=0.0)
case("a region across 0h stays in 0-360",
     all(0.0 <= tile.ra < 360.0 for tile in wrapped),
     str([round(t.ra, 2) for t in wrapped]))

case("a field of nothing does not divide by zero",
     collab.chunk(HIGH, 0.0, 0.0) == [HIGH])

# ---------------------------------------------------------------- dealing out
print("\n-- dealing the chunks out --")

shares = collab.share_out(tiles, ["a", "b", "c"])
case("every chunk is dealt to somebody",
     sum(len(regions) for regions in shares.values()) == len(tiles))
case("and nobody gets two of the same",
     len({id(tile) for regions in shares.values() for tile in regions}) == len(tiles))
counts = sorted(len(regions) for regions in shares.values())
case("the deal is even to within one", counts[-1] - counts[0] <= 1, str(counts))

# Interleaved rather than one block each: a rig that drops out should thin the
# mosaic everywhere rather than delete an edge of it.
first_three = [agent for agent, regions in shares.items()][:3]
case("the deal is interleaved, not one block each",
     shares[first_three[0]][0] is tiles[0]
     and shares[first_three[1]][0] is tiles[1])

case("dealing to nobody deals nothing", collab.share_out(tiles, []) == {})

# -- dealt by what people can actually give ---------------------------------
print("\n-- dealing by what each rig can give --")

twelve = collab.chunk(collab.Region(ra=90.0, dec=0.0, width=12.0, height=1.0),
                      1.0, 1.0, overlap=0.0)
case("twelve chunks to deal", len(twelve) == 12, str(len(twelve)))

weighted = collab.share_out(twelve, ["big", "small"],
                            {"big": 4.0, "small": 2.0})
case("a rig with twice the hours gets twice the chunks",
     len(weighted["big"]) == 8 and len(weighted["small"]) == 4,
     f'big {len(weighted["big"])}, small {len(weighted["small"])}')
case("...and every chunk is still dealt",
     sum(len(v) for v in weighted.values()) == 12)

# Interleaving still holds under weights: the point of it is that a rig
# dropping out thins the mosaic everywhere rather than deleting an edge.
case("the heavy share is still spread across the region, not one block",
     weighted["small"][0] is not twelve[0]
     and any(tile in weighted["small"] for tile in twelve[6:]),
     "small rig's chunks: "
     + str([twelve.index(t) for t in weighted["small"]]))

# Largest remainder, not repeated rounding: three rigs over ten chunks is
# 3.33 each, and rounding each down loses one off the end. A missing chunk in a
# mosaic is a hole nobody sees until it is stacked.
ten = collab.chunk(collab.Region(ra=90.0, dec=0.0, width=10.0, height=1.0),
                   1.0, 1.0, overlap=0.0)
thirds = collab.share_out(ten, ["a", "b", "c"], {"a": 1.0, "b": 1.0, "c": 1.0})
case("an uneven split loses nothing off the end",
     sum(len(v) for v in thirds.values()) == len(ten),
     f"{sum(len(v) for v in thirds.values())} of {len(ten)}")
case("...and is as even as it can be",
     sorted(len(v) for v in thirds.values()) == [3, 3, 4],
     str(sorted(len(v) for v in thirds.values())))

case("when nobody has said, the deal is equal",
     sorted(len(regions) for regions in
            collab.share_out(twelve, ["said", "quiet"],
                             {"said": 0.0, "quiet": 0.0}).values()) == [6, 6])

# Zero does not mean zero. In the settings it means "as much of the night as
# the target is up for", so a rig that has not said is dealt the average of
# those who have. Handing no work at all to a telescope the coordinator
# deliberately picked reads as the program being broken - and it was, until a
# live run showed a selected rig quietly receiving nothing.
mixed = collab.share_out(twelve, ["keen", "quiet"], {"keen": 6.0, "quiet": 0.0})
case("a rig that has not said still gets work", len(mixed["quiet"]) > 0,
     f'keen {len(mixed["keen"])}, quiet {len(mixed["quiet"])}')
case("...an average share of it",
     len(mixed["keen"]) == 6 and len(mixed["quiet"]) == 6,
     f'keen {len(mixed["keen"])}, quiet {len(mixed["quiet"])}')

# -- how many frames cover a span ------------------------------------------
print("\n-- how many panels it takes --")

# The one that cost a live run: a chunk exactly one field across came out as
# two panels, because a 10% overlap shrinks the step below the field and one
# field stops appearing to fit inside itself. Every single-frame chunk in a
# collaboration would have been shot twice.
case("one field across is one panel, overlap or not",
     collab.tiles_across(5.303, 5.303, 0.1) == 1,
     str(collab.tiles_across(5.303, 5.303, 0.1)))
case("and a hair under is still one",
     collab.tiles_across(5.2, 5.303, 0.1) == 1)
case("a hair over is two", collab.tiles_across(5.4, 5.303, 0.1) == 2)
case("twice the field is three panels at 10% overlap",
     collab.tiles_across(10.606, 5.303, 0.1) == 3,
     str(collab.tiles_across(10.606, 5.303, 0.1)))
case("...and exactly two with no overlap",
     collab.tiles_across(10.606, 5.303, 0.0) == 2)
case("a field of nothing asks for one panel, not infinity",
     collab.tiles_across(10.0, 0.0) == 1)

# The property that must survive the fix: the panels still reach across.
for span, field, lap in ((12.0, 5.303, 0.1), (10.0, 1.0, 0.0),
                         (7.72, 5.303, 0.1), (24.0, 3.54, 0.15)):
    count = collab.tiles_across(span, field, lap)
    reach = field * (1 - lap) * (count - 1) + field
    if not case(f"{count} panels still cover {span} degrees at {field:g} wide",
                reach >= span - 1e-9, f"reach {reach:.3f}"):
        break

case("a region one field across is one chunk even with overlap",
     len(collab.chunk(collab.Region(ra=100.0, dec=0.0, width=5.303,
                                    height=3.54), 5.303, 3.54, 0.1)) == 1)

# -- availability travels with the profile ----------------------------------
profile = collab.RigProfile.read({
    "name": "night owl", "focalLength": 500.0, "pixelSize": 3.76,
    "sensorWidth": 6000, "sensorHeight": 4000,
    "hoursPerNight": 3.5, "windowFrom": "21:00", "windowTo": "01:00"})
case("what a rig can give survives the wire",
     profile.hoursPerNight == 3.5 and profile.windowFrom == "21:00"
     and profile.payload()["windowTo"] == "01:00")
case("and a rig that has not said comes back as not said",
     collab.RigProfile.read({"name": "x"}).hoursPerNight is None)

# -- "nine till one" as two moments tonight ---------------------------------
print("\n-- the part of the night somebody gives --")

from astrocontrol import schedule                                 # noqa: E402

EVENING = _dt.datetime(2026, 9, 17, 21, 30).astimezone().timestamp()

start, end = schedule.clock_window("21:00", "01:00", EVENING)
case("a window is two real moments", start is not None and end is not None)
case("...the end after the start", end > start,
     f"{(end - start) / 3600:.1f} hours")
case("...and it is four hours long, not twenty short",
     abs((end - start) / 3600.0 - 4.0) < 1e-6,
     f"{(end - start) / 3600:.2f} hours")

# The trap this exists to avoid: one in the morning belongs to the night that
# started the evening before. Computed naively it lands twenty hours in the
# past, the plan reads "its end time has passed", and the target is skipped
# every single night without ever saying why.
case("one in the morning is ahead of a nine-thirty evening, not behind it",
     end > EVENING, f"{(end - EVENING) / 3600:.1f} hours away")

# ...and asked at two in the morning, the same window is the one now running,
# not tomorrow's.
SMALL_HOURS = _dt.datetime(2026, 9, 18, 2, 0).astimezone().timestamp()
start2, end2 = schedule.clock_window("21:00", "01:00", SMALL_HOURS)
case("asked at two in the morning it is still this night's window",
     start2 < SMALL_HOURS and abs(start2 - start) < 1e-6,
     _dt.datetime.fromtimestamp(start2).isoformat())

case("a blank end is an open one",
     schedule.clock_window("22:00", "")[1] is None)
case("both blank is no window at all",
     schedule.clock_window("", "") == (None, None))
case("nonsense is no window rather than midnight",
     schedule.clock_window("half nine", "25:70") == (None, None))


# ===========================================================================
# The coordinator client, against a real server
# ===========================================================================

print("\n-- administering a real server --")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"


def serve():
    import uvicorn
    from server.app import app
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")


threading.Thread(target=serve, daemon=True).start()

for _ in range(100):
    try:
        urllib.request.urlopen(f"{BASE}/api/v1/health", timeout=1).read()
        break
    except Exception:
        time.sleep(0.2)
else:
    print("FAIL  the server did not start")
    sys.exit(1)


class FakeConfig:
    """Just enough of Config for the client, with nothing on disk."""

    def __init__(self, values):
        self.values = values

    def get(self, section, key, default=None):
        return self.values.get(section, {}).get(key, default)


admin = CollabAdmin(FakeConfig({"collab": {"serverUrl": BASE,
                                           "adminToken": ADMIN}}))

case("the client finds the server", admin.health().get("ok") is True)
case("and reports that it will take orders",
     admin.health().get("adminConfigured") is True)
case("and knows it has a coordinator token", admin.configured())

made = admin.add_agent("Check scope", "the check")
AGENT_TOKEN = made["token"]
AGENT_ID = made["agent"]["id"]
case("it can enrol a telescope", bool(AGENT_TOKEN and AGENT_ID))
case("and the token comes back outside the record, once",
     "token" not in made["agent"])

case("the enrolled telescope is listed",
     any(agent["id"] == AGENT_ID for agent in admin.agents()))

project = admin.add_project(
    "Check project",
    region={"ra": 90.0, "dec": 60.0, "width": 8.0, "height": 2.0},
    requirements={"maxHfr": 3.5, "filters": {"Ha": 3.0}},
    goals={"Ha": 10.0})["project"]
case("it can start a project", bool(project.get("id")))
case("and the project comes back with its region",
     admin.project(project["id"])["project"]["payload"]["region"]["dec"] == 60.0)

# --------------------------------------------------------- the two tokens
print("\n-- the two credentials --")

as_agent = CollabAdmin(FakeConfig({"collab": {"serverUrl": BASE,
                                              "adminToken": AGENT_TOKEN}}))
try:
    as_agent.add_agent("should not happen")
    case("a telescope's token cannot enrol telescopes", False)
except CollabError as exc:
    case("a telescope's token cannot enrol telescopes", exc.status == 401,
         str(exc))

# ...and the other way. The coordinator token is not an agent, so the server
# has nothing to give it when it asks what to shoot.
request = urllib.request.Request(f"{BASE}/api/v1/agent/task",
                                 headers={"Authorization": f"Bearer {ADMIN}"})
try:
    urllib.request.urlopen(request, timeout=10)
    case("the coordinator's token cannot ask for work", False)
except urllib.error.HTTPError as exc:
    case("the coordinator's token cannot ask for work", exc.code == 401)

blank = CollabAdmin(FakeConfig({"collab": {"serverUrl": BASE}}))
case("without a coordinator token, nothing is configured", not blank.configured())
try:
    blank.agents()
    case("and an administering call says so rather than trying", False)
except CollabError as exc:
    case("and an administering call says so rather than trying",
         "token" in str(exc), str(exc))

# Reading a project is open: browsing is how somebody finds a collab to join.
case("but a project can still be read without one",
     any(row["id"] == project["id"] for row in blank.projects()))

# --------------------------------------------------- when it is not running
print("\n-- when the server is not there --")

nowhere = CollabAdmin(FakeConfig(
    {"collab": {"serverUrl": f"http://127.0.0.1:{free_port()}",
                "adminToken": ADMIN}}))
try:
    nowhere.health()
    case("an unreachable server raises rather than hanging", False)
except CollabError as exc:
    case("an unreachable server raises rather than hanging", exc.status is None,
         str(exc)[:60])

offline = nowhere.status()
case("...and status still returns something to draw", offline["online"] is False)
case("...saying what went wrong", bool(offline["error"]))
case("...rather than throwing", offline["projects"] == [])

# The one that keeps the tab drawable: a server that is up but refuses the
# token has to be distinguishable from one that is not up at all.
wrong = CollabAdmin(FakeConfig({"collab": {"serverUrl": BASE,
                                           "adminToken": "not-the-token"}}))
refused = wrong.status()
case("a server that is up but refuses the token still reads as up",
     refused["online"] is True and bool(refused["error"]))

good = admin.status()
case("and a working one lists what is on it",
     good["online"] and good["agents"] and good["projects"])


# ===========================================================================
# What a whole hand-out looks like
# ===========================================================================

print("\n-- handing work out --")

region = collab.Region.read(
    admin.project(project["id"])["project"]["payload"]["region"])
plan = collab.chunk(region, 1.0, 1.0, overlap=0.1)
deal = collab.share_out(plan, [AGENT_ID])

for chunk_region in deal[AGENT_ID][:3]:
    admin.add_task(project["id"], AGENT_ID, chunk_region.payload(),
                   [{"filter": "Ha", "exposure": 300.0, "hours": 1.0}])

detail = admin.project(project["id"])
case("the tasks are on the project", len(detail["tasks"]) == 3,
     str(len(detail["tasks"])))
case("and every one of them is offered, not started",
     all(task["state"] == "offered" for task in detail["tasks"]))

# An agent that asks now gets one, which is the whole point of the exercise.
ask = urllib.request.Request(f"{BASE}/api/v1/agent/task",
                             headers={"Authorization": f"Bearer {AGENT_TOKEN}"})
with urllib.request.urlopen(ask, timeout=10) as response:
    answer = json.loads(response.read().decode())
case("and the telescope is given one when it asks",
     (answer.get("task") or {}).get("project") == project["id"])
case("along with what the project will accept",
     answer.get("requirements", {}).get("maxHfr") == 3.5)


# ===========================================================================
# Joining a collaboration without being dealt in
# ===========================================================================

print("\n-- spreading people over a region --")

A = collab.Region(ra=100.0, dec=0.0, width=2.0, height=2.0)
case("a rectangle overlaps itself entirely",
     abs(collab.overlap_area(A, A) - 4.0) < 1e-6,
     f"{collab.overlap_area(A, A):.3f}")
case("two rectangles side by side share nothing",
     collab.overlap_area(A, collab.Region(ra=103.0, dec=0.0,
                                          width=2.0, height=2.0)) == 0.0)
case("half a rectangle over is half the area",
     abs(collab.overlap_area(A, collab.Region(ra=101.0, dec=0.0, width=2.0,
                                              height=2.0)) - 2.0) < 1e-6,
     f'{collab.overlap_area(A, collab.Region(ra=101.0, dec=0.0, width=2.0, height=2.0)):.3f}')
case("one above the other shares nothing",
     collab.overlap_area(A, collab.Region(ra=100.0, dec=3.0,
                                          width=2.0, height=2.0)) == 0.0)
# The convergence again: at +60 a degree of RA is half a degree of sky, so two
# rectangles a degree of RA apart are only half a degree apart on the sky and
# overlap more than they look like they should.
HIGH_A = collab.Region(ra=100.0, dec=60.0, width=2.0, height=2.0)
HIGH_B = collab.Region(ra=102.0, dec=60.0, width=2.0, height=2.0)
case("up at +60, two degrees of RA apart still overlap",
     collab.overlap_area(HIGH_A, HIGH_B) > 0.5,
     f"{collab.overlap_area(HIGH_A, HIGH_B):.3f} square degrees")
case("...and across 0h the wrap is taken the short way",
     collab.overlap_area(
         collab.Region(ra=359.5, dec=0.0, width=2.0, height=2.0),
         collab.Region(ra=0.5, dec=0.0, width=2.0, height=2.0)) > 0.9)

print("\n-- which chunk a joiner is handed --")

region = collab.Region(ra=100.0, dec=0.0, width=6.0, height=2.0)
cells = collab.chunk(region, 2.0, 2.0, 0.0)
case("the region divides into three", len(cells) == 3, str(len(cells)))

case("the first to join gets the first chunk",
     collab.least_covered(cells, []) is cells[0])

# ...and the second joiner must not be sent to the same patch of sky. This is
# the whole of what replaces a coordinator dealing the cards.
second = collab.least_covered(cells, [cells[0]])
case("the second is sent somewhere else", second is not cells[0],
     f"chunk {cells.index(second)}")
third = collab.least_covered(cells, [cells[0], second])
case("and the third somewhere else again",
     third is not cells[0] and third is not second,
     f"chunk {cells.index(third)}")
case("...so three joiners cover the region between them",
     {cells.index(c) for c in (cells[0], second, third)} == {0, 1, 2})

# Chunks from different cameras do not line up, and must not have to: depth is
# integration time at a point on the sky, not a tick against a grid cell.
wide = collab.chunk(region, 3.0, 2.0, 0.0)
mixed = collab.least_covered(wide, [cells[0]])
case("a different camera's chunks need not line up with anyone's",
     mixed is not None and mixed is not wide[0],
     f"picked {wide.index(mixed)} of {len(wide)}")

case("nothing to choose from is nothing", collab.least_covered([], []) is None)
case("everything taken still answers rather than failing",
     collab.least_covered(cells, cells) is not None)

print("\n-- a share of the whole mosaic, not one cell of it --")

# The bug this replaces: a rig joining a forty-panel mosaic was handed exactly
# one cell, and its target on the plan was a single frame with no sign of the
# other thirty-nine. A rig alone on a project owns all of it.
ORION = collab.Region(ra=84.0, dec=0.0, width=12.0, height=6.0)
mine = collab.grid(ORION, 3.0, 2.0, 0.0)
case("a rig tiles the whole region", len(mine) == 4 * 3, f"{len(mine)} cells")
case("...and every cell knows its row and column",
     {(c["row"], c["column"]) for c in mine} == {(r, c) for r in range(3)
                                                   for c in range(4)})
case("...in the same order chunk makes them",
     [collab.Region.read(c).ra for c in mine]
     == [t.ra for t in collab.chunk(ORION, 3.0, 2.0, 0.0)])

alone = collab.claim(mine, [], 1.0)
case("a rig alone on a project is given all of it", len(alone) == len(mine),
     f"{len(alone)} of {len(mine)}")

# ...including when the tiles overlap, which is the real case and the one that
# was wrong: overlapping cells add up to more sky than the region, so a share
# measured against the region's area stopped short of the last few.
lapped = collab.grid(ORION, 3.0, 2.0, 0.1)
case("...even when its tiles overlap",
     len(collab.claim(lapped, [], 1.0)) == len(lapped),
     f"{len(collab.claim(lapped, [], 1.0))} of {len(lapped)}")

# Two rigs, one giving twice the hours: the split is of *area*, and the busier
# one takes about two thirds. Their cameras differ, so their cells differ, and
# the second takes the cells of its own tiling least trodden by the first.
first = collab.claim(mine, [], 2 / 3)
held = [collab.Region.read(mine[i]) for i in first]
theirs = collab.grid(ORION, 2.0, 2.0, 0.0)          # a narrower camera
second = collab.claim(theirs, held, 1 / 3)
first_area = sum(collab.Region.read(mine[i]).area() for i in first)
second_area = sum(collab.Region.read(theirs[i]).area() for i in second)
case("the busier rig holds about two thirds of the sky",
     0.6 < first_area / ORION.area() < 0.75,
     f"{first_area / ORION.area():.0%}")
case("...and the other about a third", 0.25 < second_area / ORION.area() < 0.45,
     f"{second_area / ORION.area():.0%}")
overlap = sum(collab.overlap_area(collab.Region.read(theirs[j]), h)
              for j in second for h in held)
case("...on different sky, even though their cells do not line up",
     overlap < second_area * 0.35,
     f"{overlap:.1f} of {second_area:.1f} square degrees shared")

# Frames that have actually come in count as covered. A cell somebody has
# already shot to depth is the last one a newcomer should be sent to.
shot = [collab.Region.read(mine[0]), collab.Region.read(mine[1])]
late = collab.claim(mine, shot, 1 / 4)
case("cells already shot are the last to be handed out",
     0 not in late and 1 not in late, str(late))

case("a share is never empty", collab.claim(mine, [], 0.0) == [0])
case("...and no cells is no share", collab.claim([], [], 10.0) == [])

print("\n-- filter names are not case-sensitive --")

# A coordinator who typed "ha" into the project was refusing every rig on the
# server, all of which carried "Ha". One filter, three spellings.
rig = collab.RigProfile.read({"name": "r", "focalLength": 389.0, "pixelSize": 3.76,
                              "sensorWidth": 9576, "sensorHeight": 6388,
                              "filters": {"Ha": 3.0, "OIII": 3.0, "L": None}})
for spelling in ("ha", "HA", "Ha", " ha "):
    fit = collab.compatibility(rig, collab.Requirements.read(
        {"filters": {spelling: 3.0}}))
    if not case(f"a project asking for {spelling.strip()!r} takes a rig with 'Ha'",
                fit["ok"] is True, fit["summary"]):
        break
case("...and the bandpass limit still binds whatever the case",
     collab.compatibility(rig, collab.Requirements.read(
         {"filters": {"oiii": 2.0}}))["ok"] is False)
case("...and a filter the rig really lacks is still refused",
     collab.compatibility(rig, collab.Requirements.read(
         {"filters": {"SII": 3.0}}))["ok"] is False)

# The verdict on a night is judged the same way.
night = collab.Contribution.read({"filterName": "ha", "exposure": 600,
                                  "bandpass": 3.0, "seconds": 3600})
verdict = collab.judge(night, collab.Requirements.read({"filters": {"Ha": 3.0}}))
case("a night shot through 'ha' counts towards a project wanting 'Ha'",
     verdict["accepted"] is True, verdict["summary"])

# A rig that has said nothing about its filters is told what to do, not
# handed a list of everything it lacks. The wheel is off in the afternoon and
# cannot be asked; the names are typed in once.
bare = collab.RigProfile.read({"name": "bare", "focalLength": 389.0,
                               "pixelSize": 3.76, "sensorWidth": 9576,
                               "sensorHeight": 6388, "filters": {}})
unsaid = collab.compatibility(bare, collab.Requirements.read(
    {"filters": {"Ha": None, "R": None, "G": None, "B": None}}))
case("a rig with no filters listed is refused", unsaid["ok"] is False)
case("...with one instruction, not four absences",
     "Equipment" in unsaid["summary"] and "no R" not in unsaid["summary"],
     unsaid["summary"])

print("\n-- focal length, and one-shot colour cameras --")

# The limit a coordinator sets is focal length: the number everybody knows
# about their own telescope. "1.46 arcseconds a pixel" means something to few.
fl = collab.RigProfile.read({"name": "fl", "focalLength": 530.0, "pixelSize": 3.76,
                             "sensorWidth": 6248, "sensorHeight": 4176,
                             "filters": {"Ha": 3.0}})
case("a rig inside the focal length range is taken",
     collab.compatibility(fl, collab.Requirements.read(
         {"minFocalLength": 400, "maxFocalLength": 800}))["ok"] is True)
short = collab.compatibility(fl, collab.Requirements.read({"minFocalLength": 800}))
case("...one too short is refused, in millimetres",
     short["ok"] is False and "530 mm is shorter" in short["summary"], short["summary"])
case("...one too long is refused",
     collab.compatibility(fl, collab.Requirements.read(
         {"maxFocalLength": 400}))["ok"] is False)

# A one-shot colour camera is broadband whatever it does.
osc = collab.RigProfile.read({"name": "osc", "focalLength": 530.0, "pixelSize": 3.76,
                              "sensorWidth": 6248, "sensorHeight": 4176,
                              "colour": True, "filters": {}})
refused = collab.compatibility(osc, collab.Requirements.read(
    {"acceptColour": False, "filters": {"RGB": None}}))
case("a project for mono cameras refuses a colour camera",
     refused["ok"] is False and "one-shot colour" in refused["summary"],
     refused["summary"])
case("a project listing RGB takes a colour camera with no filters typed in",
     collab.compatibility(osc, collab.Requirements.read(
         {"filters": {"RGB": None}}))["ok"] is True)
narrowband = collab.compatibility(osc, collab.Requirements.read(
    {"filters": {"Ha": 3.0, "OIII": 3.0}}))
case("...and a narrowband-only project refuses it, saying what it shoots",
     narrowband["ok"] is False and "shoots RGB" in narrowband["summary"],
     narrowband["summary"])

# Its nights count only when the Moon is down, if the project says so.
rule = collab.Requirements.read({"filters": {"RGB": None}, "colourMaxMoon": 0.3})
bright = collab.judge(collab.Contribution.read(
    {"filterName": "RGB", "exposure": 120, "seconds": 3600, "colour": True,
     "moonIllumination": 0.8}), rule)
dark = collab.judge(collab.Contribution.read(
    {"filterName": "RGB", "exposure": 120, "seconds": 3600, "colour": True,
     "moonIllumination": 0.1}), rule)
mono = collab.judge(collab.Contribution.read(
    {"filterName": "RGB", "exposure": 120, "seconds": 3600, "colour": False,
     "moonIllumination": 0.8}), rule)
case("a colour camera's night under a bright Moon is refused",
     bright["accepted"] is False, bright["summary"])
case("...the same night under a dark sky counts", dark["accepted"] is True)
case("...and the colour Moon rule does not touch a mono camera",
     mono["accepted"] is True, mono["summary"])

print("\n-- a camera that cannot turn --")

# A 5.3 by 3.5 degree field at a position angle of 268 has its long axis
# running north-south: it covers 3.5 degrees of RA and 5.3 of declination.
# Tiling a north-up region with the raw numbers puts the columns where the
# rows should be, and hands out cells the camera cannot cover in a frame.
across, down = collab.footprint(5.3, 3.5, 268.0)
case("at 268 degrees the field's axes swap on the sky",
     abs(across - 3.5) < 0.2 and abs(down - 5.3) < 0.2,
     f"{across:.2f} across, {down:.2f} down")
case("a rotator reports no angle and the field is left as it is",
     collab.footprint(5.3, 3.5, None) == (5.3, 3.5))
case("at zero it is unchanged", collab.footprint(5.3, 3.5, 0.0) == (5.3, 3.5))
case("at 180 it is unchanged too",
     all(abs(a - b) < 1e-9 for a, b in
         zip(collab.footprint(5.3, 3.5, 180.0), (5.3, 3.5))))
diag = collab.footprint(5.3, 3.5, 45.0)
case("at 45 it is larger both ways, never smaller",
     diag[0] > 5.3 and diag[1] > 3.5, f"{diag[0]:.2f} x {diag[1]:.2f}")

# ...and the server cuts its cells with that footprint, so the count comes out
# the way the sky really is: the region's 16 degrees of RA at 3.5 a frame is
# five across, its 8 of declination at 5.3 is two down.
wide = collab.Region(ra=84.0, dec=0.0, width=16.0, height=8.0)
turned = collab.grid(wide, *collab.footprint(5.3, 3.5, 268.0), 0.1)
straight = collab.grid(wide, 5.3, 3.5, 0.1)
case("a turned camera's tiling has more columns than rows",
     max(c["column"] for c in turned) > max(c["row"] for c in turned),
     f'{max(c["column"] for c in turned) + 1} across, '
     f'{max(c["row"] for c in turned) + 1} down')
case("...the opposite of the same camera straight",
     max(c["column"] for c in straight) + 1 == 4
     and max(c["row"] for c in straight) + 1 == 3,
     f'{max(c["column"] for c in straight) + 1} across, '
     f'{max(c["row"] for c in straight) + 1} down')

# The program's side: a mosaic laid along the camera's axes has to be as wide
# as the region is *in the camera's frame*. Same numbers, seen from the other
# side, so the two tilings agree on how many panels there are.
frame = collab.camera_frame(wide, 268.0)
case("the region measured along a turned camera's axes swaps too",
     abs(frame[0] - 8.0) < 0.7 and abs(frame[1] - 16.0) < 0.7,
     f"{frame[0]:.2f} along the camera's width, {frame[1]:.2f} along its height")
case("...and it is unchanged for a camera at zero",
     collab.camera_frame(wide, 0.0) == (16.0, 8.0))
program_cols = collab.tiles_across(frame[0], 5.3, 0.1)
program_rows = collab.tiles_across(frame[1], 3.5, 0.1)
# Close, not equal. Each side is a bounding-box approximation from a different
# direction — the program along the camera's axes, the server north-up — and
# the corners each one adds are not the same corners. The share is matched
# between them by nearest centre, which is why a difference of a panel or two
# costs nothing; a difference of five would mean one side had the shape wrong.
case("so the program lays about as many panels as the server cut cells",
     abs(program_cols * program_rows - len(turned)) <= 2,
     f"program {program_rows}x{program_cols}={program_rows * program_cols}, "
     f"server {len(turned)}")
case("...and both put the long run of panels along the sky's width",
     program_rows > program_cols
     and max(c["column"] for c in turned) > max(c["row"] for c in turned),
     f"program {program_rows} down x {program_cols} across (camera frame), "
     f"server {max(c['column'] for c in turned) + 1} across")

# ------------------------------------------- the rig's own exposures
# A share is dealt at the rig's default exposure per filter - the length its
# darks are built for - so a project that only takes subs of a certain length
# has to say so before a night is spent, not after.
timed = collab.RigProfile.read({"name": "t", "focalLength": 389.0, "pixelSize": 3.76,
                                "sensorWidth": 9576, "sensorHeight": 6388,
                                "filters": {"L": None, "Ha": 3.0},
                                "exposures": {"L": 120, "ha": 600}})
case("a rig's default exposures travel in the one spelling",
     timed.exposures == {"L": 120.0, "H": 600.0}, str(timed.exposures))
fit = collab.compatibility(timed, collab.Requirements.read(
    {"filters": {"H": 3.0}, "minExposure": 300}))
case("a default that suits the project passes", fit["ok"] is True, fit["summary"])
fit = collab.compatibility(timed, collab.Requirements.read(
    {"filters": {"L": None}, "minExposure": 180}))
case("one that is too short is refused, naming the filter and the fix",
     fit["ok"] is False and "L at 120s" in fit["summary"] and "Equipment" in fit["summary"],
     fit["summary"])
fit = collab.compatibility(timed, collab.Requirements.read(
    {"filters": {"H": 3.0}, "maxExposure": 300}))
case("and one that is too long", fit["ok"] is False and "600s" in fit["summary"],
     fit["summary"])

# --------------------------------------------------- the project's rules
# The altitude floor and the Moon distance belong to whoever started the
# project. They travel as requirements and come out as plan-entry options.
strict = collab.Requirements.read({"minAltitude": 35, "minMoonSeparation": 60,
                                   "maxMoonIllumination": 0.4})
case("a project's altitude floor survives the round trip",
     strict.minAltitude == 35.0 and strict.payload()["minAltitude"] == 35.0)
case("...and comes out as the entry options the sequencer reads",
     strict.rules() == {"minAltitude": 35.0, "moonAvoidance": 60.0})
case("a project with no rules writes zeros, so a relaxed rule relaxes everywhere",
     collab.Requirements.read({}).rules() == {"minAltitude": 0.0, "moonAvoidance": 0.0})

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
