"""A rig's share of a mosaic is one 4-connected block of panels.

2026-11-03 22:15 CST: FSQ106 joined the M42 mosaic after the RASA and was
dealt cells [4, 6, 7, 5, 1, 0] of its 4x2 grid - the six least shot by the
RASA, ranked one by one - with the middle pair (2, 3) left out: two islands.
The real task rows from that night are in data/m42_20261103_tasks.json.

Run: python -m unittest tests.test_contiguous_share  (or pytest)
"""
import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astrocontrol import collab  # noqa: E402

DATA = Path(__file__).with_name("data") / "m42_20261103_tasks.json"


def grid_cells(rows, columns, serpentine=True):
    cells = []
    for r in range(rows):
        cols = range(columns) if (not serpentine or r % 2 == 0) else reversed(range(columns))
        for c in cols:
            cells.append({"row": r, "column": c, "ra": 80.0 + 2.0 * r, "dec": -9.0 + 3.0 * c,
                          "width": 2.2, "height": 3.3, "rotation": 0.0})
    return cells


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


def L(exposure=300.0, hours=25.0, name="L"):
    return [collab.FilterTask.read({"filter": name, "exposure": exposure, "hours": hours})]


def visits(n):
    """Hours that buy exactly `n` visits of ten 300 s frames (+90 s each)."""
    return n * (10 * 300.0 + collab.VISIT_OVERHEAD_SECONDS) / 3600.0 + 0.001


def deal(cells, claimed_cells, hours, depth=None, mine=None, goals=None):
    claimed = [collab.Region.read(c) for c in claimed_cells]
    return collab.assign(cells, goals or {"L": 25.0}, depth or {}, mine or {}, claimed,
                         hours, L(), 10)


class RealNight(unittest.TestCase):
    def setUp(self):
        tasks = json.loads(DATA.read_text())
        self.rasa = next(t for t in tasks if t["agent"] == "c9fbb7afa00b")
        self.fsq = next(t for t in tasks if t["agent"] != "c9fbb7afa00b")

    def test_recorded_deal_was_two_islands(self):
        self.assertEqual(self.fsq["share"], [4, 6, 7, 5, 1, 0])
        self.assertFalse(connected(self.fsq["cells"], self.fsq["share"]))

    def test_fsq_now_gets_one_block_of_six(self):
        claimed = [self.rasa["cells"][i] for i in self.rasa["share"]]
        share, visit = deal(self.fsq["cells"], claimed, 6.0)
        self.assertEqual(len(share), 6)
        self.assertTrue(connected(self.fsq["cells"], share), share)
        self.assertEqual(visit["frames"], {"L": 11})

    def test_first_rig_alone_is_one_block(self):
        share, _ = deal(self.rasa["cells"], [], visits(14))
        self.assertEqual(len(share), 14)
        self.assertTrue(connected(self.rasa["cells"], share))


class SameGrid(unittest.TestCase):
    def test_rigs_in_turn_get_disjoint_blocks(self):
        cells = grid_cells(7, 4)
        claimed, shares = [], []
        for hours in (visits(14), visits(14)):
            share, _ = deal(cells, claimed, hours)
            self.assertTrue(connected(cells, share), share)
            for other in shares:
                self.assertFalse(set(share) & set(other))
            shares.append(share)
            claimed += [cells[i] for i in share]
        self.assertEqual(sorted(shares[0] + shares[1]), list(range(28)))

    def test_three_rigs_on_four_by_four(self):
        cells = grid_cells(4, 4)
        claimed, taken = [], set()
        for _ in range(3):
            share, _ = deal(cells, claimed, visits(5))
            self.assertEqual(len(share), 5)
            self.assertTrue(connected(cells, share), share)
            self.assertFalse(taken & set(share))
            taken |= set(share)
            claimed += [cells[i] for i in share]

    def test_compact_rather_than_a_strip(self):
        cells = grid_cells(6, 6)
        share, _ = deal(cells, [], visits(9))
        spots = {(cells[i]["row"], cells[i]["column"]) for i in share}
        rows = {r for r, _ in spots}
        cols = {c for _, c in spots}
        self.assertEqual((len(rows), len(cols)), (3, 3))

    def test_finished_cells_split_the_field(self):
        cells = grid_cells(3, 5)
        done = {"L": 25.0 * 3600.0}
        # Column 2 finished top to bottom: the open sky is two 3x2 pieces.
        depth = {i: {collab._key(k): v for k, v in done.items()}
                 for i, c in enumerate(cells) if c["column"] == 2}
        share, visit = deal(cells, [], visits(8), depth=depth)
        self.assertTrue(connected(cells, share), share)
        self.assertTrue(all(cells[i]["column"] != 2 for i in share))
        self.assertLessEqual(len(share), 6)
        self.assertGreater(visit["seconds"], 0)

    def test_no_grid_keeps_the_old_ranking(self):
        cells = [dict(c) for c in grid_cells(2, 2)]
        for c in cells:
            c.pop("row")
            c.pop("column")
        share, _ = deal(cells, [], visits(2))
        self.assertEqual(len(share), 2)


class Fuzz(unittest.TestCase):
    def test_always_connected(self):
        rng = random.Random(7)
        for _ in range(250):
            rows, cols = rng.randint(1, 8), rng.randint(1, 8)
            cells = grid_cells(rows, cols, serpentine=rng.random() < 0.5)
            claimed = [c for c in cells if rng.random() < 0.3]
            mine = {i: rng.choice([0.0, 0.0, 600.0]) for i in range(len(cells))}
            depth = {i: {collab._key("L"): 25.0 * 3600.0} for i in range(len(cells))
                     if rng.random() < 0.15}
            share, _ = deal(cells, claimed, rng.uniform(0.5, 10.0), depth=depth, mine=mine)
            self.assertTrue(share)
            self.assertEqual(len(share), len(set(share)))
            self.assertTrue(connected(cells, share), (rows, cols, share))


if __name__ == "__main__":
    unittest.main()
