#!/usr/bin/env python3
"""Rebuild index.html and index.json of the certificates site from r/*/report.json.
Usage: index.py SITE_FOLDER"""
import html
import json
import sys
from pathlib import Path


def main(argv):
    site = Path(argv[1])
    entries = []
    for path in sorted(site.glob("r/*/report.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries.append({"path": path.parent.relative_to(site).as_posix() + "/",
                        "box": report.get("box"), "verdict": report.get("verdict"),
                        "drop_id": report.get("drop_id"), "started": report.get("started")})
    entries.sort(key=lambda e: e.get("started") or "")
    (site / "index.json").write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    rows = "".join(
        f"<tr><td><a href=\"{html.escape(e['path'])}certificate.html\">{html.escape(str(e['box']))}"
        f"</a></td><td>{html.escape(str(e['verdict']))}</td><td>{html.escape(str(e['started']))}"
        f"</td></tr>" for e in reversed(entries))
    (site / "index.html").write_text(
        "<!doctype html><meta charset=\"utf-8\"><title>research-box reruns</title>"
        "<h1>research-box reruns</h1><p>Each rerun ran a drop three times on GitHub Actions "
        "with no network, and its report is signed by the repository's rerun workflow "
        "through Sigstore.</p><table><tr><th>Box</th><th>Verdict</th><th>Run</th></tr>"
        f"{rows}</table><p><small>research-box is built with Claude, by Anthropic. Anthropic "
        "does not run or endorse these reruns.</small></p>\n", encoding="utf-8")
    print(f"{len(entries)} reruns indexed.")


if __name__ == "__main__":
    main(sys.argv)
