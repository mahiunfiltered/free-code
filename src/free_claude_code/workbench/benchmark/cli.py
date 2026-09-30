"""``fcc-bench``: run the benchmark scenarios and write report.json / report.md.

    fcc-bench                                    # fake engine, every scenario (CI-safe)
    fcc-bench --engine real --proxy http://127.0.0.1:8082 --scenario bug_fix
Exit 1 on any false completion (or, with the fake engine, any unexpected disposition).
"""

import argparse
import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from free_claude_code.config.loader import get_settings
from free_claude_code.config.paths import config_dir_path
from free_claude_code.core.gateway_model_ids import gateway_model_id
from free_claude_code.workbench.intent import ModelClient
from free_claude_code.workbench.model_client import ProxyModelClient

from .harness import Engine, ScenarioResult, run_suite
from .report import exit_code, summarize, write_report
from .scenarios import SCENARIOS


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fcc-bench", description="FCC verified-execution benchmark."
    )
    parser.add_argument("--engine", choices=("fake", "real"), default="fake")
    parser.add_argument("--proxy", default="http://127.0.0.1:8082")
    parser.add_argument(
        "--scenario", action="append", choices=sorted(SCENARIOS), dest="scenarios"
    )
    parser.add_argument(
        "--timeout", type=float, help="per-scenario seconds (fake 120, real 900)"
    )
    parser.add_argument("--out", type=Path, help="default ./.fcc-bench/<timestamp>")
    parser.add_argument("--model", help="Claude session model (real engine)")
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument(
        "--usage-db", type=Path, help="server fcc.db for failover counts (real engine)"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the scenario repos for inspection"
    )
    parser.add_argument(
        "--user-config",
        action="store_true",
        help="real engine: use your ~/.claude (plugins/hooks may write into the repos)",
    )
    return parser


def _engine(args: argparse.Namespace) -> Engine:
    if args.engine == "fake":
        return Engine("fake")
    settings = get_settings()
    proxy = args.proxy.rstrip("/")
    token = settings.proxy_auth_token

    def target() -> tuple[str, str]:
        return proxy, token

    def helper(model: str | None) -> ModelClient:
        return ProxyModelClient(target, model or gateway_model_id(settings.model))

    return Engine(
        "real",
        proxy_url=proxy,
        auth_token=token,
        claude_bin=args.claude_bin,
        model=args.model,
        helper=helper,
        usage_db=args.usage_db or config_dir_path() / "fcc.db",
    )


def _progress(result: ScenarioResult) -> None:
    flag = "  FALSE COMPLETION" if result.false_completion else ""
    print(
        f"{result.scenario:<22} {result.disposition:<18} oracle="
        f"{'pass' if result.oracle.passed else 'FAIL'} {result.elapsed_s:>7.1f}s{flag}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if importlib.util.find_spec("pytest") is None:
        print("fcc-bench needs pytest in this Python environment.", file=sys.stderr)
        return 2
    engine = _engine(args)
    timeout = args.timeout or (120.0 if engine.kind == "fake" else 900.0)
    scenarios = [SCENARIOS[s] for s in args.scenarios or SCENARIOS]
    out = args.out or Path(".fcc-bench") / datetime.now().strftime("%Y%m%d-%H%M%S")
    workdir = Path(tempfile.mkdtemp(prefix="fcc-bench-"))
    isolated = engine.kind == "real" and not args.user_config
    if isolated:
        # User hooks/plugins write files into the cwd, which the gate rightly flags as
        # out-of-scope. Auth goes through the proxy env, so an empty config dir works.
        previous = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(workdir / "claude-config")
    try:
        results = asyncio.run(
            run_suite(
                scenarios, engine, workdir, timeout_s=timeout, on_result=_progress
            )
        )
    finally:
        if isolated:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            if previous is not None:
                os.environ["CLAUDE_CONFIG_DIR"] = previous
        if args.keep:
            print(f"scenario repos kept in {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    report = summarize(
        results,
        {
            "engine": engine.kind,
            "proxy": engine.proxy_url or None,
            "timeout_s": timeout,
            "isolated_claude_config": isolated,
        },
    )
    json_path, md_path = write_report(report, out)
    print(f"report: {md_path} ({json_path.name})")
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
