"""A mount of fixed-filter telescopes is dealt all of its filters at once.

2026-11-04 00:01 CST: the triple RASA (one mount; rasa1, rasa2, rasa3 with
one filter each in a drawer, no wheels) was dealt "R tonight" on the M42
horse mosaic - one filter for the whole mount - and Starfront warned "The
plan asks for a filter this telescope does not have" on the telescopes that
carry G and B. The project wants R, G and B; the mount shoots all three on
every panel it visits. The real project and tasks from that night are in
data/m42horse_20261104_snapshot.json.

Run: python -m pytest tests/test_simultaneous_filters.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("STARFRONT_DATA", tempfile.mkdtemp(prefix="sf-simul-"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from astrocontrol import collab  # noqa: E402

SNAPSHOT = Path(__file__).with_name("data") / "m42horse_20261104_snapshot.json"
RASA = "c9fbb7afa00b"
TRIPLE = [{"name": "rasa1", "filters": ["B"], "wheel": False},
          {"name": "rasa2", "filters": ["G"], "wheel": False},
          {"name": "rasa3", "filters": ["R"], "wheel": False}]


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


def grid_cells(rows, columns):
    return [{"row": r, "column": c, "ra": 80.0 + 2.0 * r, "dec": -9.0 + 3.0 * c,
             "width": 2.2, "height": 3.3, "rotation": 0.0}
            for r in range(rows) for c in range(columns)]


def rgb(exposure=300.0):
    return [collab.FilterTask.read({"filter": n, "exposure": exposure, "hours": 25.0})
            for n in ("R", "G", "B")]


class Profile(unittest.TestCase):
    def test_triple_rasa_is_simultaneous(self):
        p = collab.RigProfile.read({"filters": {"R": None, "G": None, "B": None},
                                    "telescopes": TRIPLE})
        self.assertEqual(p.simultaneous(), ["B", "G", "R"])

    def test_old_profile_without_telescopes_is_not(self):
        p = collab.RigProfile.read({"filters": {"R": None, "G": None, "B": None}})
        self.assertEqual(p.simultaneous(), [])

    def test_one_telescope_is_not(self):
        p = collab.RigProfile.read({"telescopes": TRIPLE[:1]})
        self.assertEqual(p.simultaneous(), [])

    def test_a_wheel_on_the_mount_is_not(self):
        scopes = TRIPLE[:2] + [{"name": "fsq", "filters": ["L", "R", "G", "B"], "wheel": True}]
        self.assertEqual(collab.RigProfile.read({"telescopes": scopes}).simultaneous(), [])

    def test_names_are_canonical_and_junk_is_dropped(self):
        p = collab.RigProfile.read({"telescopes": [{"name": "a", "filters": ["Ha"]},
                                                   "junk", {"name": "b", "filters": ["oiii"]}]})
        self.assertEqual(len(p.telescopes), 2)
        self.assertEqual([collab._key(n) for n in p.simultaneous()],
                         [collab._key("H"), collab._key("O")])
        self.assertEqual(collab.RigProfile.read({"telescopes": "nope"}).telescopes, [])

    def test_payload_round_trips(self):
        p = collab.RigProfile.read({"telescopes": TRIPLE})
        self.assertEqual(collab.RigProfile.read(p.payload()).telescopes, p.telescopes)


class AssignTogether(unittest.TestCase):
    def test_every_filter_every_panel_one_block(self):
        cells = grid_cells(4, 7)
        hours = 9 * (9 * 300.0 + collab.VISIT_OVERHEAD_SECONDS) / 3600.0 + 0.001
        share, visit = collab.assign_together(cells, {"R": 25, "G": 25, "B": 25}, {}, {}, [],
                                              hours, rgb(), 9)
        self.assertEqual(len(share), 9)
        self.assertTrue(connected(cells, share), share)
        self.assertEqual(visit["frames"], {"R": 9, "G": 9, "B": 9})
        self.assertEqual(visit["seconds"], 2700.0)
        self.assertEqual(visit["together"], ["R", "G", "B"])

    def test_same_panels_as_one_filter_would_get(self):
        # The mount's time on the sky is the same whether it carries one
        # filter or three; only the frames per filter change.
        cells = grid_cells(4, 7)
        one, _ = collab.assign(cells, {"R": 25}, {}, {}, [], 7.18, rgb()[:1], 9)
        three, _ = collab.assign_together(cells, {"R": 25, "G": 25, "B": 25}, {}, {}, [],
                                          7.18, rgb(), 9)
        self.assertEqual(sorted(one), sorted(three))

    def test_mixed_exposures_paced_by_the_longest(self):
        filters = [collab.FilterTask.read({"filter": "R", "exposure": 300.0, "hours": 25}),
                   collab.FilterTask.read({"filter": "G", "exposure": 180.0, "hours": 25}),
                   collab.FilterTask.read({"filter": "B", "exposure": 120.0, "hours": 25})]
        _, visit = collab.assign_together(grid_cells(2, 2), {"R": 25, "G": 25, "B": 25},
                                          {}, {}, [], 1.0, filters, 3)
        seconds = visit["seconds"]
        self.assertEqual(visit["frames"]["R"], max(3, int(seconds // 300)))
        self.assertEqual(visit["frames"]["G"], max(3, int(seconds // 180)))
        self.assertEqual(visit["frames"]["B"], max(3, int(seconds // 120)))
        self.assertGreaterEqual(visit["frames"]["B"], visit["frames"]["R"])


class Redeal(unittest.TestCase):
    """`_redeal` on a copy of the real project, in a throwaway database."""

    @classmethod
    def setUpClass(cls):
        from server import app as server
        cls.server = server
        cls.snapshot = json.loads(SNAPSHOT.read_text())

    def setUp(self):
        from server.store import Store
        self.store = Store(Path(tempfile.mkdtemp(prefix="sf-simul-db-")) / "c.sqlite")
        self.server.store, self._real = self.store, self.server.store
        snap = copy.deepcopy(self.snapshot)
        p = snap["project"]
        self.store.add_project(p["id"], p["name"], p.get("coordinator") or "", p["payload"])
        for a in snap["agents"]:
            self.store.add_agent(a["id"], "t-" + a["id"], a["name"])
            self.store.seen(a["id"], a["profile"])
        for t in snap["tasks"]:
            self.store.add_task(t["payload"])
        self.project = p["id"]

    def tearDown(self):
        self.server.store = self._real

    def task_of(self, agent):
        return next(t for t in self.store.tasks_in(self.project) if t["agent"] == agent)

    def test_recorded_deal_was_one_filter_for_three_telescopes(self):
        t = self.task_of(RASA)
        self.assertEqual(t["visit"]["frames"], {"R": 9})
        self.assertEqual(len(collab.RigProfile.read(
            self.store.agent(RASA)["profile"]).telescopes), 0)

    def test_held_list_is_not_touched(self):
        # Mid-night: the RASA asks in the same night; its list must not move,
        # even with the new profile.
        profile = dict(self.store.agent(RASA)["profile"], telescopes=TRIPLE)
        self.store.seen(RASA, profile)
        before = {t["agent"]: t for t in self.store.tasks_in(self.project)}
        self.server._redeal(self.project, {RASA: "2026-11-03"})
        after = {t["agent"]: t for t in self.store.tasks_in(self.project)}
        self.assertEqual(before, after)

    def test_next_night_deals_rgb_together(self):
        profile = dict(self.store.agent(RASA)["profile"], telescopes=TRIPLE)
        self.store.seen(RASA, profile)
        others = {t["agent"]: t for t in self.store.tasks_in(self.project) if t["agent"] != RASA}
        self.server._redeal(self.project, {RASA: "2026-11-04"})
        t = self.task_of(RASA)
        frames = t["visit"]["frames"]
        self.assertEqual(sorted(frames), ["B", "G", "R"])
        self.assertTrue(all(n >= 9 for n in frames.values()), frames)
        self.assertNotIn("filter", t["visit"])
        self.assertTrue(connected(t["cells"], t["share"]), t["share"])
        # Nobody else's list moved.
        for agent, task in others.items():
            self.assertEqual(self.task_of(agent), task)

    def test_old_profile_keeps_one_filter_a_night(self):
        self.server._redeal(self.project, {RASA: "2026-11-04"})
        frames = self.task_of(RASA)["visit"]["frames"]
        self.assertEqual(len(frames), 1, frames)

    def test_single_telescope_rig_unchanged(self):
        fsq = "74adb590bd1f"
        self.server._redeal(self.project, {fsq: "2026-11-04"})
        self.assertEqual(len(self.task_of(fsq)["visit"]["frames"]), 1)

    def test_spoken_seconds_count_every_filter(self):
        # A rig dealt after the RASA sees R, G and B all spoken for tonight.
        profile = dict(self.store.agent(RASA)["profile"], telescopes=TRIPLE)
        self.store.seen(RASA, profile)
        self.server._redeal(self.project, {RASA: "2026-11-04"})
        t = self.task_of(RASA)
        seen = {}
        orig = collab.choose_filter

        def spy(filters, goals, depth, cells, spoken, *a, **k):
            seen.update(spoken)
            return orig(filters, goals, depth, cells, spoken, *a, **k)

        collab.choose_filter = spy
        try:
            self.server._redeal(self.project, {"143074d244d4": "2026-11-04"})
        finally:
            collab.choose_filter = orig
        panels = len(t["share"])
        for name, n in t["visit"]["frames"].items():
            self.assertGreaterEqual(seen.get(collab._key(name), 0.0), n * 300.0 * panels)


if __name__ == "__main__":
    unittest.main()
