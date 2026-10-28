"""Command line: `demo` builds everything from the fictional private cloud, `review` runs on your own exports."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import report, simulate
from .checks import run_all
from .model import load, parse_date

OUTPUTS = ("report.html", "capacity-review.md", "clusters.csv", "forecast.csv", "reclaim.csv", "datastores.csv", "demand.csv", "actions.csv",
           "findings.csv")


def run(data: Path, out: Path, title: str, as_of: date | None = None) -> dict:
    d = load(data, as_of)
    b = report.build(d)
    findings = run_all(d, b["rows"], b["reclaim"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.html").write_text(report.render_html(d, findings, title, b), encoding="utf-8")
    (out / "capacity-review.md").write_text(report.review_md(d, findings, b), encoding="utf-8")
    report.write_clusters(d, b, out / "clusters.csv")
    report.write_forecast(b, out / "forecast.csv")
    report.write_reclaim(b, out / "reclaim.csv")
    report.write_datastores(b, out / "datastores.csv")
    report.write_demand(d, b, out / "demand.csv")
    report.write_actions(findings, out / "actions.csv")
    report.write_findings(findings, out / "findings.csv")
    sm = report.summary(d, findings, b)
    sm["headline"] = report.headline(d, sm)
    return sm


def _day(value: str) -> date:
    d = parse_date(value)
    if not d:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {value}")
    return d


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="capreview", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    dm = sub.add_parser("demo", help="write the fictional private cloud to sample_data/ and build the outputs")
    dm.add_argument("--data", type=Path, default=Path("sample_data"))
    dm.add_argument("--out", type=Path, default=Path("output"))
    rv = sub.add_parser("review", help="build the outputs from your own exports")
    rv.add_argument("--data", type=Path, required=True,
                    help="folder with clusters.csv and hosts.csv (required) and vms.csv, snapshots.csv, datastores.csv, utilisation.csv, "
                         "demand.csv, events.csv, settings.json (optional)")
    rv.add_argument("--out", type=Path, default=Path("output"))
    rv.add_argument("--title", default="Capacity and availability review")
    rv.add_argument("--as-of", type=_day, help="date of the capacity meeting (default: settings.json, or today)")
    a = p.parse_args(argv)
    if a.cmd == "demo":
        simulate.write(a.data)
        sm = run(a.data, a.out, f"{simulate.ORG} · Capacity and availability review")
    else:
        try:
            sm = run(a.data, a.out, a.title, a.as_of)
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(sm["headline"])
    print(f"outputs in {a.out}/: {', '.join(OUTPUTS)}")
    return 0
