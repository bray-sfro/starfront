"""Only filters with a positive goal are dealt.

2026-11-04 (CST): the M42 horse mosaic (97fcae827a9f) allows L, R, G, B, H,
O and S in its requirements but has goals only for L, R, G, B and H (25 h
each). Every rig's task carried O and S at a placeholder hour, and the
dealer sent vsd100 (74adb590bd1f) out for nine OIII frames on panel 1. The
real project and tasks are in data/m42horse_20261104_snapshot.json.

Run: python -m pytest tests/test_goal_filters.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("STARFRONT_DATA", tempfile.mkdtemp(prefix="sf-goals-"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from astrocontrol import collab  # noqa: E402

SNAPSHOT = Path(__file__).with_name("data") / "m42horse_20261104_snapshot.json"
VSD = "74adb590bd1f"
RASA = "c9fbb7afa00b"
NO_GOAL = {"o", "s", "osc-rgb"}
TRIPLE = [{"name": "rasa1", "filters": ["B"], "wheel": False},
          {"name": "rasa2", "filters": ["G"], "wheel": False},
          {"name": "rasa3", "filters": ["R"], "wheel": False}]


def keys_of(task):
    return {collab._key(f["filter"]) for f in task.get("filters") or []}


def visit_keys(task):
    v = task.get("visit") or {}
    out = {collab._key(n) for n in (v.get("frames") or {})}
    if v.get("filter"):
        out.add(collab._key(v["filter"]))
    return out


def connected(cells, share):
    spots = {(cells[i]["row"], cells[i]["column"]) for i in share}
    if not spots:
        return True
    start = next(iter(spots))
    seen, todo = {start}, [start]
    while todo:
        r, c = todo.pop()
        for n in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
            if n in spots and n not in seen:
                seen.add(n)
                todo.append(n)
    return seen == spots


class Helpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server import app as server
        cls.server = server

    def test_goal_keys_positive_only(self):
        keys = self.server._goal_keys({"L": 25, "H": 10, "O": 0, "S": -1, "X": "junk"})
        self.assertEqual(keys, {collab._key("L"), collab._key("H")})

    def test_colour_goal_matches_its_task_entry(self):
        keys = self.server._goal_keys({"OSC": 35})
        entry = collab.FilterTask.read({"filter": "OSC", "exposure": 300, "hours": 35})
        self.assertTrue(self.server._dealable(entry, keys))

    def test_wanted_from_goals_ignores_allowed_without_goal(self):
        wants = collab.Requirements.read({"filters": {"L": None, "R": None, "H": 7.0,
                                                      "O": None, "S": None}})
        wanted = self.server._wanted_from_goals(wants, {"L": 25, "R": 25, "H": 25})
        self.assertEqual(list(wanted), [collab._key("L"), collab._key("R"), collab._key("H")])
        self.assertEqual(wanted[collab._key("H")], 7.0)

    def test_goal_not_in_requirements_is_still_wanted(self):
        wants = collab.Requirements.read({"filters": {"L": None}})
        wanted = self.server._wanted_from_goals(wants, {"L": 5, "H": 5})
        self.assertIn(collab._key("H"), wanted)

    def test_no_goals_falls_back_to_requirements(self):
        wants = collab.Requirements.read({"filters": {"L": None, "O": None}})
        wanted = self.server._wanted_from_goals(wants, {})
        self.assertEqual(set(wanted), {collab._key("L"), collab._key("O")})


class Redeal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server import app as server
        cls.server = server
        cls.snapshot = json.loads(SNAPSHOT.read_text())

    def setUp(self):
        from server.store import Store
        self.store = Store(Path(tempfile.mkdtemp(prefix="sf-goals-db-")) / "c.sqlite")
        self.server.store, self._real = self.store, self.server.store
        snap = copy.deepcopy(self.snapshot)
        p = snap["project"]
        self.goals = dict(p["payload"]["goals"])
        self.store.add_project(p["id"], p["name"], p.get("coordinator") or "", p["payload"])
        for a in snap["agents"]:
            self.store.add_agent(a["id"], "t-" + a["id"], a["name"])
            self.store.seen(a["id"], a["profile"])
        for t in snap["tasks"]:
            self.store.add_task(t["payload"])
        self.project = p["id"]

    def tearDown(self):
        self.server.store = self._real

    def tasks(self):
        return {t["agent"]: t for t in self.store.tasks_in(self.project)}

    def test_snapshot_has_the_problem(self):
        wants = (self.store.project(self.project)["payload"]["requirements"]["filters"])
        self.assertIn("O", wants)
        self.assertNotIn("O", self.goals)
        self.assertTrue(any(keys_of(t) & NO_GOAL for t in self.tasks().values()))

    def test_every_rig_next_night_no_o_or_s(self):
        for agent in list(self.tasks()):
            self.server._redeal(self.project, {agent: "2026-11-05"})
        for agent, task in self.tasks().items():
            self.assertFalse(keys_of(task) & NO_GOAL, (agent, task["filters"]))
            self.assertFalse(visit_keys(task) & NO_GOAL, (agent, task["visit"]))
            if task.get("share"):
                self.assertTrue(connected(task["cells"], task["share"]), (agent, task["share"]))

    def test_stale_list_with_o_is_not_dealt_o(self):
        # Even when a task still lists O and the profile carries it, the
        # dealer never picks it.
        self.server._redeal(self.project, {VSD: "2026-11-05"})
        t = self.tasks()[VSD]
        self.assertTrue(t["visit"].get("frames"))
        self.assertNotIn(collab._key(t["visit"].get("filter") or "L"), NO_GOAL)

    def test_held_list_is_not_touched_tonight(self):
        before = self.tasks()
        night = before[VSD].get("assignedNight")
        self.server._redeal(self.project, {VSD: night})
        self.assertEqual(before, self.tasks())

    def test_rasa_still_all_filters_together(self):
        profile = dict(self.store.agent(RASA)["profile"], telescopes=TRIPLE)
        self.store.seen(RASA, profile)
        self.server._redeal(self.project, {RASA: "2026-11-05"})
        t = self.tasks()[RASA]
        self.assertEqual(sorted(t["visit"]["frames"]), ["B", "G", "R"])
        self.assertTrue(connected(t["cells"], t["share"]), t["share"])

    def test_goals_untouched(self):
        for agent in list(self.tasks()):
            self.server._redeal(self.project, {agent: "2026-11-05"})
        self.assertEqual(self.store.project(self.project)["payload"]["goals"], self.goals)


if __name__ == "__main__":
    unittest.main()
