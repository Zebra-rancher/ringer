#!/usr/bin/env python3
"""Generate deterministic efficiency reports from Ringer attempt logs."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


CHECK_MARKER = "raw_check_output_first_2000_chars:"
BUCKET_NAMES = (
    "retry attempts",
    "first attempts that did not PASS",
    "all attempts of tasks that ended failed (final attempt non-PASS)",
    "TIMEOUT or ERROR attempts",
    'attempts whose check output starts with "[ringer.py] check timed out" (check hit the 60 s cap)',
    'attempts whose check output starts with "[ringer] missing expected files"',
    "attempts with worker_error (setup failures)",
    'attempts with model "" (unattributed model)',
    "attempts on the claude engine",
)
LANES: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = (
    ("claude-haiku", "claude", ("haiku",), None),
    ("claude-sonnet", "claude", ("claude-sonnet-5", "sonnet"), None),
    ("codex-sol-medium", "codex", ("gpt-5.6-sol",), "medium"),
    ("codex-sol-high", "codex", ("gpt-5.6-sol",), "high"),
    ("codex-astra", "codex", ("gpt-6-astra",), None),
    ("gemini-flash", "gemini", ("gemini-3.5-flash",), None),
)


def _tokens(row: dict[str, Any]) -> int:
    value = row.get("worker_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _duration(row: dict[str, Any]) -> int:
    value = row.get("duration_ms")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _check_output(row: dict[str, Any]) -> str:
    notes = row.get("notes")
    if not isinstance(notes, str) or CHECK_MARKER not in notes:
        return ""
    return notes.split(CHECK_MARKER, 1)[1].lstrip("\r\n")


def _has_worker_error(row: dict[str, Any]) -> bool:
    notes = row.get("notes")
    return isinstance(notes, str) and any(
        line.startswith("worker_error=") for line in notes.splitlines()
    )


def load_rows(log_path: Path) -> list[dict[str, Any]]:
    """Load JSON-object rows from a JSONL file; analysis filters mock rows."""
    rows: list[dict[str, Any]] = []
    with log_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"line {line_number} is not a JSON object")
            rows.append(row)
    return rows


def load_specs(runs_dir: Path) -> dict[tuple[str, str], str]:
    """Load full task specs, ignoring malformed or unrelated state files."""
    specs: dict[tuple[str, str], str] = {}
    if not runs_dir.is_dir():
        return specs
    for path in sorted(runs_dir.glob("*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict) or not isinstance(state.get("tasks"), list):
            continue
        run_id = state.get("run_id")
        if not isinstance(run_id, str):
            continue
        for task in state["tasks"]:
            if not isinstance(task, dict):
                continue
            key, spec = task.get("key"), task.get("spec")
            if isinstance(key, str) and isinstance(spec, str):
                specs[(run_id, key)] = spec
    return specs


def _ordered_tasks(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    tasks: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        tasks[(str(row.get("run_id", "")), str(row.get("task_key", "")))].append(row)
    for attempts in tasks.values():
        attempts.sort(key=lambda row: (bool(row.get("retry")), str(row.get("logged_at", ""))))
    return dict(tasks)


def _bucket_rows(
    rows: list[dict[str, Any]], tasks: dict[tuple[str, str], list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    first_failed = [attempts[0] for attempts in tasks.values() if attempts[0].get("verdict") != "PASS"]
    final_failed = [row for attempts in tasks.values() if attempts[-1].get("verdict") != "PASS" for row in attempts]
    return {
        BUCKET_NAMES[0]: [row for row in rows if bool(row.get("retry"))],
        BUCKET_NAMES[1]: first_failed,
        BUCKET_NAMES[2]: final_failed,
        BUCKET_NAMES[3]: [row for row in rows if row.get("verdict") in ("TIMEOUT", "ERROR")],
        BUCKET_NAMES[4]: [row for row in rows if _check_output(row).startswith("[ringer.py] check timed out")],
        BUCKET_NAMES[5]: [row for row in rows if _check_output(row).startswith("[ringer] missing expected files")],
        BUCKET_NAMES[6]: [row for row in rows if _has_worker_error(row)],
        BUCKET_NAMES[7]: [row for row in rows if row.get("model", "") == ""],
        BUCKET_NAMES[8]: [row for row in rows if row.get("worker_engine") == "claude"],
    }


def analyze(rows: list[dict[str, Any]], specs: dict[tuple[str, str], str]) -> dict[str, Any]:
    """Return all numeric report data in a JSON-serializable structure."""
    source_row_count = len(rows)
    rows = [row for row in rows if row.get("worker_engine") != "mock"]
    tasks = _ordered_tasks(rows)
    run_ids = {str(row.get("run_id", "")) for row in rows}
    total_tokens = sum(_tokens(row) for row in rows)
    nonpass = [row for row in rows if row.get("verdict") != "PASS"]
    dates = sorted(str(row.get("logged_at", ""))[:10] for row in rows if row.get("logged_at"))
    totals = {
        "rows": source_row_count, "runs": len(run_ids), "tasks": len(tasks),
        "total_worker_tokens": total_tokens,
        "total_wall_seconds": sum(_duration(row) for row in rows) / 1000,
        "tokens_on_non_pass_attempts": sum(_tokens(row) for row in nonpass),
        "wall_seconds_on_non_pass_attempts": sum(_duration(row) for row in nonpass) / 1000,
    }
    bucket_data = []
    for name, selected in _bucket_rows(rows, tasks).items():
        tokens = sum(_tokens(row) for row in selected)
        bucket_data.append({
            "bucket": name, "attempts": len(selected), "tokens": tokens,
            "share_of_all_tokens": tokens / total_tokens if total_tokens else 0,
        })

    signatures: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in nonpass:
        first_line = _check_output(row).splitlines()[0] if _check_output(row) else "(no check output)"
        signatures[(str(row.get("worker_engine", "")), str(row.get("task_type", "")), first_line[:90])].append(row)
    broken = [{"engine": key[0], "task_type": key[1], "first_line": key[2],
               "attempts": len(group), "tokens": sum(_tokens(row) for row in group)}
              for key, group in signatures.items()]
    broken.sort(key=lambda item: (-item["tokens"], -item["attempts"], item["engine"], item["task_type"], item["first_line"]))

    by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_run[str(row.get("run_id", ""))].append(row)
    run_waste = [{"run": run_id,
                  "wasted_tokens": sum(_tokens(row) for row in group if row.get("verdict") != "PASS" or bool(row.get("retry"))),
                  "run_tokens": sum(_tokens(row) for row in group), "attempts": len(group)}
                 for run_id, group in by_run.items()]
    run_waste.sort(key=lambda item: (-item["wasted_tokens"], item["run"]))

    spec_buckets: dict[str, list[list[dict[str, Any]]]] = {name: [] for name in ("<3k", "3-6k", "6-9k", "9k+")}
    missing_specs = 0
    for key, attempts in tasks.items():
        if attempts[0].get("worker_engine") != "codex":
            continue
        if key not in specs:
            missing_specs += 1
            continue
        length = len(specs[key])
        bucket = "<3k" if length < 3000 else "3-6k" if length < 6000 else "6-9k" if length < 9000 else "9k+"
        spec_buckets[bucket].append(attempts)
    spec_size = []
    for name, groups in spec_buckets.items():
        passed = sum(group[0].get("verdict") == "PASS" for group in groups)
        spec_size.append({"bucket": name, "tasks": len(groups), "first_try_passes": passed,
                          "mean_first_attempt_tokens": (sum(_tokens(group[0]) for group in groups) / len(groups)) if groups else None})

    cells: dict[tuple[str, str, str, str | None], list[list[dict[str, Any]]]] = defaultdict(list)
    for attempts in tasks.values():
        first = attempts[0]
        key = (str(first.get("task_type", "")), str(first.get("worker_engine", "")),
               str(first.get("model", "")), first.get("reasoning_effort"))
        cells[key].append(attempts)
    baseline = []
    for key in sorted(cells, key=lambda value: tuple("" if part is None else part for part in value)):
        groups = cells[key]
        baseline.append({
            "task_type": key[0], "engine": key[1], "model": key[2], "effort": key[3], "tasks": len(groups),
            "first_try_passes": sum(group[0].get("verdict") == "PASS" for group in groups),
            "pass_after_retry": sum(group[-1].get("verdict") == "PASS" for group in groups),
            "median_first_attempt_tokens": statistics.median(_tokens(group[0]) for group in groups),
            "median_first_attempt_seconds": statistics.median(_duration(group[0]) / 1000 for group in groups),
            "median_job_tokens": statistics.median(sum(_tokens(row) for row in group) for group in groups),
            "thin": len(groups) < 3,
        })
    return {"totals": totals, "waste_buckets": bucket_data, "broken_check_signatures": broken[:12],
            "runs_by_wasted_tokens": run_waste[:12], "spec_size_vs_first_try_pass": spec_size,
            "missing_run_json_tasks": missing_specs, "baseline": baseline,
            "date_range": {"first": dates[0] if dates else "-", "last": dates[-1] if dates else "-"}}


def _number(value: float | int) -> str:
    return f"{value:,.1f}" if isinstance(value, float) and not value.is_integer() else f"{value:,.0f}"


def _rate(passes: int, tasks: int) -> str:
    percent = int((100 * passes / tasks) + 0.5) if tasks else 0
    return f"{percent}% ({passes}/{tasks})"


def _generated(log_path: Path, data: dict[str, Any]) -> str:
    totals, dates = data["totals"], data["date_range"]
    return (f"Generated by scripts/efficiency_report.py from {log_path} "
            f"({_number(totals['rows'])} rows, {_number(totals['runs'])} runs, {_number(totals['tasks'])} tasks, "
            f"{dates['first']} to {dates['last']}).")


def render_waste(log_path: Path, data: dict[str, Any]) -> str:
    totals = data["totals"]
    lines = ["# Ringer waste audit", "", _generated(log_path, data), "", "## Totals", "",
             "| Metric | Value |", "|---|---:|"]
    labels = (("Rows", "rows"), ("Runs", "runs"), ("Tasks", "tasks"),
              ("Total worker tokens", "total_worker_tokens"), ("Total wall seconds", "total_wall_seconds"),
              ("Tokens on non-PASS attempts", "tokens_on_non_pass_attempts"),
              ("Wall seconds on non-PASS attempts", "wall_seconds_on_non_pass_attempts"))
    lines.extend(f"| {label} | {_number(totals[key])} |" for label, key in labels)
    lines += ["", "## Waste buckets", "", "| Bucket | Attempts | Tokens | Share of all tokens |", "|---|---:|---:|---:|"]
    for item in data["waste_buckets"]:
        lines.append(f"| {item['bucket']} | {_number(item['attempts'])} | {_number(item['tokens'])} | {item['share_of_all_tokens']:.1%} |")
    lines += ["", "Buckets are not mutually exclusive. The claude engine's `token_regex` captures output_tokens only, so its rows under-count by roughly the input+cache tokens; its numbers are not comparable to codex rows.",
              "", "## Broken-check signatures", "", "| Attempts | Tokens | Engine | task_type | First line |", "|---:|---:|---|---|---|"]
    for item in data["broken_check_signatures"]:
        lines.append(f"| {_number(item['attempts'])} | {_number(item['tokens'])} | {item['engine']} | {item['task_type'] or '(untyped)'} | {item['first_line'].replace('|', '\\|')} |")
    lines += ["", "## Runs by wasted tokens", "", "| Wasted tokens | Run tokens | Attempts | Run |", "|---:|---:|---:|---|"]
    for item in data["runs_by_wasted_tokens"]:
        lines.append(f"| {_number(item['wasted_tokens'])} | {_number(item['run_tokens'])} | {_number(item['attempts'])} | {item['run']} |")
    lines += ["", "## Spec size vs first-try pass", "", "| Bucket | Tasks | First-try pass | Mean first-attempt tokens |", "|---|---:|---:|---:|"]
    for item in data["spec_size_vs_first_try_pass"]:
        mean = "-" if item["mean_first_attempt_tokens"] is None else _number(item["mean_first_attempt_tokens"])
        lines.append(f"| {item['bucket']} | {_number(item['tasks'])} | {_rate(item['first_try_passes'], item['tasks'])} | {mean} |")
    lines += ["", f"Tasks with no run JSON omitted: {_number(data['missing_run_json_tasks'])}.", "", "## Measurement gaps", "",
              "- claude-engine rows log output_tokens only.",
              "- the orchestrator's own tokens are not in this log at all (the T29 ai-usage collector in TRAVISBRAIN holds per-session usage; joining it is future work).",
              "- killed runs leave no row, only the run JSON state.",
              "- spec is truncated to 500 chars in the log, so spec size comes from the run JSONs."]
    return "\n".join(lines) + "\n"


def _cell_text(item: dict[str, Any]) -> str:
    return (f"{item['task_type'] or '(untyped)'} | {item['engine']} | {item['model'] or '(blank)'} | "
            f"{item['effort'] or '-'} | {_number(item['tasks'])} | {_rate(item['first_try_passes'], item['tasks'])} | "
            f"{_rate(item['pass_after_retry'], item['tasks'])} | {_number(item['median_first_attempt_tokens'])} | "
            f"{_number(item['median_first_attempt_seconds'])} | {_number(item['median_job_tokens'])} | "
            f"{'THIN' if item['thin'] else ''}")


def render_baseline(log_path: Path, data: dict[str, Any]) -> str:
    header = "Task type | Engine | Model | Effort | Tasks | First-try pass | Pass after retry | Median first-attempt tokens | Median first-attempt seconds | Median job tokens (sum of all attempts of the task) | Thin"
    lines = ["# Ringer baseline: first-try pass by model, effort and task_type", "", _generated(log_path, data), "", f"| {header} |",
             "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|"]
    lines.extend(f"| {_cell_text(item)} |" for item in data["baseline"])
    lines += ["", "## Lanes", ""]
    for lane, engine, models, effort in LANES:
        matches = [item for item in data["baseline"] if item["engine"] == engine and item["model"] in models and (effort is None or item["effort"] == effort)]
        lines.append(f"**{lane}:**")
        if matches:
            lines += ["", f"| {header} |", "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|"]
            lines.extend(f"| {_cell_text(item)} |" for item in matches)
        else:
            lines[-1] += " no data"
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_reports(log_path: Path, runs_dir: Path, out_dir: Path) -> dict[str, Any]:
    rows = load_rows(log_path)
    data = analyze(rows, load_specs(runs_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "waste-audit.md").write_text(render_waste(log_path, data), encoding="utf-8")
    (out_dir / "baseline.md").write_text(render_baseline(log_path, data), encoding="utf-8")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=Path("~/.ringer/runs.jsonl"))
    parser.add_argument("--runs-dir", type=Path, default=Path("~/.ringer/runs"))
    parser.add_argument("--out", type=Path, default=Path("docs/efficiency"))
    parser.add_argument("--json", action="store_true", help="print report data as JSON instead of writing markdown")
    args = parser.parse_args(argv)
    log_path, runs_dir = args.log.expanduser(), args.runs_dir.expanduser()
    if not log_path.is_file():
        print(f"error: log not found: {log_path}", file=sys.stderr)
        return 2
    try:
        rows = load_rows(log_path)
        data = analyze(rows, load_specs(runs_dir))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(data, sort_keys=True))
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    paths = (args.out / "waste-audit.md", args.out / "baseline.md")
    paths[0].write_text(render_waste(log_path, data), encoding="utf-8")
    paths[1].write_text(render_baseline(log_path, data), encoding="utf-8")
    print(paths[0])
    print(paths[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
