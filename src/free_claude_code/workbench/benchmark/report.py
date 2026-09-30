"""Aggregate scenario results into report.json + report.md; decide the exit code.

Hard invariant: zero false completions (platform VERIFIED, oracle rejects). With the
deterministic fake engine every disposition must also match the scenario's expectation.
"""

import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path

from free_claude_code.core.json_types import JsonObject, JsonValue

from .harness import ScenarioResult


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile; None for no values."""

    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def summarize(results: list[ScenarioResult], meta: JsonObject) -> JsonObject:
    times = [
        t
        for r in results
        if (t := r.time_to_verified_s) is not None and r.oracle.passed
    ]
    false_completions: list[JsonValue] = [
        r.scenario for r in results if r.false_completion
    ]
    mismatched: list[JsonValue] = [
        r.scenario for r in results if r.disposition != r.expected
    ]
    verified = [r for r in results if r.disposition == "VERIFIED" and r.oracle.passed]
    tokens = sum(r.input_tokens + r.output_tokens for r in results)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        **meta,
        "aggregate": {
            "scenarios": len(results),
            "verified": sum(r.disposition == "VERIFIED" for r in results),
            "oracle_passed": sum(r.oracle.passed for r in results),
            "expected_dispositions": len(results) - len(mismatched),
            "false_completions": false_completions,
            "unexpected_dispositions": mismatched,
            "median_time_to_verified_s": statistics.median(times) if times else None,
            "p95_time_to_verified_s": percentile(times, 0.95),
            "total_elapsed_s": round(sum(r.elapsed_s for r in results), 2),
            "tokens_per_verified_success": (
                round(tokens / len(verified)) if verified else None
            ),
        },
        "runs": [r.to_json() for r in results],
    }


def exit_code(report: JsonObject) -> int:
    aggregate = report.get("aggregate")
    if not isinstance(aggregate, dict):
        return 2
    if aggregate.get("false_completions"):
        return 1
    if report.get("engine") == "fake" and aggregate.get("unexpected_dispositions"):
        return 1
    return 0


def _cell(value: JsonValue) -> str:
    if value is None:
        return "n/a"
    return str(value).replace("|", "/").replace("\n", " ")


def render_markdown(report: JsonObject) -> str:
    aggregate = report.get("aggregate")
    agg = aggregate if isinstance(aggregate, dict) else {}
    runs = report.get("runs")
    lines = [
        "# fcc-bench report",
        "",
        f"- Generated: {report.get('generated_at')}",
        f"- Engine: {report.get('engine')}"
        + (f" via {report.get('proxy')}" if report.get("proxy") else ""),
        f"- Per-scenario timeout: {report.get('timeout_s')} s",
        "",
        "## Aggregate",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    lines += [
        f"| {key.replace('_', ' ')} | {_cell(value)} |" for key, value in agg.items()
    ]
    lines += [
        "",
        "## Runs",
        "",
        "| scenario | mode | expected | disposition | oracle | false completion "
        "| elapsed s | attempts | recoveries | turns | tokens in/out | failovers "
        "| constraints kept | notes |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        oracle = run.get("oracle")
        passed = oracle.get("passed") if isinstance(oracle, dict) else None
        reasons = run.get("blocking_reasons")
        notes = run.get("error") or "; ".join(
            str(r) for r in (reasons if isinstance(reasons, list) else [])
        )
        lines.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    run.get("scenario"),
                    run.get("mode"),
                    run.get("expected"),
                    run.get("disposition"),
                    "pass" if passed else "FAIL",
                    "YES" if run.get("false_completion") else "no",
                    run.get("elapsed_s"),
                    run.get("attempts"),
                    run.get("recoveries"),
                    run.get("turns"),
                    f"{run.get('input_tokens')}/{run.get('output_tokens')}",
                    run.get("failovers"),
                    run.get("constraints_kept"),
                    str(notes)[:200],
                )
            )
            + " |"
        )
    false = agg.get("false_completions")
    lines += [
        "",
        "## Hard invariant",
        "",
        f"FALSE COMPLETIONS: {', '.join(str(x) for x in false)}"
        if isinstance(false, list) and false
        else "No false completions (every VERIFIED run passed the independent oracle).",
        "",
    ]
    return "\n".join(lines)


def write_report(report: JsonObject, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "report.json", out_dir / "report.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path
