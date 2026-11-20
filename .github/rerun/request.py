#!/usr/bin/env python3
"""Decide what to do with a rerun request issue. Reads everything from the environment and
from JSON files the workflow fetched; never runs anything. Writes the decision to
GITHUB_OUTPUT (or prints it) and the comment text to --comment.

Decisions: ignore (not a request), refuse (bad input or over a limit), wait (needs a
maintainer's label), run (dispatch the rerun)."""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

MARKER = "### Drop address"
PER_PERSON_PER_DAY = 3
PER_DAY = 20
MIN_ACCOUNT_AGE_DAYS = 7
APPROVE_LABEL = "rerun-approved"
MAINTAINER = {"admin", "maintain", "write"}
URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._~%/+=&?:@-]{1,1800}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
NAME_RE = re.compile(r"[\w .,'()-]{0,80}")
ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")


def fields(body):
    """Issue-form body: '### Label' headings, each followed by its value."""
    out, key, lines = {}, None, []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if line.startswith("### "):
            if key is not None:
                out[key] = "\n".join(lines).strip()
            key, lines = line[4:].strip(), []
        elif key is not None:
            lines.append(line)
    if key is not None:
        out[key] = "\n".join(lines).strip()
    return {k: ("" if v == "_No response_" else v) for k, v in out.items()}


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def clean_submitter(raw, login):
    raw = (raw or "").strip()
    if ORCID_RE.fullmatch(raw.removeprefix("https://orcid.org/")):
        return "ORCID " + raw.removeprefix("https://orcid.org/") + f" (@{login})"
    if raw and NAME_RE.fullmatch(raw) and "@" not in raw:
        return f"{raw} (@{login})"
    return f"@{login}"


def decide(env, author_issues, now):
    body = env.get("BODY", "")
    if MARKER not in body:
        return {"action": "ignore"}, ""
    number, login = env["ISSUE"], env["AUTHOR"]
    if env.get("EVENT_ACTION") == "labeled":
        if env.get("LABEL") != APPROVE_LABEL:
            return {"action": "ignore"}, ""
        if env.get("SENDER_PERMISSION") not in MAINTAINER:
            return {"action": "ignore"}, ""
    f = fields(body)
    url = f.get("Drop address", "").strip()
    sha = f.get("Archive sha256", "").strip().lower()
    problems = []
    if not URL_RE.fullmatch(url):
        problems.append("The drop address must be one plain https address.")
    if not SHA_RE.fullmatch(sha):
        problems.append("The sha256 must be 64 hex digits, as `sha256sum` prints it.")
    if problems:
        return {"action": "refuse"}, "This request can't run yet.\n\n" + "\n".join(
            f"- {p}" for p in problems) + "\n\nEdit nothing here; open a new request with the fixes."
    created = env.get("AUTHOR_CREATED", "")
    if not created or now - parse_time(created) < timedelta(days=MIN_ACCOUNT_AGE_DAYS):
        return {"action": "refuse"}, (f"Reruns are open to GitHub accounts at least "
                                       f"{MIN_ACCOUNT_AGE_DAYS} days old, to keep the free beta "
                                       f"usable for everyone.")
    since = now - timedelta(days=1)
    mine = [i for i in author_issues
            if str(i.get("number")) != str(number) and MARKER in (i.get("body") or "")
            and "pull_request" not in i and parse_time(i["created_at"]) >= since]
    if len(mine) >= PER_PERSON_PER_DAY:
        return {"action": "refuse"}, (f"You have opened {PER_PERSON_PER_DAY} rerun requests in "
                                       f"the last 24 hours, the beta's limit. Please try again "
                                       f"tomorrow.")
    if int(env.get("RUNS_TODAY", "0")) >= PER_DAY:
        return {"action": "refuse"}, (f"The beta has run its {PER_DAY} reruns for the last 24 "
                                       f"hours. Please open a new request tomorrow.")
    if env.get("APPROVAL", "auto") == "maintainer" and env.get("EVENT_ACTION") != "labeled":
        return {"action": "wait"}, ("Thanks. A maintainer will start this rerun; you'll get a "
                                    "comment here when it's done.")
    decision = {"action": "run", "drop_url": url, "drop_sha256": sha,
                "submitter": clean_submitter(f.get("Name or ORCID to show on the report (optional)"),
                                             login)}
    return decision, ("Rerun started: three runs with no network on GitHub's machines. "
                      "This issue gets a comment with the result when it's done.")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--author-issues", required=True, help="JSON list of the author's issues")
    p.add_argument("--comment", required=True, help="file to write the comment to")
    a = p.parse_args(argv)
    issues = json.loads(open(a.author_issues).read() or "[]")
    decision, comment = decide(os.environ, issues, datetime.now(timezone.utc))
    with open(a.comment, "w") as fh:
        fh.write(comment)
    lines = [f"{k}={v}" for k, v in decision.items()]
    for line in lines:
        if "\n" in line or "\r" in line:
            sys.exit("refusing to write a multi-line output")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    print(json.dumps(decision))


if __name__ == "__main__":
    main()
