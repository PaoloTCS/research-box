#!/usr/bin/env python3
"""Assemble one report from the three reruns of a drop, for the rerun workflow.

The reruns happen in an earlier job that holds no credentials. This script runs in the job
that signs (attests), and it never runs the box's code. It trusts the earlier job only for
what the box printed. Everything it can work out itself it works out again from the drop:
which drop it is, the declared command and expectations, and whether the declared values
already appear in the box's own files. A run report that disagrees is refused, and then
nothing is signed.

Exit status: 0 a report was written (whatever the verdict); 1 the run reports do not match
the drop, so nothing should be signed; 2 bad input.
"""
import argparse
import html
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

RUNS = 3
HERE = Path(__file__).resolve().parent
TOOL = HERE.parent.parent / "research-box" / "scripts" / "build_box.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("build_box", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def declared(bb, drop):
    """The command and the (label, expected) rows that the drop itself declares."""
    box = json.loads((drop / "box.json").read_text(encoding="utf-8"))

    def stop(message):
        raise SystemExit(message)

    block = bb.read_reproduce(drop, box, stop)
    if block is None:
        raise SystemExit("the drop declares no reproduce command")
    # Compare against empty output: every row then carries the drop's own label and rule.
    # Named-number checks need an empty JSON object, or they collapse into one "not JSON" row.
    rows = [(r[0], r[1]) for e in block["expect"]
            for r in bb.compare(e, b"{}" if e["match"] == "values" else b"")]
    return block["command"], rows


def problems_with(run, bb, drop_id, command, rows):
    found = []
    if run.get("drop_id") != drop_id:
        found.append("names another drop")
    if run.get("command") != command:
        found.append("names another command")
    if run.get("tool") != f"research-box {bb.VERSION}":
        found.append("was made by another version of the tool")
    if not str(run.get("network", "")).startswith("none"):
        found.append("ran with a network")
    if run.get("claim") != bb.REPORT_CLAIM:
        found.append("states another claim")
    got = [(e.get("output"), e.get("expected")) for e in run.get("expectations") or []
           if isinstance(e, dict)]
    if run.get("verdict") in ("reproduced", "did not reproduce") and got != rows:
        found.append("lists other expectations than the drop declares")
    if run.get("verdict") == "reproduced" and not all(
            e.get("matched") for e in run.get("expectations") or []):
        found.append("says reproduced with a value that did not match")
    return found


def stored(bb, drop, runs):
    """Declared values that already appear in the drop, worked out again here."""
    literals = []
    for e in runs[0].get("expectations") or []:
        value = e.get("observed")
        if e.get("matched") and isinstance(value, (int, float)) and not isinstance(value, bool):
            literals.append((e.get("output"), value))
    return bb.stored_values(drop, literals, [])


