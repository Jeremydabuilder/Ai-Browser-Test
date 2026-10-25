#!/usr/bin/env python3
"""PyBrowser evaluation harness.

Two layers, kept apart on purpose:

* Deterministic checks live in ``tests/`` and cost nothing (``python tests/run.py``).
* This tool is the OPTIONAL live layer. Nothing here runs, calls a model, or spends money unless
  you pass ``--live`` AND ``--yes`` after reading the plan it prints. It never reads or prints a key:
  the provider is resolved exactly as the browser does (OS keyring or the provider's env var).

Commands
  list [--smoke]                      the tasks
  show TASK_ID                        one task in full, with its success and failure criteria
  run (--smoke | --tasks A,B | --all-fixtures) --live --yes [--provider P --model M]
                                      run FIXTURE tasks headlessly against a real model
  run ... --scripted                  run the same pipeline with a scripted stand-in (harness check; free;
                                      results are labelled scripted and excluded from reports)
  record TASK_ID --system NAME ...    create a result sheet for a run you did by hand: PyBrowser in the
                                      app (all 'lv-*' tasks) or a competitor, under the same conditions
  score RESULT.json --set k=v ...     fill in the manual rubric scores (see RUBRIC.md)
  report [--include-scripted]         a Markdown table of everything in evals/results/

Fixture tasks send only the small local pages in ``evals/fixtures/`` (fictional products; no personal
data). 'lv-*' tasks use real sites and are run by a person in the app, then recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
TASKS_FILE = ROOT / "tasks.json"
FIXTURES = ROOT / "fixtures"
RESULTS = ROOT / "results"
COMPETITORS = RESULTS / "competitors"

RUBRIC = ("requirements", "accuracy", "evidence", "honesty", "usefulness")   # each 0-2, see RUBRIC.md


def load_tasks() -> list[dict]:
    return json.loads(TASKS_FILE.read_text(encoding="utf-8"))["tasks"]


def pick(args, tasks: list[dict]) -> list[dict]:
    if getattr(args, "smoke", False):
        return [t for t in tasks if t["smoke"]]
    if getattr(args, "all_fixtures", False):
        return [t for t in tasks if t["kind"] == "fixture"]
    wanted = [x.strip() for x in (getattr(args, "tasks", "") or "").split(",") if x.strip()]
    by_id = {t["id"]: t for t in tasks}
    unknown = [w for w in wanted if w not in by_id]
    if unknown:
        sys.exit(f"Unknown task id(s): {', '.join(unknown)}")
    return [by_id[w] for w in wanted]


# -- automatic checks (fixture tasks only) ---------------------------------------------------
def auto_check(task: dict, final_text: str) -> dict:
    """Keyword/regex checks against the final answer. They catch invented figures and missing
    caveats cheaply; they are NOT a quality score and a pass proves nothing about correctness."""
    spec = task.get("auto_checks") or {}
    out = {}
    for name in ("must_include", "must_flag_gaps", "must_flag_conflict"):
        for pattern in spec.get(name, []):
            out[f"{name}: {pattern}"] = bool(re.search(pattern, final_text, re.IGNORECASE))
    for pattern in spec.get("must_not_include", []):
        out[f"must_not_include: {pattern}"] = not re.search(pattern, final_text, re.IGNORECASE)
    return out


def load_sources(task: dict):
    from app.team.model import Source, SourceKind
    from app.team.webfetch import extract_text

    sources = []
    for index, rel in enumerate(task["inputs"].get("fixtures", []), start=1):
        html = (FIXTURES / rel).read_text(encoding="utf-8")
        title, text, _ = extract_text(html, 20000)
        readable = len(text.strip()) >= 40 and "sign in" not in title.lower()
        if not readable:
            from app.team.model import SourceStatus
            sources.append(Source(f"S{index}", SourceKind.TAB, title or rel, f"fixture://{rel}", "",
                                  SourceStatus.INACCESSIBLE, "The page needs a sign-in; no readable content."))
            continue
        sources.append(Source(f"S{index}", SourceKind.TAB, title or rel, f"fixture://{rel}", text,
                              retrieved=date.today().isoformat()))
    return sources


def scripted_client():
    sys.path.insert(0, str(REPO))
    from tests.test_team_engine import APPROVE, RESEARCH_WRITE_REVIEW, FakeClient
    return FakeClient({"plan": [RESEARCH_WRITE_REVIEW], "researcher": ["Scripted notes [S1]\n\n## Handoff\nok"],
                       "writer": ["Scripted draft [S1]\n\n## Handoff\nok"], "reviewer": [APPROVE],
                       "final": ["Scripted answer. This is NOT a model result. [S1]"]})


def run_task(task: dict, *, scripted: bool, provider: str, model: str, price_in: float | None,
             price_out: float | None) -> dict:
    sys.path.insert(0, str(REPO))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app.team import sandbox as sandbox_mod
    from app.team.engine import Capabilities, TeamEngine
    from app.team.limits import TeamLimits
    from app.team.llm import make_client_factory, resolve_provider
    from app.team.model import Mission

    limits = TeamLimits()
    if scripted:
        client = scripted_client()
        factory = lambda: client  # noqa: E731
        status_label, status_model = "scripted", "scripted"
    else:
        env = dict(os.environ)
        if provider:
            env["PYBROWSER_TEAM_PROVIDER"] = provider
        if model:
            env["PYBROWSER_TEAM_MODEL"] = model
        status = resolve_provider(None, env)
        if not status.available:
            sys.exit(f"No usable provider: {status.detail}")
        os.environ.update({k: v for k, v in env.items() if k.startswith("PYBROWSER_TEAM_")})
        factory = make_client_factory(status, None, limits)
        status_label, status_model = status.provider, status.model

    mission = Mission(goal=task["prompt"], requirements=list(task.get("requirements", [])),
                      sources=load_sources(task))
    engine = TeamEngine(mission, factory, limits, Capabilities(sandbox_mod.SandboxStatus(False, "not used")))
    started = time.monotonic()
    engine.run()
    seconds = round(time.monotonic() - started, 1)
    final = mission.artifact(mission.final_artifact_id)
    text = final.content if final else ""
    cost = None
    if price_in is not None and price_out is not None:
        cost = round(mission.input_tokens / 1e6 * price_in + mission.output_tokens / 1e6 * price_out, 4)
    return {
        "schema": 1, "task_id": task["id"], "system": "PyBrowser (headless fixture run)",
        "provider": status_label, "model": status_model, "scripted": scripted,
        "app_version": _version(), "run_date": date.today().isoformat(),
        "status": mission.status, "seconds": seconds, "model_calls": mission.model_calls,
        "input_tokens": mission.input_tokens, "output_tokens": mission.output_tokens, "cost_usd": cost,
        "user_interventions": 0,
        "auto_checks": auto_check(task, text),
        "final_answer": text, "scores": {k: None for k in RUBRIC}, "notes": "",
        "limitations_reported": list(mission.limitations),
    }


def _version() -> str:
    sys.path.insert(0, str(REPO))
    from app import __version__
    return __version__


def save(result: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", result["system"].lower()).strip("-")[:30]
    path = folder / f"{result['task_id']}__{slug}__{stamp}.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# -- commands ----------------------------------------------------------------------------------
def cmd_list(args) -> None:
    for t in load_tasks():
        if args.smoke and not t["smoke"]:
            continue
        print(f"{'*' if t['smoke'] else ' '} {t['id']:<28} {t['kind']:<8} {t['category']:<11} {t['title']}")
    print("\n* = smoke subset. 'fixture' tasks run headlessly; 'live' tasks are run by hand in the app.")


def cmd_show(args) -> None:
    task = next((t for t in load_tasks() if t["id"] == args.task), None)
    if task is None:
        sys.exit("Unknown task")
    print(json.dumps(task, indent=2, ensure_ascii=False))


def cmd_run(args) -> None:
    tasks = [t for t in pick(args, load_tasks())]
    live_tasks = [t for t in tasks if t["kind"] != "fixture"]
    if live_tasks:
        print("These need a person in the app (use `record` afterwards): " + ", ".join(t["id"] for t in live_tasks))
    tasks = [t for t in tasks if t["kind"] == "fixture"]
    if not tasks:
        sys.exit("Nothing to run headlessly.")
    from_scripted = args.scripted
    if not from_scripted:
        if not (args.live and args.yes) or os.environ.get("PYBROWSER_EVAL_LIVE") != "1":
            sys.path.insert(0, str(REPO))
            from app.team.limits import TeamLimits
            print(f"PLAN: {len(tasks)} task(s) would call a real model. Each run is capped at "
                  f"{TeamLimits().max_model_calls} model calls (typically 4-8 for these small tasks).\n"
                  "Nothing was run. To run: set PYBROWSER_EVAL_LIVE=1 and pass --live --yes. Your provider's "
                  "usage terms and any charges apply; use a free-tier or local model if you prefer.")
            return
    for task in tasks:
        print(f"-> {task['id']} ...", flush=True)
        result = run_task(task, scripted=from_scripted, provider=args.provider or "", model=args.model or "",
                          price_in=args.price_in, price_out=args.price_out)
        path = save(result, RESULTS)
        passed = sum(result["auto_checks"].values())
        print(f"   {result['status']} in {result['seconds']}s, {result['model_calls']} calls, "
              f"auto-checks {passed}/{len(result['auto_checks'])} -> {path}")
    print("\nAuto-checks are keyword checks, not a quality score. Now score each result with `score`.")


def cmd_record(args) -> None:
    task = next((t for t in load_tasks() if t["id"] == args.task), None)
    if task is None:
        sys.exit("Unknown task")
    result = {
        "schema": 1, "task_id": task["id"], "system": args.system, "provider": args.provider or "",
        "model": args.model or "", "scripted": False, "app_version": args.version or "",
        "run_date": args.date or date.today().isoformat(), "status": args.status or "",
        "seconds": round(args.minutes * 60, 1) if args.minutes is not None else None,
        "model_calls": args.calls, "input_tokens": args.input_tokens, "output_tokens": args.output_tokens,
        "cost_usd": args.cost, "user_interventions": args.interventions, "auto_checks": {},
        "final_answer": "", "scores": {k: None for k in RUBRIC}, "notes": args.notes or "",
        "manual": True,
    }
    folder = COMPETITORS if args.competitor else RESULTS
    print(save(result, folder))


def cmd_score(args) -> None:
    path = Path(args.result)
    data = json.loads(path.read_text(encoding="utf-8"))
    for pair in args.set or []:
        key, _, value = pair.partition("=")
        if key in RUBRIC:
            if value not in ("0", "1", "2"):
                sys.exit(f"{key} must be 0, 1 or 2")
            data["scores"][key] = int(value)
        elif key in ("notes", "status", "user_interventions", "seconds", "cost_usd"):
            data[key] = value if key in ("notes", "status") else float(value)
        else:
            sys.exit(f"Unknown field {key!r}; scores: {', '.join(RUBRIC)}")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print("updated", path)


def cmd_report(args) -> None:
    rows = []
    for folder in (RESULTS, COMPETITORS):
        for path in sorted(folder.glob("*.json")) if folder.exists() else []:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("scripted") and not args.include_scripted:
                continue
            rows.append(data)
    if not rows:
        print("No results yet. Nothing is implied about quality until runs exist.")
        return
    print("| Task | System | Model | Date | Status | Auto | Rubric (0-10) | Time | Calls | Tokens in/out | Cost | Interventions |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for d in sorted(rows, key=lambda r: (r["task_id"], r["system"], r["run_date"])):
        checks = d.get("auto_checks") or {}
        auto = f"{sum(checks.values())}/{len(checks)}" if checks else "-"
        scores = [v for v in d["scores"].values() if v is not None]
        rubric = str(sum(scores)) if len(scores) == len(RUBRIC) else ("unscored" if not scores else "partial")
        secs = f"{d['seconds']:.0f}s" if d.get("seconds") is not None else "-"
        tokens = f"{d.get('input_tokens') or '-'}/{d.get('output_tokens') or '-'}"
        cost = f"${d['cost_usd']}" if d.get("cost_usd") is not None else "-"
        label = d["system"] + (" (scripted)" if d.get("scripted") else "")
        print(f"| {d['task_id']} | {label} | {d.get('model') or '-'} | {d['run_date']} | {d.get('status') or '-'} | "
              f"{auto} | {rubric} | {secs} | {d.get('model_calls') or '-'} | {tokens} | {cost} | "
              f"{d.get('user_interventions', '-')} |")
    print("\nOnly rows with the same task, inputs and conditions are comparable. A single run is an anecdote.")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list"); s.add_argument("--smoke", action="store_true"); s.set_defaults(fn=cmd_list)
    s = sub.add_parser("show"); s.add_argument("task"); s.set_defaults(fn=cmd_show)
    s = sub.add_parser("run")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--smoke", action="store_true"); g.add_argument("--tasks"); g.add_argument("--all-fixtures", action="store_true")
    s.add_argument("--live", action="store_true"); s.add_argument("--yes", action="store_true")
    s.add_argument("--scripted", action="store_true")
    s.add_argument("--provider"); s.add_argument("--model")
    s.add_argument("--price-in", type=float, help="USD per million input tokens (optional, for the cost field)")
    s.add_argument("--price-out", type=float, help="USD per million output tokens (optional)")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("record"); s.add_argument("task"); s.add_argument("--system", required=True)
    for flag, typ in (("--provider", str), ("--model", str), ("--version", str), ("--date", str), ("--status", str),
                      ("--minutes", float), ("--calls", int), ("--input-tokens", int), ("--output-tokens", int),
                      ("--cost", float), ("--interventions", int), ("--notes", str)):
        s.add_argument(flag, type=typ)
    s.add_argument("--competitor", action="store_true"); s.set_defaults(fn=cmd_record)
    s = sub.add_parser("score"); s.add_argument("result"); s.add_argument("--set", nargs="+"); s.set_defaults(fn=cmd_score)
    s = sub.add_parser("report"); s.add_argument("--include-scripted", action="store_true"); s.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
