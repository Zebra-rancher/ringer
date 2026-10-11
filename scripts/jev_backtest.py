#!/usr/bin/env python3
"""Counterfactual backtest of Jev lane picks against historical Ringer runs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
os.environ["RINGER_NO_SELF_UPDATE"] = "1"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ringer = _load_module("ringer_jev_backtest", ROOT / "ringer.py")



def _state_digest(state: dict, lanes) -> str:
    """Cache key: the state AND the question (lane set + criteria). Editing lanes.toml invalidates answers."""
    question = ringer.jev_lane_question(lanes)
    return hashlib.sha256(json.dumps({"state": state, "questions": question}, sort_keys=True).encode()).hexdigest()

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except (TypeError, ValueError):
            continue
    return rows


def _tokens(row: dict[str, Any]) -> int:
    value = row.get("worker_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _effort(engine_args: tuple[str, ...]) -> str | None:
    prefix = "model_reasoning_effort="
    for arg in engine_args:
        if arg.startswith(prefix):
            return arg[len(prefix):] or None
    return None


def actual_lane(row: dict[str, Any], lanes: dict[str, Any]) -> str:
    engine = str(row.get("worker_engine") or "")
    model = str(row.get("model") or "")
    effort = row.get("reasoning_effort") or None
    for name, lane in lanes.items():
        model_matches = model == lane.model
        if lane.model == "sonnet" and model in {"sonnet", "claude-sonnet-5", "claude-sonnet-5-5"}:
            model_matches = True
        if lane.engine == engine and model_matches and _effort(lane.engine_args) == effort:
            return name
    return f"other:{engine}/{model or '(blank)'}/{effort or '-'}"


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    cached = {}
    for row in read_jsonl(path):
        digest = row.get("state_sha256", row.get("sha256"))
        answers = row.get("answers")
        if isinstance(digest, str) and isinstance(answers, dict):
            cached[digest] = answers
    return cached


def _append_cache(path: Path, digest: str, answers: dict[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"state_sha256": digest, "answers": answers}, sort_keys=True) + "\n")


def _load_client(path: Path):
    module = _load_module("jev_backtest_client", path.expanduser())
    return module.ask


def _answer_pick(answers: Any, lanes: dict[str, Any]) -> tuple[str, float] | None:
    try:
        answer = answers["lane"]
        choice = answer["choice"]
        if choice not in lanes:
            return None
        return choice, float(answer.get("confidence", 0.0))
    except (KeyError, TypeError, ValueError):
        return None


def _run_specs(runs_dir: Path, run_id: str) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads((runs_dir.expanduser() / f"{run_id}.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    tasks = raw.get("tasks", []) if isinstance(raw, dict) else []
    return {str(task.get("key")): task for task in tasks if isinstance(task, dict) and task.get("key") is not None}


def collect_tasks(log_path: Path, runs_dir: Path, lanes: dict[str, Any], since: str | None, limit: int | None) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(log_path):
        if row.get("worker_engine") == "mock":
            continue
        run_id, key = row.get("run_id"), row.get("task_key")
        if isinstance(run_id, str) and isinstance(key, str):
            grouped[(run_id, key)].append(row)

    tasks = []
    specs_by_run: dict[str, dict[str, dict[str, Any]]] = {}
    for (run_id, key), attempts in grouped.items():
        attempts.sort(key=lambda row: (
            row.get("retry") if isinstance(row.get("retry"), int) else 0,
            str(row.get("logged_at", "")),
        ))
        first = attempts[0]
        logged_at = str(first.get("logged_at", ""))
        if since and logged_at[:10] < since:
            continue
        if run_id not in specs_by_run:
            specs_by_run[run_id] = _run_specs(runs_dir, run_id)
        tasks.append({
            "run_id": run_id,
            "key": key,
            "logged_at": logged_at,
            "task_type": str(first.get("task_type") or "(untyped)"),
            "first_verdict": str(first.get("verdict") or ""),
            "first_tokens": _tokens(first),
            "job_tokens": sum(_tokens(row) for row in attempts),
            "actual_lane": actual_lane(first, lanes),
            "spec_obj": specs_by_run[run_id].get(key),
        })
    tasks.sort(key=lambda task: (task["logged_at"], task["run_id"], task["key"]))
    return tasks[:limit] if limit is not None else tasks


def _baselines(tasks: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, float]]:
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        cells[(task["actual_lane"], task["task_type"])].append(task)
    return {
        key: {
            "tasks": len(group),
            "first_try_rate": sum(item["first_verdict"] == "PASS" for item in group) / len(group),
            "median_first_tokens": statistics.median(item["first_tokens"] for item in group),
        }
        for key, group in cells.items() if len(group) >= 3
    }


def backtest(log_path: Path, runs_dir: Path, lanes_path: Path, client_path: Path, cache_path: Path,
             *, since: str | None = None, limit: int | None = None) -> dict[str, Any]:
    lanes = ringer.load_lanes(lanes_path.expanduser())
    if not lanes:
        raise ValueError(f"no lanes loaded from {lanes_path.expanduser()}")
    tasks = collect_tasks(log_path.expanduser(), runs_dir.expanduser(), lanes, since, limit)
    baselines = _baselines(tasks)
    cache = _load_cache(cache_path)
    client = None
    for task in tasks:
        raw = task["spec_obj"]
        if not isinstance(raw, dict) or not isinstance(raw.get("spec"), str):
            continue
        task["has_spec"] = True
        task_spec = ringer.TaskSpec(
            key=task["key"], spec=raw["spec"], check=str(raw.get("check") or ""),
            engine=str(raw.get("engine") or ""), task_type=task["task_type"],
        )
        state = ringer.jev_lane_state(task_spec, ringer.jev_check_text(task_spec, None))
        digest = _state_digest(state, lanes)
        answers = cache.get(digest)
        if answers is None:
            if client is None:
                client = _load_client(client_path)
            try:
                answers = client("ringer-spec", state, ringer.jev_lane_question(lanes), caller="jev-backtest")
            except Exception:
                answers = None
            if isinstance(answers, dict):
                cache[digest] = answers
                _append_cache(cache_path, digest, answers)
        picked = _answer_pick(answers, lanes)
        if picked is None:
            task["pick"] = None
            continue
        pick, confidence = picked
        task["pick"] = pick
        task["confidence"] = confidence
        task["agreement"] = pick == task["actual_lane"]
        if task["agreement"]:
            task["estimated_first_try"] = float(task["first_verdict"] == "PASS")
            task["estimated_tokens"] = task["first_tokens"]
            task["token_delta"] = 0
        elif (pick, task["task_type"]) in baselines:
            cell = baselines[(pick, task["task_type"])]
            task["estimated_first_try"] = cell["first_try_rate"]
            task["estimated_tokens"] = cell["median_first_tokens"]
            task["token_delta"] = cell["median_first_tokens"] - task["job_tokens"]
        else:
            task["estimated_first_try"] = None
            task["estimated_tokens"] = None
            task["token_delta"] = None

    answered = sum(task.get("pick") is not None for task in tasks)
    with_spec = sum(bool(task.get("has_spec")) for task in tasks)
    data = {
        "counts": {
            "tasks_seen": len(tasks), "with_spec": with_spec, "asked": with_spec,
            "answered": answered, "no_pick": with_spec - answered,
            "cached": sum(1 for task in tasks if task.get("has_spec") and _state_is_cached(task, cache, lanes)),
        },
        "by_task_type": _summaries(tasks),
        "confusion": _confusion(tasks),
        "picks_by_lane": _picks_by_lane(tasks),
        "disagreements": _disagreements(tasks),
    }
    data["total"] = _summary(tasks)
    return data


def _state_is_cached(task: dict[str, Any], cache: dict[str, Any], lanes) -> bool:
    raw = task.get("spec_obj")
    if not isinstance(raw, dict):
        return False
    spec = ringer.TaskSpec(key=task["key"], spec=raw["spec"], check=str(raw.get("check") or ""),
                           engine=str(raw.get("engine") or ""), task_type=task["task_type"])
    state = ringer.jev_lane_state(spec, ringer.jev_check_text(spec, None))
    return _state_digest(state, lanes) in cache


def _summary(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    picks = [task for task in tasks if task.get("pick") is not None]
    known = [task for task in picks if task.get("estimated_first_try") is not None]
    deltas = [task["token_delta"] for task in known]
    return {
        "tasks": len(tasks), "picks": len(picks),
        "agreements": sum(bool(task.get("agreement")) for task in picks),
        "agreement_rate": (sum(bool(task.get("agreement")) for task in picks) / len(picks)) if picks else None,
        "actual_first_try_rate": (sum(task["first_verdict"] == "PASS" for task in tasks) / len(tasks)) if tasks else None,
        "estimated_first_try_rate": (sum(task["estimated_first_try"] for task in known) / len(known)) if known else None,
        "known": len(known), "unknown_thin": len(picks) - len(known),
        "token_delta_sum": sum(deltas), "median_delta": statistics.median(deltas) if deltas else None,
    }


def _summaries(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = sorted({task["task_type"] for task in tasks})
    return [{"task_type": name, **_summary([task for task in tasks if task["task_type"] == name])} for name in names]


def _confusion(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter((task["actual_lane"], task["pick"]) for task in tasks if task.get("pick") is not None)
    return [{"actual_lane": actual, "jev_pick": pick, "tasks": count}
            for (actual, pick), count in sorted(counts.items())]


def _picks_by_lane(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for task in tasks:
        if task.get("pick") is not None:
            grouped[task["pick"]].append(task["confidence"])
    return [{"lane": lane, "picks": len(values), "mean_confidence": statistics.mean(values)}
            for lane, values in sorted(grouped.items())]


def _disagreements(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: task.get(key) for key in (
        "run_id", "key", "task_type", "actual_lane", "pick", "confidence",
        "first_verdict", "job_tokens", "estimated_first_try", "estimated_tokens", "token_delta",
    )} for task in tasks if task.get("pick") is not None and not task.get("agreement")][:25]


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _number(value: float | int | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    rounded = round(value)
    text = f"{rounded:,}" if abs(value - rounded) < 1e-9 else f"{value:,.1f}"
    return f"+{text}" if signed and value > 0 else text


def render_markdown(data: dict[str, Any]) -> str:
    c = data["counts"]
    lines = [
        "# Jev lane-pick backtest", "",
        "Generated by scripts/jev_backtest.py — "
        f"tasks seen: {c['tasks_seen']}; with spec: {c['with_spec']}; asked: {c['asked']}; "
        f"answered: {c['answered']}; no pick: {c['no_pick']}; cached: {c['cached']}.", "",
        "## Caveats", "",
        "- This is a counterfactual estimate from per-cell averages, not a measurement of work Jev actually ran.",
        "- Cells with fewer than three historical tasks are thin and excluded from estimated rates and token sums.",
        "- Claude rows under-count tokens.",
        "- Agreement with the actual lane is not the same as being right; the actual lane was the orchestrator's guess.", "",
        "## By task_type", "",
        "| Task type | Tasks | Picks | Agreement with actual | Actual first-try | Estimated first-try under Jev | Known | Unknown (thin) | Token delta (sum) | Median delta |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in [*data["by_task_type"], {"task_type": "TOTAL", **data["total"]}]:
        lines.append(
            f"| {row['task_type']} | {row['tasks']} | {row['picks']} | {_rate(row['agreement_rate'])} | "
            f"{_rate(row['actual_first_try_rate'])} | {_rate(row['estimated_first_try_rate'])} | "
            f"{row['known']} | {row['unknown_thin']} | {_number(row['token_delta_sum'], signed=True)} | "
            f"{_number(row['median_delta'], signed=True)} |"
        )
    lines += ["", "## Confusion (actual lane x Jev pick)", "",
              "| Actual lane | Jev pick | Tasks |", "|---|---|---:|"]
    lines += [f"| {row['actual_lane']} | {row['jev_pick']} | {row['tasks']} |" for row in data["confusion"]]
    lines += ["", "## Picks by lane", "", "| Lane | Picks | Mean confidence |", "|---|---:|---:|"]
    lines += [f"| {row['lane']} | {row['picks']} | {row['mean_confidence']:.2f} |" for row in data["picks_by_lane"]]
    lines += ["", "## Disagreements", "", "| Run ID | Key | Task type | Actual lane | Pick | Confidence | Real verdict | Real job tokens | Estimate |",
              "|---|---|---|---|---|---:|---|---:|---|"]
    for row in data["disagreements"]:
        estimate = "unknown (thin)" if row["estimated_first_try"] is None else (
            f"{_rate(row['estimated_first_try'])} first-try, {_number(row['estimated_tokens'])} tokens, "
            f"{_number(row['token_delta'], signed=True)} delta"
        )
        lines.append(f"| {row['run_id']} | {row['key']} | {row['task_type']} | {row['actual_lane']} | "
                     f"{row['pick']} | {row['confidence']:.2f} | {row['first_verdict']} | "
                     f"{_number(row['job_tokens'])} | {estimate} |")
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=Path("~/.ringer/runs.jsonl"))
    parser.add_argument("--runs-dir", type=Path, default=Path("~/.ringer/runs"))
    parser.add_argument("--lanes", type=Path, default=ROOT / "registry" / "lanes.toml")
    parser.add_argument("--client", type=Path, default=Path("~/.claude/scripts/jev_call.py"))
    parser.add_argument("--cache", type=Path, default=Path("~/.ringer/jev-backtest-cache.jsonl"))
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "efficiency" / "jev-backtest.md")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--since")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.since:
        try:
            date.fromisoformat(args.since)
        except ValueError:
            parser.error("--since must be YYYY-MM-DD")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data = backtest(args.log, args.runs_dir, args.lanes, args.client, args.cache,
                    since=args.since, limit=args.limit)
    markdown = render_markdown(data)
    args.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
    args.out.expanduser().write_text(markdown, encoding="utf-8")
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
