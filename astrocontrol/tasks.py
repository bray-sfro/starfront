"""Device commands that may take a while, run off the request (0.2.50).

A press of Park, a filter change or a slew on a driver without an async slew
used to hold its HTTP request - and the button that sent it, and one of the
server's worker threads - for as long as the hardware took. Two minutes of
"nothing happened" is a press that gets pressed again, and every second press
queued another call behind the first. Enough of them and the worker pool was
full and nothing answered at all.

`DeviceTasks.run` starts the command on a thread of its own and waits a
moment for it. A command that is done in that moment answers exactly as it
always did (and a quick failure is still the error the button shows); one that
is not answers `{"pending": true, "task": ...}` straight away, and the status
carries the task until it ends - done or the error - so the page can say so.
A second press of the same command on the same telescope while the first is
running is refused at once (`TaskBusy`) instead of queueing.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Callable

#: How long a request waits for its command before answering "started".
WAIT_SECONDS = 2.0
#: How long a finished task stays in the status, so the page sees how it ended.
KEEP_SECONDS = 30.0


class TaskBusy(RuntimeError):
    def __init__(self, task: dict[str, Any]) -> None:
        super().__init__(f"{task['label']} is already in progress "
                         f"({time.time() - task['started']:.0f}s so far)")
        self.task = task


class TaskFailed(RuntimeError):
    """The command failed inside the wait: the original exception is `cause`."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


class DeviceTasks:
    def __init__(self, log: Callable[[str, str], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._log = log

    def run(self, key: str, label: str, action: Callable[..., Any], *args: Any,
            wait: float | None = None, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            current = self._tasks.get(key)
            if current is not None and current["state"] == "running":
                raise TaskBusy(dict(current))
            task = {"id": next(self._ids), "key": key, "label": label,
                    "state": "running", "started": time.time(),
                    "finished": None, "error": None}
            self._tasks[key] = task
        done = threading.Event()
        failure: list[BaseException] = []
        answered = [False]      # under self._lock: has the request already answered "pending"?

        def body() -> None:
            try:
                action(*args, **kwargs)
                outcome, error = "done", None
            except BaseException as exc:                 # noqa: BLE001 - recorded
                failure.append(exc)
                outcome, error = "error", str(exc) or type(exc).__name__
            with self._lock:
                task.update(state=outcome, error=error, finished=time.time())
                late = answered[0]
            done.set()
            if not late:
                return                  # the request is still waiting and reports it itself
            if self._log is not None:
                if outcome == "error":
                    self._log(f"{label} failed: {error}", "error")
                else:
                    self._log(f"{label}: done", "success")

        threading.Thread(target=body, daemon=True, name=f"task-{key}").start()
        done.wait(WAIT_SECONDS if wait is None else wait)
        with self._lock:
            answered[0] = True
            finished = task["state"] != "running"
        if finished:
            if failure:
                raise TaskFailed(failure[0])
            return {"ok": True}
        return {"ok": True, "pending": True, "task": self._public(task)}

    @staticmethod
    def _public(task: dict[str, Any]) -> dict[str, Any]:
        out = dict(task)
        out["elapsed"] = round((task["finished"] or time.time()) - task["started"], 1)
        return out

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            for key in [k for k, t in self._tasks.items()
                        if t["finished"] and now - t["finished"] > KEEP_SECONDS]:
                del self._tasks[key]
            return [self._public(t) for t in self._tasks.values()]

    def busy(self, key: str) -> bool:
        with self._lock:
            task = self._tasks.get(key)
            return task is not None and task["state"] == "running"
