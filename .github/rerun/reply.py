#!/usr/bin/env python3
"""Write the comment that answers a rerun request. Uses only values the workflow computed or
checked; the box's own text (its name, its outputs) never goes into the comment."""
import argparse
import json
from pathlib import Path

VERDICTS = {"reproduced", "did not reproduce", "inconclusive"}


def comment(report_path, run_url, site, run_id, dry_run):
    report = None
    if report_path and Path(report_path).is_file():
        try:
            report = json.loads(Path(report_path).read_text())
        except ValueError:
            report = None
    if report is None:
        return ("The rerun did not produce a report, so nothing was signed. The workflow run "
                f"shows what happened: {run_url}\n\nCommon causes: the archive's sha256 didn't "
                "match, the address wasn't reachable, or the archive isn't a drop made with "
                "`--out`.")
    verdict = report.get("verdict")
    verdict = verdict if verdict in VERDICTS else "inconclusive"
    runner = report.get("runner", {})
    runs = [v for v in runner.get("verdicts", []) if v in VERDICTS]
    lines = [f"Verdict: **{verdict}** ({', '.join(runs) or 'no runs'}), three runs with no network."]
    if dry_run:
        lines.append("\nThis was a test run: nothing was signed or published.")
    else:
        lines.append(f"\nSigned report and certificate page: {site.rstrip('/')}/r/{run_id}/certificate.html")
        lines.append("\nThe signature shows which workflow produced the report. A reproduced "
                     "verdict shows that these exact files, run with the declared command, gave "
                     "the declared outputs; it does not show that the method is correct.")
    lines.append(f"\nWorkflow run: {run_url}")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--report")
    p.add_argument("--run-url", required=True)
    p.add_argument("--site", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--dry-run", default="false")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    Path(a.out).write_text(comment(a.report, a.run_url, a.site, a.run_id, a.dry_run == "true"))


if __name__ == "__main__":
    main()
