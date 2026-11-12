"""A collaboration chunk on a triple RASA: every telescope shoots its own drawer.

    python tools/check_collab_multiscope.py

The bug this pins down, from DESKTOP-4PILSFA on 2026-11-03: three RASAs on
one mount, R, G and B fitted by hand in their drawers, one agent to the
server ("rasa1"). The server deals one filter a night per agent on a mosaic,
so the task's visit named R alone. The plan entry then carried R x 10 at 180 s
for the master and nothing of its own for the other two, which followed the
master's rows through their drawers - G and B at R's 180 s rather than the
project's 300 s - and the collaboration block on the Plan tab read the
master's list only: R, and a dash for G and B. The profile also told the
server the master's exposures alone ({"R": 180}), so G and B were dealt at a
fallback length.

Driven against the real application module with a stub task in place of the
collaboration server. The task and requirements are the real ones the
server holds for "m42 horse test mosaic".
"""
import os
import sys
import tempfile
import warnings as _warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()
_warnings.simplefilter("ignore")

from astrocontrol import main, plans                               # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


main.watchdog.stop()
main.config.update("site", {"latitude": 31.547, "longitude": -99.38, "useMount": False})
main.add_telescope(main.TelescopeRequest(name="rasa2"))
main.add_telescope(main.TelescopeRequest(name="rasa3"))
scopes = list(main.rigs.all)
master = main.rigs.master
rasa2, rasa3 = [r for r in scopes if r is not master]
main.set_telescope_filters(main.TelescopeFiltersRequest(
    filters={master.id: "R", rasa2.id: "G", rasa3.id: "B"}))
case("three telescopes, R G B in the drawers",
     plans.fitted_filters(scopes) == {master.id: "R", rasa2.id: "G", rasa3.id: "B"},
     str(plans.fitted_filters(scopes)))

# -- the profile names every telescope's sub lengths -------------------------
exposures = main.collab_client._default_exposures()
case("the profile's exposures cover R, G and B, not the master's R alone",
     set(exposures) >= {"R", "G", "B"}, str(exposures))
names = sorted(main.collab_client.profile()["filters"])
case("...and its filters are all three drawers", names == ["B", "G", "R"], str(names))

# -- the real task -------------------------------------------------------------
TASK = {
    "id": "653493b1b6ae", "project": "97fcae827a9f", "projectName": "m42 horse test mosaic",
    "agent": "c9fbb7afa00b", "version": 2, "state": "accepted", "kind": "mosaic",
    "region": {"ra": 84.0, "dec": -3.5, "width": 12.0, "height": 9.0, "rotation": 270.0},
    "filters": [{"filter": "R", "exposure": 180.0, "hours": 25.0, "channels": ["R"]},
                {"filter": "G", "exposure": 300.0, "hours": 25.0, "channels": ["G"]},
                {"filter": "B", "exposure": 300.0, "hours": 25.0, "channels": ["B"]}],
    "share": list(range(1, 15)),
    "visit": {"seconds": 1800.0, "frames": {"R": 10}, "filter": "R", "moon": 0.075},
}
RULES = {"97fcae827a9f": {"minFramesPerVisit": 10,
                          "filters": {n: None for n in ("L", "R", "G", "B", "H")}}}
main.collab_client._task = TASK
main.collab_client._tasks = [TASK]
main.collab_client._open = []
main.collab_client._by_project = RULES

target = main.targets.create(
    "m42 horse test mosaic", 84.0 / 15.0, -3.5, rotation=0.0,
    panel_width=3.37, panel_height=2.25, rows=2, columns=7,
    collab={"project": "97fcae827a9f", "projectName": "m42 horse test mosaic",
            "task": "653493b1b6ae", "version": 2})
main.targets.stamp_collab(target["id"], {"share": list(range(1, 15))})
target = main.targets.get(target["id"])
entry = main.plan.add(target["id"], target["name"])


def stored():
    return next(e for e in main.plan.raw()["entries"] if e["id"] == entry["id"])


def per_rig():
    e = stored()
    fitted = plans.fitted_filters(main.rigs.all)
    return {r.name: [(a["name"], a["exposure"], a["count"])
                     for a in plans.allocation_for(e, r.id, master.id, fitted)]
            for r in main.rigs.all}


# -- before: what 0.2.47-0.2.53 does with this task ----------------------------
# Written the old way (master's rows only, nobody else's), as a plan saved by
# the running version would be.
main.plan.set_filters(entry["id"], [{"name": "R", "exposure": 180.0, "count": 10}],
                      14, 1e12, main._overheads())
