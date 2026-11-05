"""How far the distributed live-stacking work has got, read from its task list.

The work spans two repositories (this one and Rapid Stacker Collab) and runs
over many sessions, so "where are we" is easy to lose. The answer is already
written down: `TASKS.DISTRIBUTED_STACKING.md` ticks a box in the commit that
lands each task. This script only reads that file and the two repositories'
git history, so it cannot drift from what was actually committed, and it never
changes anything unless asked to run a task's own Verify command.

    python3 tools/task_progress.py                 # the summary
    python3 tools/task_progress.py --phase 1       # every task of one phase
    python3 tools/task_progress.py --pending       # every task not done yet
    python3 tools/task_progress.py --verify T104   # run T104's Verify command (repeatable)
    python3 tools/task_progress.py --verify-done   # re-run the Verify of every ticked task
    python3 tools/task_progress.py --json          # the same facts for another program
    python3 tools/task_progress.py --export DIR    # tasks.json + activity.json for the progress web page

`--verify-done` is how to check that a ticked box is still true: a task whose
command now fails is reported, and the script exits non-zero. Some commands
build Rust or .NET and take minutes.

Rapid Stacker Collab is looked for beside this repository in the author's
layout (`../Rust/rapid-stacker-collab`); `--rsc PATH` or `RSC_DIR` points
elsewhere. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TASKS = HERE / "TASKS.DISTRIBUTED_STACKING.md"

PHASE_RE = re.compile(r"^## Phase (\w+) — (.+)$")
TASK_RE = re.compile(r"^- \[( |x)\] ([DT]\d{3}) — (.+)$")
FIELD_RE = re.compile(r"^\s+\*\*(Files|Do|Done when|Verify|Manual):\*\*\s*(.*)$")
BEFORE_RE = re.compile(r"before ((?:T\d{3})(?:(?:,| and) T\d{3})*)")


@dataclass
class Task:
    id: str
    title: str
    done: bool
    phase: str
    repo: str = ""            # "RSC", "SF" or "" (a manual step or a decision)
    verify: str = ""
    manual: str = ""
    before: list = field(default_factory=list)   # decisions: the tasks they must precede


@dataclass
class Phase:
    key: str
    title: str
    tasks: list = field(default_factory=list)


def parse(text: str) -> list[Phase]:
    phases: list[Phase] = []
    task: Task | None = None
    current = None   # (field name, list of lines)

    def close_field():
        nonlocal current
        if task and current:
            name, lines = current
            value = " ".join(s.strip() for s in lines).strip()
            if name == "Files":
                m = re.match(r"(RSC|SF)\b", value)
                task.repo = m.group(1) if m else ""
            elif name == "Verify":
                m = re.search(r"`(.+)`", value)
                task.verify = m.group(1) if m else ""
            elif name == "Manual":
                task.manual = value
                if task.id.startswith("D"):
                    m = BEFORE_RE.search(value)
                    if m:
                        task.before = re.findall(r"T\d{3}", m.group(1))
        current = None

    for line in text.splitlines():
        m = PHASE_RE.match(line)
        if m:
            close_field()
            task = None
            phases.append(Phase(m.group(1), m.group(2)))
            continue
        m = TASK_RE.match(line)
        if m and phases:
            close_field()
            task = Task(m.group(2), m.group(3), m.group(1) == "x", phases[-1].key)
            phases[-1].tasks.append(task)
            continue
        m = FIELD_RE.match(line)
        if m and task:
            close_field()
            current = (m.group(1), [m.group(2)])
            continue
        if current and line.startswith("      ") and line.strip():
            current[1].append(line)
        elif not line.strip():
            close_field()
    close_field()
    return phases


# --- git ---------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def activity(sf: Path, rsc: Path) -> dict:
    rel = TASKS.relative_to(sf).as_posix()
    info = {
        "starfront": {
            "branch": git(sf, "branch", "--show-current"),
            "last_tick": git(sf, "log", "-1", "--format=%h %cs %s", "--", rel),
            "task_list_uncommitted": bool(git(sf, "status", "--porcelain", "--", rel)),
            "unpushed": git(sf, "rev-list", "--count", "@{u}..HEAD") or "?",
        },
        "rapid_stacker_collab": {"present": rsc.is_dir()},
    }
    if rsc.is_dir():
        branches = []
        refs = git(rsc, "for-each-ref", "--sort=-committerdate", "--count=4",
                   "--format=%(refname:short)|%(committerdate:short)|%(subject)", "refs/heads")
        for row in refs.splitlines():
            name, date, subject = (row.split("|", 2) + ["", ""])[:3]
            ahead = git(rsc, "rev-list", "--count", f"master..{name}") if name != "master" else "0"
            branches.append({"branch": name, "date": date, "subject": subject, "ahead_of_master": ahead})
        info["rapid_stacker_collab"].update({
            "current": git(rsc, "branch", "--show-current"),
            "dirty": bool(git(rsc, "status", "--porcelain")),
            "recent_branches": branches,
            "hub_crate": (rsc / "worker" / "crates" / "hub").is_dir(),
        })
    return info


# --- the report --------------------------------------------------------------

def colour(enabled: bool):
    def paint(code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if enabled else s
    return paint


def bar(done: int, total: int, width: int = 20) -> str:
    filled = round(width * done / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def next_task(phases: list[Phase]) -> Task | None:
    for p in phases:
        if p.key == "D":
            continue
        for t in p.tasks:
            if not t.done:
                return t
    return None


def order_index(phases: list[Phase]) -> dict[str, int]:
    ids = [t.id for p in phases if p.key != "D" for t in p.tasks]
    return {tid: i for i, tid in enumerate(ids)}


def summary(phases, act, paint) -> str:
    all_tasks = [t for p in phases for t in p.tasks]
    work = [t for t in all_tasks if t.id.startswith("T")]
    done = sum(t.done for t in work)
    out = [paint("1", f"Distributed live stacking: {done} of {len(work)} tasks done "
                      f"({round(100 * done / len(work)) if work else 0}%)"), ""]
    for p in phases:
        d = sum(t.done for t in p.tasks)
        n = len(p.tasks)
        state = paint("32", "done") if d == n else (paint("33", "in progress") if d else "")
        label = "decisions" if p.key == "D" else p.title
        out.append(f"  {p.key:>2}  {d:>2}/{n:<2}  {bar(d, n)}  {label}  {state}")

    nxt = next_task(phases)
    idx = order_index(phases)
    out.append("")
    if nxt:
        where = {"RSC": "rapid-stacker-collab", "SF": "starfront"}.get(nxt.repo, "manual step")
        out.append(paint("1", "Next: ") + f"{nxt.id} — {nxt.title}  [{where}]")
    else:
        out.append(paint("32", "Every task is done."))

    open_d = [t for t in phases[0].tasks if t.id.startswith("D") and not t.done] if phases else []
    if open_d:
        out.append("")
        out.append(paint("1", "Waiting on your decisions:"))
        for d in open_d:
            due = min(d.before, key=lambda x: idx.get(x, 10**6)) if d.before else ""
            now = nxt and due and idx.get(due, 10**6) <= idx.get(nxt.id, 0)
            tag = paint("31", "  BLOCKING NOW") if now else ""
            out.append(f"  {d.id} — {d.title}" + (f"  (before {due})" if due else "") + tag)

    manual = [t for t in work if t.manual and not t.done and nxt and idx.get(t.id, 0) < idx.get(nxt.id, 0)]
    if manual:
        out.append("")
        out.append(paint("1", "Manual checks still open behind the work:"))
        for t in manual:
            out.append(f"  {t.id} — {t.title}")

    sf, rsc = act["starfront"], act["rapid_stacker_collab"]
    out += ["", paint("1", "Activity:")]
    out.append(f"  starfront ({sf['branch'] or '?'}): last task-list commit {sf['last_tick'] or 'none'}")
    if sf["task_list_uncommitted"]:
        out.append(paint("33", "    the task list has uncommitted changes"))
    if sf["unpushed"] not in ("0", "?"):
        out.append(paint("33", f"    {sf['unpushed']} commit(s) not pushed"))
    if not rsc["present"]:
        out.append("  rapid-stacker-collab: not found (use --rsc PATH)")
    else:
        out.append(f"  rapid-stacker-collab (on {rsc['current'] or '?'}"
                   f"{', uncommitted changes' if rsc['dirty'] else ''}"
                   f"{'' if rsc['hub_crate'] else ', no hub crate yet'}):")
        for b in rsc["recent_branches"]:
            ahead = f", {b['ahead_of_master']} ahead of master" if b["ahead_of_master"] not in ("0", "") else ""
            out.append(f"    {b['branch']:<34} {b['date']}{ahead}  {b['subject'][:60]}")
    return "\n".join(out)


def listing(tasks, paint) -> str:
    rows = []
    for t in tasks:
        box = paint("32", "[x]") if t.done else "[ ]"
        kind = "manual" if t.manual and not t.verify else (t.repo or "")
        rows.append(f"  {box} {t.id}  {kind:<6}  {t.title}")
    return "\n".join(rows)


# --- running Verify commands -------------------------------------------------

def run_verify(task: Task, sf: Path, rsc: Path, paint) -> bool:
    if not task.verify:
        print(f"{task.id}: no Verify command ({'a manual step' if task.manual else 'nothing to run'})")
        return True
    root = rsc if task.repo == "RSC" else sf
    if not root.is_dir():
        print(paint("31", f"{task.id}: {root} not found"))
        return False
    print(paint("1", f"{task.id} — {task.title}"))
    print(f"  in {root}\n  $ {task.verify}")
    ok = subprocess.run(task.verify, shell=True, cwd=root).returncode == 0
    print(paint("32", "  PASS") if ok else paint("31", "  FAIL"))
    return ok


# --- rows for the progress web page ------------------------------------------

REPO_NAMES = {"RSC": "Rapid Stacker Collab", "SF": "Starfront"}


def plain(text: str) -> str:
    return text.replace("`", "")


def export(phases: list[Phase], act: dict, out: Path) -> None:
    """Two row files the published progress page loads: one row per task, one per recent branch."""
    rows, seq = [], 0
    for p in phases:
        phase_title = "Your decisions" if p.key == "D" else re.sub(r"\s*\(.*\)\s*$", "", p.title)
        phase_title = phase_title[:1].upper() + phase_title[1:]
        for t in p.tasks:
            seq += 1
            kind = "decision" if t.id.startswith("D") else ("manual" if t.manual and not t.verify else "work")
            rows.append({"id": t.id, "title": plain(t.title), "phase": p.key, "phase_title": phase_title,
                         "kind": kind, "repo": REPO_NAMES.get(t.repo, ""),
                         "owner": "You" if kind != "work" else REPO_NAMES.get(t.repo, ""), "status": "done" if t.done else "pending",
                         "done": 1 if t.done else 0, "before": ", ".join(t.before), "seq": seq})
    sf, rsc = act["starfront"], act["rapid_stacker_collab"]
    head = git(HERE, "log", "-1", "--format=%cs|%s").split("|", 1) + ["", ""]
    notes = []
    if sf["task_list_uncommitted"]:
        notes.append("task list has uncommitted changes")
    if sf["unpushed"] not in ("0", "?", ""):
        notes.append(f"{sf['unpushed']} commit(s) not pushed")
    activity_rows = [{"repo": "Starfront", "branch": sf["branch"] or "?", "date": head[0], "subject": head[1],
                      "ahead": "", "note": "; ".join(notes)}]
    for b in rsc.get("recent_branches", []):
        note = "checked out" if b["branch"] == rsc.get("current") else ""
        if b["branch"] == rsc.get("current") and rsc.get("dirty"):
            note += ", uncommitted changes"
        activity_rows.append({"repo": "Rapid Stacker Collab", "branch": b["branch"], "date": b["date"],
                              "subject": b["subject"], "ahead": b["ahead_of_master"] if b["branch"] != "master" else "",
                              "note": note})
    out.mkdir(parents=True, exist_ok=True)
    (out / "tasks.json").write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    (out / "activity.json").write_text(json.dumps(activity_rows, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out / 'tasks.json'} ({len(rows)} rows) and {out / 'activity.json'} ({len(activity_rows)} rows)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phase", help="list every task of one phase (D, 0-7)")
    ap.add_argument("--pending", action="store_true", help="list every task not done yet")
    ap.add_argument("--verify", action="append", metavar="ID", help="run a task's Verify command")
    ap.add_argument("--verify-done", action="store_true", help="re-run the Verify of every ticked task")
    ap.add_argument("--json", action="store_true", help="print the facts as JSON")
    ap.add_argument("--export", metavar="DIR", help="write tasks.json and activity.json for the progress page")
    ap.add_argument("--rsc", default=os.environ.get("RSC_DIR", str(HERE.parent / "Rust" / "rapid-stacker-collab")))
    ap.add_argument("--no-colour", action="store_true")
    ap.add_argument("--file", default=str(TASKS), help=argparse.SUPPRESS)   # a copy, for testing
    args = ap.parse_args()
    tasks_file = Path(args.file)

    if not tasks_file.is_file():
        print(f"{tasks_file} not found", file=sys.stderr)
        return 2
    phases = parse(tasks_file.read_text(encoding="utf-8"))
    by_id = {t.id: t for p in phases for t in p.tasks}
    sf, rsc = HERE, Path(args.rsc).expanduser().resolve()
    paint = colour(sys.stdout.isatty() and not args.no_colour and "NO_COLOR" not in os.environ)

    if args.export:
        export(phases, activity(sf, rsc), Path(args.export).expanduser())
        return 0

    if args.json:
        print(json.dumps({"phases": [{"key": p.key, "title": p.title, "tasks": [asdict(t) for t in p.tasks]}
                                     for p in phases],
                          "next": (next_task(phases).id if next_task(phases) else None),
                          "activity": activity(sf, rsc)}, indent=2))
        return 0

    if args.verify or args.verify_done:
        chosen = [by_id[i.upper()] for i in (args.verify or []) if i.upper() in by_id]
        unknown = [i for i in (args.verify or []) if i.upper() not in by_id]
        if args.verify_done:
            chosen += [t for t in by_id.values() if t.done and t.verify and t not in chosen]
        for i in unknown:
            print(paint("31", f"{i}: no such task"))
        failed = [t.id for t in chosen if not run_verify(t, sf, rsc, paint)]
        print()
        print(paint("31", f"failed: {', '.join(failed)}") if failed
              else paint("32", f"{len(chosen)} task(s) verified"))
        return 1 if failed or unknown else 0

    if args.phase:
        p = next((p for p in phases if p.key.upper() == args.phase.upper()), None)
        if not p:
            print(f"no phase {args.phase}", file=sys.stderr)
            return 2
        print(paint("1", f"Phase {p.key} — {p.title}"))
        print(listing(p.tasks, paint))
        return 0

    if args.pending:
        for p in phases:
            todo = [t for t in p.tasks if not t.done]
            if todo:
                print(paint("1", f"Phase {p.key} — {p.title}"))
                print(listing(todo, paint))
        return 0

    print(summary(phases, activity(sf, rsc), paint))
    return 0


if __name__ == "__main__":
    sys.exit(main())
