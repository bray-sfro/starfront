"""Closed collaborations must leave a rig's nightly plan.

2026-11-11 (CST): closing a project took it off the browse list but left
every accepted share in place. GET /api/v1/agent/task and the dealer kept
handing those leftovers out, so a rig that had joined two live collabs and
two later-closed ones was dealt four six-hour shares for one night, two of
them dead. The plan could not be built, and joining something new looked
broken.

Run: python -m pytest tests/test_closed_project_tasks.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("STARFRONT_DATA", tempfile.mkdtemp(prefix="sf-closed-"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

AGENT = "aaaaaaaa0001"
OPEN_ID = "bbbbbbbb0001"
CLOSED_ID = "cccccccc0001"
NIGHT = "2026-11-11"


def region():
    return {"ra": 10.684, "dec": 41.269, "width": 3.0, "height": 3.0, "rotation": 0.0}


def cell(index=0):
    return {"row": 0, "column": index, "ra": 10.684, "dec": 41.269,
            "width": 3.0, "height": 3.0, "rotation": 0.0}


def payload(project_id, project_name, task_id, share, night="2026-11-10"):
    now = time.time()
    return {
        "id": task_id,
        "project": project_id,
        "projectName": project_name,
        "agent": AGENT,
        "region": region(),
        "cells": [cell(0), cell(1)],
        "share": list(share),
        "kind": "mosaic",
        "filters": [{"filter": "L", "exposure": 300.0, "hours": 6.0}],
        "state": "accepted",
        "version": 7,
        "issued": now,
        "note": "joined by test",
        "assignedNight": night,
        "assignedAt": now - 3600,
        "visit": {},
        "dealtHours": 6.0,
        "seconds": 21600.0,
    }


class ClosedProjectTasks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server import app as server
        cls.server = server

    def setUp(self):
        from server.store import Store
        self.store = Store(Path(tempfile.mkdtemp(prefix="sf-closed-db-")) / "c.sqlite")
        self.server.store, self._real = self.store, self.server.store
        self.store.add_agent(AGENT, "tok-closed-test", "Telescope 1")
        self.store.seen(AGENT, {
            "name": "Telescope 1",
            "focalLength": 389.0,
            "pixelSize": 3.76,
            "sensorWidth": 9576,
            "sensorHeight": 6388,
            "binning": 1,
            "filters": {"L": None},
            "colour": False,
            "hoursPerNight": 6.0,
        })
        goals = {"L": 10.0}
        wants = {"filters": {"L": None}}
        self.store.add_project(OPEN_ID, "Live mosaic", "test",
                               {"region": region(), "kind": "mosaic",
                                "goals": goals, "requirements": wants})
        self.store.add_project(CLOSED_ID, "Finished mosaic", "test",
                               {"region": region(), "kind": "mosaic",
                                "goals": goals, "requirements": wants})
        self.store.set_project(CLOSED_ID,
                               self.store.project(CLOSED_ID)["payload"],
                               status="closed")
        self.store.add_task(payload(OPEN_ID, "Live mosaic", "taskopen0001", [0]))
        self.store.add_task(payload(CLOSED_ID, "Finished mosaic", "taskshut0001", [0, 1]))

    def tearDown(self):
        self.server.store = self._real

    def test_helper_drops_tasks_whose_project_is_closed(self):
        kept = self.server._open_project_tasks(self.store.tasks_for(AGENT))
        self.assertEqual([t["id"] for t in kept], ["taskopen0001"])
        self.assertEqual([t["project"] for t in kept], [OPEN_ID])

    def test_helper_drops_a_missing_project_too(self):
        ghost = payload("doesnotexist01", "Gone", "taskgone0001", [0])
        kept = self.server._open_project_tasks([ghost])
        self.assertEqual(kept, [])

    def test_redeal_leaves_a_closed_project_untouched(self):
        before = self.store.task("taskshut0001")
        changed = self.server._redeal(CLOSED_ID, {AGENT: NIGHT})
        after = self.store.task("taskshut0001")
        self.assertEqual(changed, 0)
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["share"], before["share"])
        self.assertEqual(after["assignedNight"], "2026-11-10")
        self.assertEqual(after["state"], "accepted")

    def test_redeal_still_deals_an_open_project(self):
        # Last night's list must be remade for tonight on a live collab.
        changed = self.server._redeal(OPEN_ID, {AGENT: NIGHT})
        after = self.store.task("taskopen0001")
        self.assertGreaterEqual(changed, 1)
        self.assertEqual(after["assignedNight"], NIGHT)
        self.assertEqual(after["state"], "accepted")
        self.assertTrue(after.get("share"))

    def test_current_task_hides_the_closed_share(self):
        answer = self.server.current_task(
            agent=self.store.agent(AGENT), night=NIGHT, moon=0.1, moonUp=0.0)
        ids = [t["id"] for t in answer["tasks"]]
        self.assertEqual(ids, ["taskopen0001"])
        self.assertEqual(answer["task"]["id"], "taskopen0001")
        self.assertEqual(answer["task"]["project"], OPEN_ID)
        self.assertNotIn(CLOSED_ID, answer.get("requirementsByProject") or {})
        # The leftover row is still accepted in the store; only the plan hides it.
        leftover = self.store.task("taskshut0001")
        self.assertEqual(leftover["state"], "accepted")
        self.assertEqual(leftover["assignedNight"], "2026-11-10")

    def test_current_task_is_empty_when_every_share_is_closed(self):
        open_task = self.store.task("taskopen0001")
        open_task["state"] = "declined"
        open_task["version"] = int(open_task["version"]) + 1
        self.store.set_task(open_task)
        answer = self.server.current_task(
            agent=self.store.agent(AGENT), night=NIGHT, moon=0.1, moonUp=0.0)
        self.assertIsNone(answer["task"])
        self.assertEqual(answer["tasks"], [])
        self.assertEqual(answer["version"], 0)