old = per_rig()
case("the old plan shot G and B at R's 180 s (the bug, reproduced)",
     old == {master.name: [("R", 180.0, 10)], "rasa2": [("G", 180.0, 10)],
             "rasa3": [("B", 180.0, 10)]}, str(old))

# -- reading the plan brings an old entry into step, once ----------------------
shown = main.get_plan(date=None)
row = next(e for e in shown["entries"] if e["id"] == entry["id"])
now = per_rig()
case("reading the plan gives each drawer telescope its own share",
     now == {master.name: [("R", 180.0, 10)], "rasa2": [("G", 300.0, 10)],
             "rasa3": [("B", 300.0, 10)]}, str(now))
allocs = {k: [(a["name"], a["exposure"], a["count"]) for a in v]
          for k, v in (row.get("allocations") or {}).items()}
case("the plan's per-telescope allocations say R, G and B at the project's lengths",
     allocs == {master.id: [("R", 180.0, 10)], rasa2.id: [("G", 300.0, 10)],
                rasa3.id: [("B", 300.0, 10)]}, str(allocs))
reach = {r.name: (row.get("tonight") or {}).get(r.id, {}).get("frames") for r in scopes}
case("tonight's forecast has frames for every telescope in its own filter",
     all(reach[r.name] and list(reach[r.name]) == [f]
         for r, f in zip(scopes, ["R", "G", "B"])), str(reach))
again = main.get_plan(date=None)
case("a second read changes nothing", per_rig() == now)

# -- applying a fresh deal directly ------------------------------------------------
main.plan.set_filters(entry["id"], [], 14, 1e12, main._overheads())
main._apply_task_allocation(entry["id"], TASK, target)
case("a deal applied by the poll gives the same per-telescope shares", per_rig() == now,
     str(per_rig()))
case("the master's own list is still the visit's", [
    (f["name"], f["count"]) for f in stored()["filters"]] == [("R", 10)],
    str(stored()["filters"]))

# -- the server names G for the mount: the R drawer still shoots R ---------------
g_night = {**TASK, "visit": {"seconds": 3000.0, "frames": {"G": 10}, "filter": "G"}}
main._apply_task_allocation(entry["id"], g_night, target)
got = per_rig()
case("a G night: the master shoots R (its drawer), rasa2 shoots the visit's G",
     got == {master.name: [("R", 180.0, 16)], "rasa2": [("G", 300.0, 10)],
             "rasa3": [("B", 300.0, 10)]}, str(got))

# -- a short visit never goes below the project's minimum per visit --------------
short = {**TASK, "visit": {"seconds": 600.0, "frames": {"R": 10}, "filter": "R"}}
main._apply_task_allocation(entry["id"], short, target)
case("at least minFramesPerVisit on every filter",
     all(c >= 10 for rows in per_rig().values() for _, _, c in rows), str(per_rig()))

# -- a drawer filter the project does not take sits out ---------------------------
main.set_telescope_filters(main.TelescopeFiltersRequest(filters={rasa3.id: "S"}))
main._apply_task_allocation(entry["id"], TASK, target)
got = per_rig()
case("rasa3 with S fitted sits out rather than shooting S for an RGB project",
     got["rasa3"] == [] and got["rasa2"] == [("G", 300.0, 10)], str(got))
shown = main.get_plan(date=None)
case("...and the plan read does not keep re-applying it", per_rig() == got)
main.set_telescope_filters(main.TelescopeFiltersRequest(filters={rasa3.id: "B"}))

# -- an older server with no visit: each drawer gets its filter's full depth ------
main._apply_task_allocation(entry["id"], {**TASK, "visit": {}}, target)
got = per_rig()
case("without a visit each telescope gets its own filter's full depth",
     got == {master.name: [("R", 180.0, 500)], "rasa2": [("G", 300.0, 300)],
             "rasa3": [("B", 300.0, 300)]}, str(got))

# -- telescopes with wheels follow the master as before ---------------------------
main.config.update("camera", {"filterNames": ["L", "R", "G", "B"]})
for r in scopes:
    r.config.update("camera", {"fixedFilter": ""})
main._apply_task_allocation(entry["id"], TASK, target)
case("with no drawers nobody gets a list of their own (unchanged behaviour)",
     not stored().get("rigFilters"), str(stored().get("rigFilters")))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
