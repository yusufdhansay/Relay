"""Run the Relay evaluation suite.

Usage:
    python -m scripts.run_evals                 # mock provider (default)
    RELAY_LLM_PROVIDER=openai RELAY_OPENAI_API_KEY=... python -m scripts.run_evals --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import Settings
from evals.report import render_markdown
from evals.runner import run_suite

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use RELAY_* settings from env/.env")
    parser.add_argument("--output", default=str(ROOT / "docs" / "evaluation"), help="Output dir")
    args = parser.parse_args()

    settings = None
    if args.live:
        settings = Settings(environment="test", log_level="ERROR")
    report = asyncio.run(run_suite(settings))

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    suffix = report.provider
    (out / f"results-{suffix}.json").write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    (out / f"results-{suffix}.md").write_text(render_markdown(report))

    print(f"Cases: {len(report.cases)}  provider: {report.provider}")
    for group in ("system", "quality"):
        print(f"\n{group}:")
        for name, metric in getattr(report, group).items():
            value = metric["value"] if isinstance(metric, dict) else metric
            print(f"  {name:32} {value}")
    print(f"\nWritten to {out}")


if __name__ == "__main__":
    main()
