"""The Planner's panels are exactly the server's share - one cell, one panel.

    python tools/check_collab_share_grid.py

The bug this pins down, found on a real night (m42 horse test mosaic, the
FSQ106 twin on EAGLE4PRO0258, 3 Nov 2026): the server dealt the rig eight
contiguous cells - columns 1 and 2 of every row of its 4-row by 3-column
grid - and the Planner showed six panels in two separate columns, 6 5 4 and
10 11 12, with 7 8 9 between them.

The rig has a rotator, so its profile reports no camera angle and the server
tiled the region north-up with the camera's raw field (5.3 degrees east-west,
3.5 north-south). The program laid its own grid at the project's 270 degrees,
long axis north-south - the same rows and columns, the other way round on
the sky - and matched the two by nearest centre, which folded the eight cells
onto six panels. The payload used here is that task exactly as the server
stored it (tools/fixtures/collab_task_m42_horse_eagle.json).

No test framework, for the same reason as the other checks here.
"""
import json
import os
import sys
import tempfile
import warnings as _warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()
_warnings.simplefilter("ignore")

from astrocontrol import astro, collab, main                       # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


main.watchdog.stop()
main.config.update("site", {"latitude": 31.5, "longitude": -99.4, "useMount": False})

TASK = json.loads((ROOT / "tools" / "fixtures" / "collab_task_m42_horse_eagle.json").read_text())
FIELD = (5.303290550128534, 3.537742275921165)      # EAGLE's FSQ106 field, from the cells
REGION = collab.Region.read(TASK["region"])
main.collab_client._task = TASK
main.collab_client._tasks = [TASK]
main.collab_client._open = []

# The rig on the night: a rotator, the plan's "turn to the project's angle" on.
_rotator = {"on": True}
main.collabclient.has_rotator = lambda rig: _rotator["on"]


def server_cells_of(share):
    return [TASK["cells"][i] for i in share]


def contiguous(target, chosen):
    """Every chosen panel edge-adjacent to another, as one connected block."""
    where = {p["index"]: (p["row"], p["column"]) for p in target["panels"]}
    cells = {where[i] for i in chosen}
    if not cells:
        return False
    seen, todo = set(), [next(iter(cells))]
    while todo:
        r, c = todo.pop()
        if (r, c) in seen:
            continue
        seen.add((r, c))
        todo += [n for n in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)) if n in cells]
    return seen == cells


def covers(target, chosen, share):
    """Each dealt cell has its own panel, centred within a third of a panel."""
    panels = {p["index"]: p for p in target["panels"]}
    limit = main.CELL_MATCH_FRACTION * min(FIELD)
    for cell in server_cells_of(share):
        gaps = [astro.separation_degrees(panels[i]["ra"], panels[i]["dec"],
                                         cell["ra"] / 15.0, cell["dec"]) for i in chosen]
        if min(gaps) > limit:
            return False
    return True


# -- the bug, as it was ----------------------------------------------------
old = main.targets.create("old layout", REGION.ra / 15.0, REGION.dec, rotation=270.0,
                          panel_width=FIELD[0], panel_height=FIELD[1], rows=4, columns=3,
                          align="aligned")


def old_mapping(task, target):
    chosen = []
    for index in task["share"]:
        cell = task["cells"][index]
        nearest = min(target["panels"], key=lambda p: astro.separation_degrees(
            p["ra"], p["dec"], cell["ra"] / 15.0, cell["dec"]))
        if nearest["index"] not in chosen:
            chosen.append(nearest["index"])
    return sorted(chosen)


case("the old nearest-centre mapping reproduces the screenshot (6 5 4, 10 11 12)",
     old_mapping(TASK, old) == [4, 5, 6, 10, 11, 12], str(old_mapping(TASK, old)))
mapped = main._share_panels(TASK, old)
case("the new mapping never folds two cells onto one panel, even on the old grid",
     len(mapped) == len(set(mapped)) and len(mapped) < len(TASK["share"]), str(mapped))