def page(report, source):
    r = report["runner"]
    rows = "".join(
        f"<tr><td>{html.escape(str(e.get('output')))}</td>"
        f"<td>{html.escape(str(e.get('expected')))}</td>"
        f"<td>{html.escape(str(e.get('observed')))}</td>"
        f"<td>{'match' if e.get('matched') else 'differs'}</td></tr>"
        for e in report.get("expectations") or [])
    flagged = "".join(
        f"<li>{html.escape(str(s['output']))} = {html.escape(str(s['value']))} appears in "
        f"{html.escape(', '.join(s['files']))}</li>" for s in report.get("stored_values") or [])
    title = ("Declared outputs reproduced (automated rerun on GitHub Actions)"
             if report["verdict"] == "reproduced"
             else f"Rerun on GitHub Actions: {report['verdict']}")
    return f"""<!doctype html><meta charset="utf-8">
<title>{html.escape(title)}: {html.escape(report['box'])}</title>
<h1>{html.escape(title)}: {html.escape(report['box'])}</h1>
<p><b>Verdict:</b> {html.escape(report['verdict'])}; runs: {', '.join(map(html.escape, r['verdicts']))}; no network.</p>
<p><b>What this shows:</b> {html.escape(report['claim'])}</p>
<table><tr><th>Output</th><th>Expected</th><th>Observed (run 1)</th><th></th></tr>{rows}</table>
{('<h2>Values already in the box</h2><p>These values appear in the box' + chr(39) + 's own files, so the code may print them rather than compute them:</p><ul>' + flagged + '</ul>') if flagged else ''}
<h2>Run</h2><p>drop {html.escape(report['drop_id'])}<br>
{html.escape(report['platform'])}, Python {html.escape(report['python'])}, {html.escape(report['tool'])}<br>
runner image {html.escape(str(r.get('runner_image')))}<br>
workflow run <a href="{html.escape(r['workflow_run'])}">{html.escape(r['workflow_run'])}</a> at commit {html.escape(r['workflow_sha'])}<br>
drop file {html.escape(source['url'])} (sha256 {html.escape(source['sha256'])})</p>
<h2>Check it yourself</h2><pre>gh attestation verify report.json --repo {html.escape(r['repository'])} \\
  --signer-workflow {html.escape(r['repository'])}/.github/workflows/rerun.yml
python3 build_box.py DROP --verify-report report.json --attested-by {html.escape(r['repository'])}</pre>
<p>The signature is GitHub's keyless signing through Sigstore, tied to this repository's
rerun workflow, and recorded in Sigstore's public transparency log. It shows which workflow
produced the report, not that the result is correct.</p>
<p><small>research-box is built with Claude, by Anthropic. Anthropic does not run or endorse
these reruns.</small></p>
"""


def main(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--drop", type=Path, required=True)
    p.add_argument("--runs", type=Path, required=True, help="folder with run-1.json ...")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--repository", required=True)
    p.add_argument("--workflow-run", required=True)
    p.add_argument("--workflow-sha", required=True)
    p.add_argument("--runner-image", default="")
    p.add_argument("--source-url", required=True)
    p.add_argument("--source-sha256", required=True)
    p.add_argument("--submitter", default="")
    args = p.parse_args(argv[1:])

    bb = load_tool()
    drop = args.drop
    if bb.check(drop) != 0:
        print("The drop does not match its own manifests. Nothing to sign.")
        return 1
    drop_id = "sha256:" + bb.sha256(drop / "tagmanifest-sha256.txt")
    try:
        command, rows = declared(bb, drop)
    except SystemExit as err:
        print(f"Cannot read the drop's reproduce block ({err}). Nothing to sign.")
        return 1
    runs = []
    for i in range(1, RUNS + 1):
        path = args.runs / f"run-{i}.json"
        try:
            runs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as err:
            print(f"Run {i} left no readable report ({err}). Nothing to sign.")
            return 1
    for i, run in enumerate(runs, 1):
        found = problems_with(run, bb, drop_id, command, rows)
        if found:
            print(f"Run {i}'s report {'; '.join(found)}. Nothing to sign.")
            return 1

    verdicts = [run["verdict"] for run in runs]
    if all(v == "reproduced" for v in verdicts):
        verdict = "reproduced"
    elif "did not reproduce" in verdicts:
        verdict = "did not reproduce"
    else:
        verdict = "inconclusive"
    observed = [[e.get("observed") for e in run.get("expectations") or []] for run in runs]
    report = dict(runs[0])
    report["verdict"] = verdict
    report["stored_values"] = stored(bb, drop, runs)
    report["runner"] = {
        "runner": "GitHub Actions, research-box rerun workflow (beta)",
        "repository": args.repository,
        "workflow_run": args.workflow_run,
        "workflow_sha": args.workflow_sha,
        "runner_image": args.runner_image or None,
        "runs": RUNS,
        "verdicts": verdicts,
        "all_reproduced": verdict == "reproduced",
        "observed_identical": all(o == observed[0] for o in observed),
        "source": {"url": args.source_url, "sha256": args.source_sha256},
        "submitter": args.submitter or None,
        "assembled": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    (args.out / "certificate.html").write_text(
        page(report, report["runner"]["source"]), encoding="utf-8")
    print(f"{report['box']}: {verdict} ({', '.join(verdicts)}). Report in {args.out}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