# -- the fix: the grid follows the server's cells ------------------------------
layout = main._collab_geometry(TASK, REGION, FIELD, True)
case("a rotator rig lays the server's 4x3 grid with the camera across it (0 or 180 deg)",
     (layout["rows"], layout["columns"]) == (4, 3)
     and main._angle_gap(layout["angle"], 0.0) % 180.0 < 1.0, str(layout))
made = main._make_target(TASK, REGION, FIELD, True)["target"]
share = made["collab"]["share"]
case("all eight dealt cells are eight panels", len(share) == 8, str(share))
case("...one contiguous block", contiguous(made, share), str(share))
case("...each centred on its own server cell", covers(made, share, TASK["share"]))
undealt = [p["index"] for p in made["panels"] if p["index"] not in share]
case("...and the four not dealt are the server's other four cells",
     len(undealt) == 4 and covers(made, undealt, [i for i in range(12) if i not in TASK["share"]]),
     str(undealt))

# -- a fixed camera on cells cut the program's way is unchanged ---------------
_rotator["on"] = False
main.rigs.master.config.update("optics", {"rotation": 270.0})
fixed_cells = collab.camera_grid(REGION, FIELD[0], FIELD[1], 270.0, 0.1)
fixed_task = {**TASK, "id": "task-fixed", "cells": fixed_cells, "share": [8, 9, 10, 11, 12, 13, 14]}
before = {"align": "fixed"}
across, down = collab.camera_frame(REGION, 270.0)
expected = (collab.tiles_across(down, FIELD[1]), collab.tiles_across(across, FIELD[0]))
layout = main._collab_geometry(fixed_task, REGION, FIELD, True)
case("a fixed camera keeps its own angle and grid when the server cut the same one",
     (layout["rows"], layout["columns"]) == expected and layout["angle"] == 270.0
     and layout["align"] == "fixed", str(layout))
fixed = main.targets.create("fixed", REGION.ra / 15.0, REGION.dec, rotation=270.0,
                            panel_width=FIELD[0], panel_height=FIELD[1],
                            rows=layout["rows"], columns=layout["columns"], align="fixed")
case("...and cell i is still panel i+1 there",
     main._share_panels(fixed_task, fixed) == [i + 1 for i in fixed_task["share"]],
     str(main._share_panels(fixed_task, fixed)))
_rotator["on"] = True

# -- a target laid by the old program is repaired on the next poll ------------
for t in main.targets.listing():
    if (t.get("collab") or {}).get("task") == TASK["id"]:
        main.targets.delete(t["id"]) if hasattr(main.targets, "delete") else None
stale = main.targets.create(
    TASK["projectName"], REGION.ra / 15.0, REGION.dec, rotation=270.0,
    panel_width=FIELD[0], panel_height=FIELD[1], rows=4, columns=3, align="aligned",
    collab={"project": TASK["project"], "projectName": TASK["projectName"],
            "task": TASK["id"], "version": TASK["version"]})
main.targets.stamp_collab(stale["id"], {"share": [4, 5, 6, 10, 11, 12]})
entry = main.plan.add(stale["id"], stale["name"])
main.plan.set_options(entry["id"], {"panels": [4, 5, 6, 10, 11, 12]})


def current():
    target, _ = main.adopted_for(TASK["id"])
    return target


type(main.sequencer).running = property(lambda self: True)
main._resync_task(TASK)
case("same task version, sequence running: the panels are not moved under it",
     current()["collab"]["share"] == [4, 5, 6, 10, 11, 12] and current()["rows"] == 4
     and current()["rotation"] == 270.0, str(current()["collab"]["share"]))
type(main.sequencer).running = property(lambda self: False)
main._resync_task(TASK)
fixed_target = current()
share = fixed_target["collab"]["share"]
case("same task version, idle: the old layout is re-laid on the server's cells",
     len(share) == 8 and contiguous(fixed_target, share)
     and covers(fixed_target, share, TASK["share"]), str(share))
stored = next(e for e in main.plan.raw()["entries"] if e["id"] == entry["id"])
case("...and the plan entry's panels follow",
     sorted(main.plans.options_for(stored).get("panels") or []) == share,
     str(main.plans.options_for(stored).get("panels")))
case("...after which a further poll changes nothing",
     not main._share_out_of_step(TASK, fixed_target, stored))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
