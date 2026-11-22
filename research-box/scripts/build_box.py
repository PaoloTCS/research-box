#!/usr/bin/env python3
"""Build BOX.md, ro-crate-metadata.json and manifest-sha256.txt for a research box.

Usage:
  python3 build_box.py <box-folder> [--for shareable|same-assistant|private] [--out <folder>]
                       [--strict [--allow-unscanned]]
  python3 build_box.py <box-folder> --check
  python3 build_box.py <dropped-box> --reproduce [--yes] [--timeout SECONDS] [--report FILE]
  python3 build_box.py <dropped-box> --verify-report REPORT --allowed-signers FILE
                       [--signer IDENTITY] [--signature FILE]
  python3 build_box.py <dropped-box> --id [--provenance FILE]
  python3 build_box.py <dropped-box> --verify-provenance FILE --allowed-signers FILE
                       [--signer IDENTITY] [--signature FILE]
  python3 build_box.py <box-folder> --lookup
  python3 build_box.py <box-folder> --check-receipts
  python3 build_box.py --help

Reads <box-folder>/box.json (format in references/box-format.md). Standard library only.

--for     Audience of the drop. Items, state lines and box details narrower than the
          audience are left out of every output.
--out     Write a clean copy for that audience into a new or empty folder: only the kept
          snapshot files plus the generated files. The source folder is not touched.
          Required whenever the drop holds anything back (items, state lines or box
          details); without it the script refuses, so nothing held back can sit beside
          what is shared.
--check   Compare the folder's files with manifest-sha256.txt and tagmanifest-sha256.txt and
          report files that changed, went missing or are not in the manifests. Writes nothing.
          Exit status: 0 = everything matches, 1 = a file changed, went missing or is not
          listed, 2 = the check is incomplete (no manifest or no tag manifest). Run it after
          every copy and before a refresh.
--strict  Refuse a shareable drop, writing nothing, when a watch word (private_terms) appears
          in any generated file, text snapshot or Office/OpenDocument file, or when a file
          cannot be scanned (PDFs, images, embedded files) unless --allow-unscanned is given
          after checking those files by eye. Without it, both are reported only.
--reproduce
          Run the box's declared command (box.json "reproduce") and compare its output with
          what the box expects. It runs --check first and stops unless the drop matches,
          shows the command, and asks before running it (--yes skips the question). The
          command runs without a shell, on a temporary copy of the drop, with only PATH,
          LANG and a temporary HOME from your environment, under a timeout. It is not a
          sandbox: the box's code runs with your permissions. --report writes the result as
          JSON outside the drop. Exit status: 0 = reproduced, 1 = did not reproduce (or the
          drop does not match its manifests), 2 = not run or inconclusive (no reproduce
          block, no manifest, declined, could not start, or timed out).

--verify-report
          Check a --report file that someone signed with an SSH key (ssh-keygen -Y sign -n
          research-box-report REPORT, which writes REPORT.sig). Runs nothing from the box.
          It checks the drop against its manifests, the signature against an OpenSSH
          allowed_signers file (--allowed-signers) using the local ssh-keygen, that the
          report names this drop (drop_id), and that its verdict is reproduced. It then works
          the comparison out again from the drop's own expectations and the report's observed
          values (same tolerances as --reproduce) and fails a report whose rows, exit code or
          verdict disagree with that. Exit status:
          0 verified, 1 a signature, drop, file or verdict does not match, 2 could not check
          (no manifests, unreadable report, no signature file, or no ssh-keygen).

--id      Print the drop's portable identifiers: its content id (the SHA-256 of the manifest's
          lines sorted by path, so the same files give the same id on any machine) and its
          drop id (the SHA-256 of tagmanifest-sha256.txt). --provenance FILE writes them, with
          the author's identifiers (author_ids, e.g. ORCID or ISNI) and the drop it replaces
          (previous), as a provenance record to sign with an SSH key (Ed25519):
          ssh-keygen -Y sign -n research-box-provenance FILE.
--verify-provenance
          Check a signed provenance record against this drop: the drop matches its
          manifests, an allowed key signed the record, and every identifier in it is the
          drop's own. Exit status: 0 verified, 1 does not match, 2 could not check.

--lookup  Ask the registries about every ORCID iD and ISNI in author_ids and every DOI an
          item links to (live: https://doi.org/10... or doi:10...), and save each answer as
          a receipt under receipts/: the claim, the registry and address asked, the time, the
          HTTP status, the response saved byte for byte, its SHA-256 and the answer read
          from it. DOIs are asked of doi.org twice (the handle record and the citation
          metadata), so a disagreement shows. Uses the network; nothing is sent but the
          identifiers. A drop carries the receipts in its manifests, author receipts always
          and a DOI receipt only with its item. Exit status: 0 every claim found and the
          registries agree, 1 a claim not found, registries disagree or one gave no answer,
          2 nothing to look up.
--check-receipts
          Replay the receipts offline: each saved response must match its SHA-256, have come
          from the registry's own address and still give the recorded answer; registries
          that disagree are shown, never resolved. Registries do not sign their answers, so
          a receipt shows what whoever looked it up saved. Exit status: 0 all hold, 1 one
          changed, is missing, was not found or disagrees, 2 no receipts.

A drop is also refused, writing nothing, when a snapshot path leaves the box folder, a
declared snapshot file is missing, one file is listed under items with different levels, or
a reproduce command uses a path outside the box or needs a file the drop does not hold.

The script never deletes anything. Runs of --reproduce are left in a temporary folder,
which it names, so their outputs can be inspected.
"""
import argparse
import base64
import hashlib
import http.client
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePath

LEVELS = {"private": 0, "same-assistant": 1, "shareable": 2}
LABELS = {"private": "Private", "same-assistant": "Same assistant", "shareable": "Shareable"}
BOX_KEYS = ["box", "title", "description", "created", "dropped", "author", "author_ids",
            "license", "previous", "default_sharing", "topics", "state", "public", "level_labels", "private_terms",
            "prepared_for", "reproduce", "items"]
ITEM_KEYS = ["item", "kind", "live", "live_sha256", "live_pinned", "revision", "snapshot",
             "sharing", "notes"]
REPRO_KEYS = ["name", "command", "inputs", "requires", "timeout_seconds", "sharing", "expect"]
GENERATED = {"BOX.md", "box.json", "ro-crate-metadata.json", "manifest-sha256.txt",
             "tagmanifest-sha256.txt"}
TAGGED = ["BOX.md", "box.json", "ro-crate-metadata.json", "manifest-sha256.txt"]
VERSION = "0.5.1"
# What a reproduction report (and so a signature on it) can claim, and what it cannot.
REPORT_CLAIM = ("Running these exact files with the declared command produced the declared "
                "outputs within the declared tolerances. This does not show that the outputs "
                "were computed rather than stored or fetched, that the method is correct, or "
                "that the result holds on other machines.")
LITERAL_MIN_DIGITS = 4          # shorter numbers (0, 1, 42) appear in any code by chance
LITERAL_MIN_TEXT = 8
LITERAL_MAX_BYTES = 20 << 20


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def level_of(value, box, where):
    """Map a sharing value to a level, accepting the box's own labels (level_labels)."""
    value = value or box.get("default_sharing", "private")
    value = box.get("level_labels", {}).get(value, value)
    if value not in LEVELS:
        sys.exit(f"{where}: unknown sharing level {value!r}; use one of {', '.join(LEVELS)} "
                 f"or map it in level_labels")
    return value


def inside(folder, rel):
    """True if rel is a plain relative path that stays inside folder, symlinks resolved."""
    pure = PurePath(rel)
    if pure.is_absolute() or ".." in pure.parts or not str(rel).strip():
        return False
    root = Path(folder).resolve()
    try:
        (root / pure).resolve().relative_to(root)
    except ValueError:
        return False
    return True


def term_pattern(term):
    """Match a watch word as a whole word or phrase, any case. A word ends at anything
    that is not a letter or digit, so "lantern" is found in lantern_key or LANTERN-2 but
    not in "lanterns". Spaces inside a phrase match any run of whitespace, underscores or
    hyphens, so a phrase broken across lines, or written as project_lantern, is found."""
    words = [re.escape(w) for w in term.split()]
    return re.compile(r"(?<![^\W_])" + r"[\s_\-]+".join(words) + r"(?![^\W_])",
                      re.IGNORECASE)


# Office files (Word, Excel, PowerPoint and OpenDocument) are zip archives of XML. Their
# text, comments, sheet names and document properties are read with the standard library
# so watch words in them are found. Other binary files (PDF, images) cannot be read here.
OFFICE = {".docx", ".docm", ".dotx", ".xlsx", ".xlsm", ".xltx", ".pptx", ".pptm", ".potx",
          ".odt", ".ods", ".odp", ".odg"}
OFFICE_LIMIT = 200 * 1024 * 1024  # uncompressed bytes read from one file, at most
# Elements that end a paragraph, cell or property, so text in neighbouring cells is not
# run together; runs inside a paragraph are joined as written.
BLOCKS = {"p", "h", "si", "c", "is", "tc", "tr", "row", "table-cell", "comment", "text",
          "title", "subject", "creator", "keywords", "description", "lastModifiedBy",
          "category", "Company", "Manager", "sheet", "definedName", "cellComment"}


def _flatten(el, parts):
    for value in el.attrib.values():
        parts.append("\n" + value + "\n")
    if el.text:
        parts.append(el.text)
    for child in el:
        _flatten(child, parts)
        if child.tail:
            parts.append(child.tail)
    if el.tag.rsplit("}", 1)[-1] in BLOCKS:
        parts.append("\n")


def office_text(path):
    """Text of an Office or OpenDocument file, and the names of parts that could not be
    read (embedded pictures or files). Returns (None, reason) if it is not readable."""
    try:
        with zipfile.ZipFile(path) as z:
            members = [m for m in z.infolist() if not m.is_dir()]
            if sum(m.file_size for m in members) > OFFICE_LIMIT:
                return None, "too large to read"
            parts, skipped = [], []
            for m in members:
                if not m.filename.endswith(".xml"):
                    if not m.filename.endswith(".rels") and m.filename != "mimetype":
                        skipped.append(m.filename)
                    continue
                try:
                    root = ET.fromstring(z.read(m))
                except ET.ParseError:
                    skipped.append(m.filename)
                    continue
                parts.append(f"\n[{m.filename}]\n")
                _flatten(root, parts)
            return "".join(parts), skipped
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as err:
        return None, f"not a readable archive ({err})"


def find_terms(text, patterns):
    """Return (term, line number, context) for the first occurrence of each term."""
    found = []
    for term, pattern in patterns:
        m = pattern.search(text)
        if m:
            line = text.count("\n", 0, m.start()) + 1
            start, end = max(0, m.start() - 30), min(len(text), m.end() + 30)
            context = " ".join(text[start:end].split())
            found.append((term, line, context))
    return found


def all_files(folder):
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")
                  if p.is_file() and not p.name.startswith("."))


def check(folder):
    manifest = folder / "manifest-sha256.txt"
    if not manifest.is_file():
        print("No manifest-sha256.txt: there is no baseline, so hand edits cannot be detected. "
              "Compare against a copy you kept, or keep each old file as <name>.prev before "
              "replacing it.")
        return 2
    recorded, incomplete = {}, False
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.strip():
            h, rel = line.split("  ", 1)
            recorded[rel] = h
    tag = folder / "tagmanifest-sha256.txt"
    if tag.is_file():
        for line in tag.read_text(encoding="utf-8").splitlines():
            if line.strip():
                h, rel = line.split("  ", 1)
                recorded[rel] = h
    else:
        print("No tagmanifest-sha256.txt: BOX.md and box.json cannot be verified, so this "
              "check is incomplete.")
        incomplete = True
    outside = [r for r in recorded if not inside(folder, r)]
    for r in outside:
        del recorded[r]
    if outside:
        print("Listed paths outside the folder (not read): " + ", ".join(outside))
    changed = [r for r, h in recorded.items() if (folder / r).is_file() and sha256(folder / r) != h]
    missing = [r for r in recorded if not (folder / r).is_file()]
    extra = [r for r in all_files(folder) if r not in recorded and r not in GENERATED]
    for label, rows in (("Changed since the last drop", changed), ("Missing", missing),
                        ("Not in the manifest", extra)):
        if rows:
            print(f"{label}: " + ", ".join(rows))
    if changed or missing or extra or outside:
        return 1
    if incomplete:
        print("The snapshot files match, but the check is incomplete.")
        return 2
    print("All files match the manifest.")
    return 0

# ---------------------------------------------------------------------------------------
# Reproduce on open: a box may declare one command and what its output should be.
# The drop checks the declaration; --reproduce runs it on a temporary copy, only after the
# opener confirms, and reports whether the result reproduced.

MATCHES = ("sha256", "text", "values")
DEFAULT_TIMEOUT = 600


def unsafe_arg(arg):
    """Why a command argument may not ship in a box, or None. Arguments are not paths in
    general, but anything that looks like a path must be a relative path inside the box,
    so a private path such as /Users/<name>/... can never travel in a drop."""
    if not isinstance(arg, str) or not arg:
        return "every argument must be a non-empty string"
    if arg.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:[\\/]", arg):
        return f"{arg!r} is an absolute or home path"
    if ".." in re.split(r"[\\/=]", arg):
        return f"{arg!r} leads out of the box folder"
    return None


def read_reproduce(folder, box, refuse):
    """Check the reproduce block's shape and paths. Returns it, or None if there is none."""
    block = box.get("reproduce")
    if block is None:
        return None
    if not isinstance(block, dict):
        refuse("reproduce must be an object.")
    unknown = [k for k in block if k not in REPRO_KEYS]
    if unknown:
        refuse("Unknown field(s) in reproduce: " + ", ".join(unknown) + ".")
    command = block.get("command")
    if not isinstance(command, list) or not command:
        refuse("reproduce.command must be a list: the program, then its arguments. "
               "It runs without a shell, so write pipes and loops in a script in code/.")
    for arg in command:
        why = unsafe_arg(arg)
        if why:
            refuse(f"reproduce.command: {why}.")
    for rel in block.get("inputs", []):
        if not isinstance(rel, str) or not inside(folder, rel):
            refuse(f"reproduce.inputs: {rel!r} is not a relative path inside the box folder.")
    t = block.get("timeout_seconds", DEFAULT_TIMEOUT)
    if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
        refuse("reproduce.timeout_seconds must be a positive number.")
    expect = block.get("expect")
    if not isinstance(expect, list) or not expect:
        refuse("reproduce.expect must list at least one expected output.")
    for i, e in enumerate(expect, 1):
        where = f"reproduce.expect[{i}]"
        if not isinstance(e, dict):
            refuse(f"{where} must be an object.")
        src = e.get("source")
        if src != "stdout" and (not isinstance(src, str) or not inside(folder, src)):
            refuse(f"{where}.source must be \"stdout\" or a relative path inside the box "
                   f"folder; got {src!r}.")
        match = e.get("match")
        if match not in MATCHES:
            refuse(f"{where}.match must be one of {', '.join(MATCHES)}.")
        if match == "sha256" and not re.fullmatch(r"[0-9a-f]{64}", str(e.get("value", ""))):
            refuse(f"{where}.value must be 64 lowercase hex digits.")
        if match == "text" and not isinstance(e.get("value"), str):
            refuse(f"{where}.value must be text.")
        if match == "values":
            vals = e.get("values")
            if not isinstance(vals, list) or not vals:
                refuse(f"{where}.values must list at least one named number.")
            for v in vals:
                if not isinstance(v, dict) or not isinstance(v.get("key"), str):
                    refuse(f"{where}.values: each entry needs a key.")
                nums = {k: v[k] for k in ("equals", "abs", "rel", "at_most", "at_least") if k in v}
                if any(isinstance(n, bool) or not isinstance(n, (int, float))
                       for n in nums.values()):
                    refuse(f"{where}.values[{v['key']!r}]: equals, abs, rel, at_most and "
                           f"at_least must be numbers.")
                if not ({"equals", "at_most", "at_least"} & set(nums)):
                    refuse(f"{where}.values[{v['key']!r}] needs equals, at_most or at_least.")
                if ({"abs", "rel"} & set(nums)) and "equals" not in nums:
                    refuse(f"{where}.values[{v['key']!r}]: abs and rel go with equals.")
    return block


def reproduce_files(folder, block):
    """Box files the command needs: arguments naming a file in the box, plus inputs."""
    files = [a for a in block["command"] if (Path(folder) / a).is_file() and inside(folder, a)]
    return list(dict.fromkeys(files + list(block.get("inputs", []))))


def command_text(command):
    return " ".join(shlex.quote(a) for a in command)


def value_rule(v):
    """One named number's rule in words, e.g. "= 2.84 within 0.01" or "<= 9"."""
    parts = []
    if "equals" in v:
        tol = [f"{v['abs']:g}" for _ in [0] if "abs" in v] + \
              [f"{v['rel']:g} relative" for _ in [0] if "rel" in v]
        parts.append(f"= {v['equals']}" + (f" within {' or '.join(tol)}" if tol else ""))
    if "at_least" in v:
        parts.append(f">= {v['at_least']}")
    if "at_most" in v:
        parts.append(f"<= {v['at_most']}")
    return ", ".join(parts)


def describe_expect(e):
    if e["match"] == "values":
        return f"{e['source']}: " + "; ".join(f"{v['key']} {value_rule(v)}" for v in e["values"])
    if e["match"] == "sha256":
        return f"{e['source']}: SHA-256 {e['value']}"
    return f"{e['source']}: the text {e['value']!r}"


def lookup(data, key):
    """Follow a dotted key into parsed JSON; list positions are numbers."""
    for part in key.split(".") if key else []:
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            raise KeyError(key)
    return data


def normal_text(raw):
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def compare(e, raw):
    """Compare one expectation with the bytes produced (None if missing). Returns rows of
    (label, expected, observed, matched)."""
    label = e["source"]
    if raw is None:
        return [(label, describe_expect(e), "missing", False)]
    if e["match"] == "sha256":
        h = hashlib.sha256(raw).hexdigest()
        return [(f"{label} (SHA-256)", e["value"], h, h == e["value"])]
    if e["match"] == "text":
        got = normal_text(raw)
        return [(f"{label} (text)", e["value"], got, got == normal_text(e["value"].encode()))]
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return [(f"{label} (values)", describe_expect(e), "not JSON", False)]
    rows = []
    for v in e["values"]:
        name = f"{label} {v['key']}"
        try:
            got = lookup(data, v["key"])
        except KeyError:
            rows.append((name, value_rule(v), "missing", False))
            continue
        if isinstance(got, bool) or not isinstance(got, (int, float)):
            rows.append((name, value_rule(v), repr(got), False))
            continue
        rows.append((name, value_rule(v), got, value_ok(v, got)))
    return rows


def value_ok(v, got):
    """Whether a number meets one named number's rule (equals within abs or rel, at_least,
    at_most). Anything that is not a number does not."""
    if isinstance(got, bool) or not isinstance(got, (int, float)):
        return False
    ok = True
    if "equals" in v:
        allowed = max(v.get("abs", 0), v.get("rel", 0) * abs(v["equals"]))
        ok = abs(got - v["equals"]) <= allowed
    if "at_least" in v:
        ok = ok and got >= v["at_least"]
    if "at_most" in v:
        ok = ok and got <= v["at_most"]
    return ok


def explain(e, row, raw=None):
    """One structured difference for a row that did not match: which output (and key)
    missed, what was expected and observed, and by how much against the tolerance. The
    row is (label, expected, observed, matched) as compare() writes it; raw is the output's
    bytes where they are at hand (for the first differing line of a text output)."""
    label, expected, observed, _ = row
    src = e["source"]
    d = {"output": label, "source": src, "match": e["match"], "expected": expected,
         "observed": observed}
    if label == src and observed == "missing":
        d.update(reason="output missing", summary="the command did not write this output")
        return d
    if e["match"] == "values" and label == f"{src} (values)":
        d.update(reason="not JSON", summary="the output is not JSON, so no value could be read")
        return d
    if e["match"] == "sha256":
        d.update(reason="hash differs", summary="the bytes differ from the declared SHA-256")
        if raw is not None:
            d["observed_bytes"] = len(raw)
        return d
    if e["match"] == "text":
        want = normal_text(e["value"].encode("utf-8")).split("\n")
        got = normal_text(raw if raw is not None else str(observed).encode("utf-8")).split("\n")
        line = next((i for i in range(max(len(want), len(got)))
                     if (want[i:i + 1] or [None]) != (got[i:i + 1] or [None])), None)
        d.update(reason="text differs", summary="the text differs")
        if line is not None:
            exp_line = want[line] if line < len(want) else None
            got_line = got[line] if line < len(got) else None
            d["first_difference"] = {"line": line + 1, "expected": exp_line,
                                     "observed": got_line}
            d["summary"] = f"the text differs from line {line + 1}"
        return d
    v = next((v for v in e["values"] if label == f"{src} {v['key']}"), None)
    if v is None:  # not a row this expectation writes; describe it as it stands
        d.update(reason="differs", summary="differs")
        return d
    d["key"] = v["key"]
    if observed == "missing":
        d.update(reason="key missing", summary=f"no value at {v['key']} in the output")
        return d
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        d.update(reason="not a number", summary=f"{v['key']} is {observed!r}, not a number")
        return d
    reasons, words = [], []
    if "equals" in v:
        allowed = max(v.get("abs", 0), v.get("rel", 0) * abs(v["equals"]))
        off = observed - v["equals"]
        d.update(target=v["equals"], difference=off, allowed=allowed,
                 times_allowed=(abs(off) / allowed) if allowed else None)
        if abs(off) > allowed:
            reasons.append("outside tolerance")
            words.append(f"off by {off:+g}, " + (
                f"{abs(off) / allowed:.3g} times the allowed {allowed:g}" if allowed
                else "and no difference is allowed"))
    if "at_least" in v and observed < v["at_least"]:
        d["below_by"] = v["at_least"] - observed
        reasons.append("below at_least")
        words.append(f"{d['below_by']:g} below the least allowed {v['at_least']:g}")
    if "at_most" in v and observed > v["at_most"]:
        d["above_by"] = observed - v["at_most"]
        reasons.append("above at_most")
        words.append(f"{d['above_by']:g} above the most allowed {v['at_most']:g}")
    d.update(reason="; ".join(reasons) or "differs", summary="; ".join(words) or "differs")
    return d


def recheck_expectations(block, rows):
    """Work out again, from the drop's own declared expectations (block["expect"]) and the
    observed values a report records, whether each output matched. Rows are the report's
    "expectations" list. Returns (checked, problems): checked is a list of (label,
    expected, observed, matched) as worked out here; problems lists every way the report's
    rows disagree with the drop or with themselves. An empty problems list means the rows
    are exactly what --reproduce writes for those observed values."""
    if not isinstance(rows, list) or not rows:
        return [], ["the report lists no expectations"]
    checked, problems = [], []

    def take(i):
        row = rows[i] if i < len(rows) else None
        if not isinstance(row, dict) or not all(k in row for k in
                                                ("output", "expected", "observed", "matched")):
            return None
        return row

    def judge(row, label, expected, matched):
        observed = row["observed"]
        checked.append((label, expected, observed, matched))
        if row["output"] != label or row["expected"] != expected:
            problems.append(f"the row {row['output']!r} (expected {row['expected']!r}) is not "
                            f"what the drop declares ({label!r}, expected {expected!r})")
        elif row["matched"] is not matched:
            problems.append(f"{label}: the report says "
                            f"{'match' if row['matched'] else 'differs'}, but observed "
                            f"{observed!r} against expected {expected!r} "
                            f"{'matches' if matched else 'differs'}")

    i = 0
    for e in block["expect"]:
        src = e["source"]
        row = take(i)
        if row is None:
            problems.append(f"no row (or an incomplete row) for {describe_expect(e)}")
            return checked, problems
        # A run with no such output, or values output that is not JSON, writes one row.
        if row["output"] == src and row["observed"] == "missing":
            judge(row, src, describe_expect(e), False)
            i += 1
            continue
        if e["match"] == "values" and row["output"] == f"{src} (values)" \
                and row["observed"] == "not JSON":
            judge(row, f"{src} (values)", describe_expect(e), False)
            i += 1
            continue
        if e["match"] == "sha256":
            judge(row, f"{src} (SHA-256)", e["value"], row["observed"] == e["value"])
            i += 1
        elif e["match"] == "text":
            got = row["observed"]
            ok = isinstance(got, str) and \
                normal_text(got.encode("utf-8")) == normal_text(e["value"].encode("utf-8"))
            judge(row, f"{src} (text)", e["value"], ok)
            i += 1
        else:
            for v in e["values"]:
                row = take(i)
                if row is None:
                    problems.append(f"no row (or an incomplete row) for {src} {v['key']}")
                    return checked, problems
                judge(row, f"{src} {v['key']}", value_rule(v), value_ok(v, row["observed"]))
                i += 1
    if i < len(rows):
        problems.append(f"{len(rows) - i} row(s) that the drop does not declare")
    return checked, problems


def recheck_report(folder, data):
    """Everything in a report's own contents that contradicts a "reproduced" verdict, worked
    out from the drop's declared expectations. Returns (checked rows, problems)."""
    folder = Path(folder)
    problems = []

    def stop(message):
        raise SystemExit(message)

    try:
        box = json.loads((folder / "box.json").read_text(encoding="utf-8"))
        block = read_reproduce(folder, box, stop)
    except (OSError, ValueError, SystemExit) as err:
        return [], [f"the drop's reproduce block cannot be read ({err})"]
    if block is None:
        return [], ["the drop declares no reproduce command"]
    if data.get("command") != block["command"]:
        problems.append("the report names another command than the drop declares")
    if "exit_code" not in data:
        problems.append("the report records no exit code")
    elif data["exit_code"] != 0 or isinstance(data["exit_code"], bool):
        problems.append(f"the command exited with status {data['exit_code']!r}")
    checked, found = recheck_expectations(block, data.get("expectations"))
    problems += found
    if data.get("differences"):
        problems.append("the report lists differences from the expected outputs")
    if not problems and not all(r[3] for r in checked):
        problems.append("an output differs from what the drop expects")
    runner = data.get("runner")
    if isinstance(runner, dict) and "verdicts" in runner:
        verdicts = runner.get("verdicts")
        if not isinstance(verdicts, list) or not verdicts or \
                any(v != "reproduced" for v in verdicts):
            problems.append(f"not every rerun reproduced ({verdicts!r})")
    return checked, problems


def number_forms(value):
    """Ways a printed number can be written literally in a file."""
    if isinstance(value, int):
        forms = {str(value)}
        if abs(value) >= 10000:
            forms |= {f"{value:_}", f"{value:,}"}
    else:
        forms = {repr(value)}
    return [f for f in forms if sum(c.isdigit() for c in f) >= LITERAL_MIN_DIGITS]


def stored_values(folder, literals, hashes):
    """Best-effort scan: declared outputs whose values already sit, literally, in the box's
    own files (not the generated ones). Returns [{"output", "value", "files"}]. A hit does
    not prove a box is faked and no hit does not prove it is honest: a value can be
    encoded or fetched. It tells a reader where to look."""
    found = {}
    for rel in all_files(Path(folder)):
        if rel in GENERATED:
            continue
        path = Path(folder) / rel
        try:
            if path.stat().st_size > LITERAL_MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(data).hexdigest()
        for label, value in hashes:
            if digest == value:
                found.setdefault((label, f"SHA-256 {value}"), []).append(rel)
        text = data.decode("utf-8", errors="ignore")
        for label, literal in literals:
            if isinstance(literal, str):
                hit = literal in text
                shown = literal
            else:
                forms = number_forms(literal)
                hit = any(re.search(r"(?<![\d.])" + re.escape(f) + r"(?![\d])", text)
                          for f in forms)
                shown = str(literal)
            if hit:
                found.setdefault((label, shown), []).append(rel)
    return [{"output": label, "value": value, "files": files}
            for (label, value), files in found.items()]


def network_wrapper():
    """Prefix that runs a command with no network: a new Linux network namespace with only
    a loopback device that is down. None where that is not available."""
    exe = shutil.which("unshare")
    if not sys.platform.startswith("linux") or exe is None:
        return None
    # With its own process namespace too, anything the command leaves running is killed
    # when the command ends, so it cannot touch the report afterwards.
    for prefix in ([exe, "--map-root-user", "--net", "--pid", "--fork", "--"],
                   [exe, "--map-root-user", "--net", "--"]):
        try:
            probe = subprocess.run(prefix + [sys.executable, "-c", ""],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return prefix
    return None


def reproduce(folder, args):
    """Run a drop's reproduce command on a temporary copy and compare. Exit status:
    0 reproduced, 1 did not reproduce (or the drop does not match its manifests),
    2 not attempted or inconclusive."""
    folder = Path(folder)
    if not (folder / "box.json").is_file():
        print(f"No box.json in {folder}: nothing to reproduce.")
        return 2

    def stop(message):
        raise SystemExit(message)

    box = json.loads((folder / "box.json").read_text(encoding="utf-8"))
    try:
        block = read_reproduce(folder, box, stop)
    except SystemExit as e:
        print(f"{e} Nothing was run.")
        return 2
    if block is None:
        print("This box declares no reproduce command. Nothing was run.")
        return 2
    report = args.report
    if report is not None:
        r = report.resolve()
        if r == folder.resolve() or folder.resolve() in r.parents:
            print("--report must be outside the drop, so the drop still matches its "
                  "manifests. Nothing was run.")
            return 2

    print("Checking the drop against its manifests first.")
    status = check(folder)
    if status != 0:
        print("The drop does not match its manifests, so reproducing it would prove nothing. "
              "Nothing was run.")
        return status

    timeout = args.timeout or block.get("timeout_seconds", DEFAULT_TIMEOUT)
    command = block["command"]
    prefix = []
    if args.no_network:
        prefix = network_wrapper()
        if prefix is None:
            print("--no-network needs Linux with unshare and unprivileged user namespaces, "
                  "and they are not available here. Nothing was run.")
            return 2
    print("")
    print(f"Reproduce: {block.get('name', box.get('box', ''))}")
    print(f"  Command:  {command_text(command)}")
    print(f"  Runs in:  a temporary copy of {folder}")
    print(f"  Timeout:  {timeout:g} seconds")
    print(f"  Network:  {'none' if prefix else 'not restricted'}")
    if block.get("requires"):
        print(f"  Requires: {block['requires']}")
    for e in block["expect"]:
        print(f"  Expects:  {describe_expect(e)}")
    print("This runs the box's own code with your permissions. It is not a sandbox: the code "
          "can read and write your files and use the network. Run boxes from people you do "
          "not know in a container or virtual machine.")
    if not args.yes:
        try:
            answer = input("Run it? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Not run.")
            return 2

    base = Path(tempfile.mkdtemp(prefix="research-box-run-"))
    work = base / "box"
    shutil.copytree(folder, work)
    (base / "home").mkdir()
    # A declared output file that is already in the drop would pass even if the command
    # never wrote it, so remove it from the run copy first (unless the command reads it).
    needed = set(reproduce_files(folder, block))
    cleared = []
    for e in block["expect"]:
        src = e["source"]
        if src != "stdout" and src not in needed and (work / src).is_file():
            (work / src).unlink()
            cleared.append(src)
    env = {"PATH": os.environ.get("PATH", ""), "LANG": os.environ.get("LANG", "C.UTF-8"),
           "HOME": str(base / "home")}
    for name in ("SYSTEMROOT", "SystemRoot"):  # Windows cannot start Python without it
        if name in os.environ:
            env[name] = os.environ[name]

    started = datetime.now(timezone.utc)
    result = {"box": box.get("box", ""), "dropped": box.get("dropped", ""),
              "drop_id": "sha256:" + sha256(folder / "tagmanifest-sha256.txt"),
              "command": command, "platform": platform.platform(),
              "python": platform.python_version(),
              "tool": f"research-box {VERSION}",
              "network": "none (Linux network namespace)" if prefix else "not restricted",
              "environment": sorted(env),
              "cleared_outputs": cleared,
              "started": started.isoformat(timespec="seconds")}
    rows, verdict, code, exit_code = [], "inconclusive", 2, None
    differences = []
    try:
        proc = subprocess.run(prefix + command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        print(f"The command did not finish within {timeout:g} seconds. Inconclusive.")
    except OSError as err:
        print(f"The command could not start ({err.strerror or err}). Inconclusive: check "
              f"that this machine has what the box requires.")
    else:
        exit_code = proc.returncode
        literals, hashes = [], []
        for e in block["expect"]:
            if e["source"] == "stdout":
                raw = proc.stdout
            else:
                path = work / e["source"]
                raw = path.read_bytes() if path.is_file() else None
            got = compare(e, raw)
            rows += got
            differences += [(r, explain(e, r, raw)) for r in got if not r[3]]
            if e["match"] == "sha256":
                hashes.append((got[0][0], e["value"]))
            elif e["match"] == "text" and len(e["value"].strip()) >= LITERAL_MIN_TEXT:
                literals.append((got[0][0], e["value"].strip()))
            else:
                literals += [(r[0], r[2]) for r in got if r[3] and not isinstance(r[2], bool)
                             and isinstance(r[2], (int, float))]
        stored = stored_values(folder, literals, hashes)
        result["stored_values"] = stored
        if stored:
            print("Note: these output values already appear in the box's own files, so the "
                  "run may print them rather than compute them. Read the code to check:")
            for s in stored:
                print(f"  {s['output']} = {s['value']}: in {', '.join(s['files'])}")
        if exit_code != 0:
            tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-5:]
            print(f"The command exited with status {exit_code}." +
                  ("\n  " + "\n  ".join(tail) if tail else ""))
        ok = exit_code == 0 and all(r[3] for r in rows)
        verdict, code = ("reproduced", 0) if ok else ("did not reproduce", 1)
    explained = dict((id(r), d) for r, d in differences)
    for row in rows:
        label, expected, observed, matched = row
        print(f"  {'match ' if matched else 'DIFFERS'}  {label}: expected {expected}; "
              f"observed {observed}")
        if id(row) in explained:
            print(f"           {explained[id(row)]['summary']}")
    print(f"Result: {verdict}. The run's files are in {work}.")

    if report is not None:
        result.update(ended=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      exit_code=exit_code, run_folder="<tmp>", verdict=verdict,
                      claim=REPORT_CLAIM,
                      expectations=[{"output": label, "expected": expected,
                                     "observed": observed, "matched": matched}
                                    for label, expected, observed, matched in rows])
        if verdict == "did not reproduce" and differences:
            # Added in 0.6.0, only to reports that did not reproduce: which output and key
            # missed, expected against observed, and by how much against the tolerance.
            result["differences"] = [d for _, d in differences]
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8", newline="\n")
        print(f"Report written to {report}.")
    return code


# ---------------------------------------------------------------------------------------
# Signed reproduction reports: anyone who ran --reproduce can sign the --report file with
# an SSH key (ssh-keygen -Y sign -n research-box-report). --verify-report checks, without
# running anything, that the signature verifies against an allowed-signers file, that the
# report is about this exact drop, that the drop still matches its manifests, and that the
# report says the result reproduced, and that the report's own observed values, compared
# again with the drop's declared expectations, support that verdict.

REPORT_NAMESPACE = "research-box-report"
RERUN_WORKFLOW = ".github/workflows/rerun.yml"
LOG_NAMESPACE = "research-box-log"


# A transparency log (RFC 9162 Merkle tree) records each signed report once, so a report
# cannot later be back-dated or swapped. A log proof is a JSON file: {"log": origin,
# "leaf_index", "tree_size", "inclusion": [hex hashes], "checkpoint": text,
# "checkpoint_signature": SSH signature text}. The checkpoint is a C2SP tlog checkpoint
# (origin, tree size and base64 root hash, one per line), signed with ssh-keygen in the
# research-box-log namespace. The leaf is report_leaf() of the report and signature bytes.

def leaf_hash(data):
    return hashlib.sha256(b"\x00" + data).digest()


def node_hash(left, right):
    return hashlib.sha256(b"\x01" + left + right).digest()


def report_leaf(report_bytes, signature_bytes):
    return ("research-box-report/v1\n" + hashlib.sha256(report_bytes).hexdigest() + "\n"
            + hashlib.sha256(signature_bytes).hexdigest() + "\n").encode()


def root_from_inclusion(index, size, leaf, path):
    """The tree root implied by an inclusion proof (RFC 9162, 2.1.3.2), or None."""
    if not 0 <= index < size:
        return None
    fn, sn, r = index, size - 1, leaf
    for p in path:
        if sn == 0:
            return None
        if fn & 1 or fn == sn:
            r = node_hash(p, r)
            while not fn & 1 and fn != 0:
                fn >>= 1
                sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    return r if sn == 0 else None


def check_signature(data, signature_text, signers, namespace):
    """Verify an SSH signature (given as text) over data. Returns (status, identity or
    reason): status 0 good, 1 bad, 2 could not check."""
    with tempfile.TemporaryDirectory() as d:
        sig = Path(d) / "sig"
        sig.write_text(signature_text, encoding="utf-8", newline="\n")
        found = ssh_keygen(["-Y", "find-principals", "-f", str(signers), "-s", str(sig)], data)
        if found is None:
            return 2, "ssh-keygen (OpenSSH 8.2 or later) was not found"
        names = [n for n in found[1].splitlines() if n.strip()] if found[0] == 0 else []
        if not names:
            return 1, "no key in the signers file made this signature"
        result = ssh_keygen(["-Y", "verify", "-f", str(signers), "-I", names[0],
                             "-n", namespace, "-s", str(sig)], data)
        if result is None:
            return 2, "ssh-keygen (OpenSSH 8.2 or later) was not found"
        return (0, names[0]) if result[0] == 0 else (1, f"bad signature for {names[0]}")


def check_log_proof(proof_path, signers, report_bytes, signature_bytes):
    """Returns (status, message): 0 included, 1 does not match, 2 could not check."""
    try:
        proof = json.loads(Path(proof_path).read_text(encoding="utf-8"))
        index, size = int(proof["leaf_index"]), int(proof["tree_size"])
        path = [bytes.fromhex(h) for h in proof["inclusion"]]
        checkpoint, cp_sig = proof["checkpoint"], proof["checkpoint_signature"]
        origin = proof["log"]
    except (OSError, ValueError, KeyError, TypeError) as err:
        return 2, f"could not read the log proof {proof_path} ({err})"
    lines = checkpoint.split("\n")
    if len(lines) < 4 or lines[0] != origin or lines[1] != str(size):
        return 1, "the checkpoint does not name this log and tree size"
    try:
        root = base64.b64decode(lines[2], validate=True)
    except ValueError:
        return 1, "the checkpoint's root hash is not base64"
    status, who = check_signature(checkpoint.encode(), cp_sig, signers, LOG_NAMESPACE)
    if status != 0:
        return status, f"checkpoint signature: {who}"
    leaf = leaf_hash(report_leaf(report_bytes, signature_bytes))
    if root_from_inclusion(index, size, leaf, path) != root:
        return 1, "the report and signature are not in the log at that position"
    return 0, (f"entry {index} of {size} in the log {origin}, checkpoint signed by {who}")


def ssh_keygen(arguments, data):
    """Run the local ssh-keygen with data on stdin. Returns (status, output) or None when
    ssh-keygen is not installed."""
    exe = shutil.which("ssh-keygen")
    if exe is None:
        return None
    proc = subprocess.run([exe, *arguments], input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, shell=False)
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace").strip()


def verify_report(folder, args):
    """Check a signed reproduction report against a drop. Exit status:
    0 a valid signature from an allowed signer on a report that this exact drop reproduced,
    and the drop still matches its manifests; 1 something does not match (signature, drop,
    files or verdict); 2 the check could not be made (no manifests, unreadable report,
    no signature file or no ssh-keygen)."""
    folder = Path(folder)
    report = args.verify_report
    signature = args.signature or Path(str(report) + ".sig")
    signers = args.allowed_signers

    try:
        raw = report.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as err:
        print(f"Could not read the report {report} ({err}). Nothing was verified.")
        return 2
    if not isinstance(data, dict) or not all(k in data for k in ("drop_id", "verdict")):
        print(f"{report} is not a research-box reproduction report (no drop_id or verdict). "
              "Nothing was verified.")
        return 2
    attested = getattr(args, "attested_by", None)
    if attested:
        needed = [(args.bundle, "attestation bundle")] if args.bundle else []
    else:
        needed = [(signature, "signature"), (signers, "allowed-signers file")]
    if args.log_proof is not None:
        needed += [(args.log_proof, "log proof"), (args.log_signers, "log signers file")]
    for path, what in needed:
        if not path.is_file():
            print(f"No {what} at {path}. Nothing was verified.")
            return 2

    print("Checking the drop against its manifests first.")
    status = check(folder)
    if status != 0:
        print("The drop does not match its manifests, so the report cannot be about these "
              "files. Not verified.")
        return status

    if attested:
        status, identity = check_attestation(report, attested, args.bundle,
                                              getattr(args, "signer_workflow", None))
        if status != 0:
            return status
        logged = "an entry in the Sigstore public transparency log"
        return finish_verify(folder, data, raw, identity, logged, args, signature=None)

    sig = str(signature)
    identity = args.signer
    if identity is None:
        found = ssh_keygen(["-Y", "find-principals", "-f", str(signers), "-s", sig], raw)
        if found is None:
            print("ssh-keygen (OpenSSH 8.2 or later) is needed to check signatures and was "
                  "not found. Nothing was verified.")
            return 2
        code, out = found
        principals = [p for p in out.splitlines() if p.strip()] if code == 0 else []
        if not principals:
            print("The signature was not made by any key in the allowed-signers file. "
                  "Not verified.")
            return 1
        identity = principals[0]
    result = ssh_keygen(["-Y", "verify", "-f", str(signers), "-I", identity,
                         "-n", REPORT_NAMESPACE, "-s", sig], raw)
    if result is None:
        print("ssh-keygen (OpenSSH 8.2 or later) is needed to check signatures and was not "
              "found. Nothing was verified.")
        return 2
    if result[0] != 0:
        print(f"The signature does not verify for {identity} with namespace "
              f"{REPORT_NAMESPACE}: the report was changed after signing, or another key "
              "signed it. Not verified.")
        return 1
    print(f"Signature: good, by {identity}.")
    return finish_verify(folder, data, raw, identity, None, args, signature)


def check_attestation(report, repo, bundle, signer_workflow=None):
    """Check with the GitHub CLI that the rerun workflow of REPO (or SIGNER_WORKFLOW, for a
    rerun run in someone's own repository) attested to this exact report file. Returns
    (status, identity)."""
    workflow = signer_workflow or f"{repo}/{RERUN_WORKFLOW}"
    exe = shutil.which("gh")
    if exe is None:
        print("The GitHub CLI (gh) is needed to check a GitHub attestation and was not "
              "found. Nothing was verified.")
        return 2, None
    command = [exe, "attestation", "verify", str(report), "--repo", repo,
               "--signer-workflow", workflow]
    if bundle:
        command += ["--bundle", str(bundle)]
    try:
        proc = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as err:
        print(f"gh attestation verify could not run ({err}). Nothing was verified.")
        return 2, None
    if proc.returncode != 0:
        tail = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()[-3:]
        print(f"No valid attestation by {workflow} for this exact report file. Not verified."
              + ("\n  " + "\n  ".join(tail) if tail else ""))
        return 1, None
    identity = f"the GitHub Actions workflow {workflow}"
    print(f"Attestation: good, by {identity}.")
    return 0, identity


def finish_verify(folder, data, raw, identity, logged, args, signature):
    drop_id = "sha256:" + sha256(folder / "tagmanifest-sha256.txt")
    if data["drop_id"] != drop_id:
        print(f"The report is about another drop ({data['drop_id']}); this drop is "
              f"{drop_id}. Not verified.")
        return 1
    print(f"Drop: the report names this drop ({drop_id}).")

    if logged is not None:
        print(f"Log: {logged}.")
    elif args.log_proof is not None:
        status, message = check_log_proof(args.log_proof, args.log_signers, raw,
                                          signature.read_bytes())
        if status != 0:
            print(f"Log: {message}. Not verified.")
            return status
        logged = message
        print(f"Log: {message}.")
    else:
        print("Log: no log proof given, so nothing shows when this report was first "
              "published; its times are the signer's word.")
    if data.get("network"):
        print(f"Network during the run: {data['network']}.")
    for s in data.get("stored_values") or []:
        if isinstance(s, dict):
            print(f"Note: {s.get('output')} = {s.get('value')} already appears in "
                  f"{', '.join(map(str, s.get('files') or []))}; check that it is computed.")

    # The signature vouches for who wrote the report, not that it is consistent: work the
    # comparison out again from the drop's own expectations and the observed values.
    checked, problems = recheck_report(folder, data)
    for label, expected, observed, matched in checked:
        print(f"  {'match ' if matched else 'DIFFERS'}  {label}: expected {expected}; "
              f"observed {observed}")
    when = data.get("started", "an unrecorded time")
    where = data.get("platform", "an unrecorded platform")
    if data["verdict"] != "reproduced":
        print(f"Result: the signed report says this drop did not reproduce "
              f"({data['verdict']}), run {when} on {where}.")
        return 1
    if problems:
        print("The report says reproduced, but its own contents say otherwise:")
        for p in problems:
            print(f"  - {p}")
        print("Result: not verified. The comparison, worked out again from the drop's "
              "expectations and the report's observed values, does not support the verdict.")
        return 1
    print(f"Result: verified. {identity} signed a report that this drop reproduced, run "
          f"{when} on {where}" + (f", logged as {logged}" if logged else "") + ". A "
          "signature shows who vouches for the run, not that the run was honest or the "
          "result is correct.")
    if data.get("claim"):
        print(f"What the report claims: {data['claim']}")
    return 0


# ---------------------------------------------------------------------------------------
# Portable identifiers and signed provenance. A drop has two ids, both SHA-256 digests that
# anyone can work out again from the files alone, on any machine:
#   content id  the SHA-256 of the manifest's lines ("<hash>  <path>"), sorted by path: the
#               snapshot files and nothing else, so it survives a change of wording in
#               BOX.md or a different item order;
#   drop id     the SHA-256 of tagmanifest-sha256.txt: this exact drop with its description
#               (the id a reproduction report names).
# A box may also name its author's identifiers (ORCID, ISNI or others, never minted here)
# and the drop it replaces. A provenance record states all of these, and whoever vouches
# for them signs it with an SSH key (Ed25519), as with reports.

PROVENANCE_NAMESPACE = "research-box-provenance"
PROVENANCE_TYPE = "research-box-provenance/v1"
PROVENANCE_CLAIM = ("The signer vouches that these files, with this description, are this "
                    "box, by this author, replacing the drop named as previous. The ids are "
                    "digests of the files; the author identifiers were not looked up in any "
                    "registry.")
ID_PATTERN = r"sha256:[0-9a-f]{64}"
CHECKED_SCHEMES = {"orcid": "ORCID", "isni": "ISNI"}


def iso7064_ok(digits):
    """ISO 7064 MOD 11-2 check character, as ORCID and ISNI use it."""
    total = 0
    for c in digits[:-1]:
        total = (total + int(c)) * 2
    rest = (12 - total % 11) % 11
    return digits[-1] == ("X" if rest == 10 else str(rest))


def author_id_problem(entry):
    """Why an author_ids entry is not valid, or None."""
    if not isinstance(entry, dict) or not isinstance(entry.get("scheme"), str) \
            or not isinstance(entry.get("id"), str) or not entry["id"].strip() \
            or not entry["scheme"].strip():
        return "each entry needs a scheme and an id, e.g. {\"scheme\": \"orcid\", " \
               "\"id\": \"0000-0002-1825-0097\"}"
    unknown = [k for k in entry if k not in ("scheme", "id")]
    if unknown:
        return f"unknown field(s) {', '.join(unknown)}"
    scheme, value = entry["scheme"].lower(), entry["id"]
    if scheme == "orcid":
        if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", value) \
                or not iso7064_ok(value.replace("-", "")):
            return f"{value!r} is not an ORCID iD (16 digits in groups of four, last one a " \
                   f"check digit or X, without https://orcid.org/)"
    if scheme == "isni":
        digits = value.replace(" ", "")
        if not re.fullmatch(r"\d{15}[\dX]", digits) or not iso7064_ok(digits):
            return f"{value!r} is not an ISNI (16 digits, last one a check digit or X)"
    return None


def read_identity(box, refuse):
    """Check author_ids and previous. Returns (author_ids, previous)."""
    ids = box.get("author_ids", [])
    if not isinstance(ids, list):
        refuse("author_ids must be a list.")
    for i, entry in enumerate(ids, 1):
        why = author_id_problem(entry)
        if why:
            refuse(f"author_ids[{i}]: {why}.")
    previous = box.get("previous")
    if previous is not None and (not isinstance(previous, str)
                                 or not re.fullmatch(ID_PATTERN, previous)):
        refuse("previous must be the drop id of the drop this one replaces: "
               "sha256: and 64 lowercase hex digits.")
    return ids, previous


def author_id_text(entry):
    scheme = entry["scheme"].lower()
    return f"{CHECKED_SCHEMES.get(scheme, entry['scheme'])} {entry['id']}"


def author_id_uri(entry):
    """The identifier as a URI where the scheme has a usual one."""
    scheme = entry["scheme"].lower()
    if scheme == "orcid":
        return f"https://orcid.org/{entry['id']}"
    if scheme == "isni":
        return f"https://isni.org/isni/{entry['id'].replace(' ', '')}"
    return f"{entry['scheme']}:{entry['id']}"


def content_id(folder):
    """The SHA-256 of the manifest's lines sorted by path, as "sha256:<hex>"."""
    rows = []
    for line in (Path(folder) / "manifest-sha256.txt").read_text(encoding="utf-8").splitlines():
        if line.strip():
            h, rel = line.split("  ", 1)
            rows.append((rel, h))
    text = "".join(f"{h}  {rel}\n" for rel, h in sorted(rows))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def drop_identity(folder):
    """What a drop says about itself: ids, box name, dates, author and author ids."""
    folder = Path(folder)
    box = json.loads((folder / "box.json").read_text(encoding="utf-8"))
    files = sum(1 for line in (folder / "manifest-sha256.txt").read_text(
        encoding="utf-8").splitlines() if line.strip())
    return {"box": box.get("box", ""), "dropped": box.get("dropped", ""),
            "content_id": content_id(folder),
            "drop_id": "sha256:" + sha256(folder / "tagmanifest-sha256.txt"),
            "files": files, "author": box.get("author"),
            "author_ids": box.get("author_ids", []), "previous": box.get("previous")}


def show_id(folder, args):
    """Print a drop's identifiers and optionally write a provenance record. Exit status as
    --check: 0 printed, 1 the drop does not match its manifests, 2 no manifests."""
    folder = Path(folder)
    if args.provenance is not None:
        r = args.provenance.resolve()
        if r == folder.resolve() or folder.resolve() in r.parents:
            print("--provenance must be outside the drop, so the drop still matches its "
                  "manifests. Nothing was written.")
            return 2
    status = check(folder)
    if status != 0:
        print("The drop does not match its manifests, so its ids would not describe these "
              "files. Nothing was written.")
        return status
    def stop(message):
        raise SystemExit(message)

    try:
        ident = drop_identity(folder)
        read_identity(json.loads((folder / "box.json").read_text(encoding="utf-8")), stop)
    except (OSError, ValueError, SystemExit) as err:
        print(f"Could not read the drop's box.json ({err}). Nothing was written.")
        return 2
    print(f"Box:        {ident['box']} (dropped {ident['dropped'] or 'on an unrecorded date'})")
    print(f"Content id: {ident['content_id']}  (the {ident['files']} snapshot file(s))")
    print(f"Drop id:    {ident['drop_id']}  (this drop with its description)")
    if ident["author"]:
        print(f"Author:     {ident['author']}")
    for entry in ident["author_ids"]:
        print(f"            {author_id_text(entry)}")
    if ident["previous"]:
        print(f"Replaces:   {ident['previous']}")
    if args.provenance is not None:
        record = {"type": PROVENANCE_TYPE, **ident, "tool": f"research-box {VERSION}",
                  "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "claim": PROVENANCE_CLAIM}
        args.provenance.parent.mkdir(parents=True, exist_ok=True)
        args.provenance.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                                   encoding="utf-8", newline="\n")
        print(f"Provenance record written to {args.provenance}. Sign it with your SSH key:")
        print(f"  ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n {PROVENANCE_NAMESPACE} "
              f"{shlex.quote(str(args.provenance))}")
    return 0


def verify_provenance(folder, args):
    """Check a signed provenance record against a drop. Exit status: 0 verified, 1 the drop,
    signature or an identifier does not match, 2 the check could not be made."""
    folder = Path(folder)
    record_path = args.verify_provenance
    signature = args.signature or Path(str(record_path) + ".sig")
    try:
        raw = record_path.read_bytes()
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as err:
        print(f"Could not read the record {record_path} ({err}). Nothing was verified.")
        return 2
    if not isinstance(record, dict) or record.get("type") != PROVENANCE_TYPE:
        print(f"{record_path} is not a research-box provenance record (type "
              f"{PROVENANCE_TYPE}). Nothing was verified.")
        return 2
    for path, what in ((signature, "signature"), (args.allowed_signers, "allowed-signers file")):
        if not path.is_file():
            print(f"No {what} at {path}. Nothing was verified.")
            return 2
    print("Checking the drop against its manifests first.")
    status = check(folder)
    if status != 0:
        print("The drop does not match its manifests, so the record cannot be about these "
              "files. Not verified.")
        return status
    sig_text = signature.read_text(encoding="utf-8")
    if args.signer:
        with tempfile.TemporaryDirectory() as d:
            sig = Path(d) / "sig"
            sig.write_text(sig_text, encoding="utf-8", newline="\n")
            result = ssh_keygen(["-Y", "verify", "-f", str(args.allowed_signers), "-I",
                                 args.signer, "-n", PROVENANCE_NAMESPACE, "-s", str(sig)], raw)
        if result is None:
            status, who = 2, "ssh-keygen (OpenSSH 8.2 or later) was not found"
        else:
            status, who = (0, args.signer) if result[0] == 0 else \
                (1, f"no valid signature by {args.signer}")
    else:
        status, who = check_signature(raw, sig_text, args.allowed_signers, PROVENANCE_NAMESPACE)
    if status != 0:
        print(f"Signature: {who} (namespace {PROVENANCE_NAMESPACE}). "
              + ("Nothing was verified." if status == 2 else "Not verified."))
        return status
    print(f"Signature: good, by {who}.")
    try:
        ident = drop_identity(folder)
    except (OSError, ValueError) as err:
        print(f"Could not read the drop's box.json ({err}). Nothing was verified.")
        return 2
    wrong = [k for k in ("box", "dropped", "content_id", "drop_id", "files", "author",
                         "author_ids", "previous") if record.get(k) != ident[k]]
    for k in wrong:
        print(f"  {k}: the record says {record.get(k)!r}; this drop says {ident[k]!r}")
    if wrong:
        print("Result: not verified. The signed record describes another drop or another "
              "author.")
        return 1
    who_wrote = ident["author"] or "an unnamed author"
    if ident["author_ids"]:
        who_wrote += " (" + ", ".join(author_id_text(e) for e in ident["author_ids"]) + ")"
    print(f"Result: verified. {who} signed that content {ident['content_id']}, drop "
          f"{ident['drop_id']}, is the box {ident['box']} by {who_wrote}"
          + (f", replacing {ident['previous']}" if ident["previous"] else "") + ". A "
          "signature shows who vouches for these claims; the author identifiers were not "
          "looked up in any registry.")
    return 0


# ---------------------------------------------------------------------------------------
# Link rot. An item's live page can change or vanish after the drop. --pin-links fetches each
# web page an item links to and records its SHA-256 in box.json (live_sha256, live_pinned);
# --check-links fetches them again and reports which pinned pages changed or cannot be
# reached. These are the script's only network calls, and only these two options make them.

LINK_TIMEOUT = 30
LINK_MAX_BYTES = 50 << 20


def web_link(url):
    return isinstance(url, str) and re.match(r"https?://[^\s/]+", url, re.I) is not None


def fetch_digest(url, timeout=LINK_TIMEOUT):
    """Fetch a web page and return (sha256 hex, None), or (None, reason it failed)."""
    request = urllib.request.Request(
        url, headers={"User-Agent": f"research-box/{VERSION} (link check)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            h, size = hashlib.sha256(), 0
            for chunk in iter(lambda: response.read(1 << 16), b""):
                size += len(chunk)
                if size > LINK_MAX_BYTES:
                    return None, f"larger than {LINK_MAX_BYTES >> 20} MB"
                h.update(chunk)
            return h.hexdigest(), None
    except urllib.error.HTTPError as err:
        return None, f"HTTP {err.code}"
    except urllib.error.URLError as err:
        return None, str(err.reason)
    except (OSError, ValueError, http.client.HTTPException) as err:
        return None, str(err) or type(err).__name__


def pin_links(folder):
    """Fetch every item's live web page and record its SHA-256 in box.json. Exit status:
    0 every page pinned, 1 some page could not be fetched (the others are pinned),
    2 nothing to pin."""
    folder = Path(folder)
    path = folder / "box.json"
    if not path.is_file():
        print(f"No box.json in {folder}: nothing to pin.")
        return 2
    box = json.loads(path.read_text(encoding="utf-8"))
    today = datetime.now(timezone.utc).date().isoformat()
    pinned, failed = 0, 0
    for it in box.get("items", []):
        url = it.get("live")
        if not web_link(url):
            continue
        digest, why = fetch_digest(url)
        if digest is None:
            failed += 1
            print(f"  not pinned  {it['item']}: {url} ({why})")
            continue
        before = it.get("live_sha256")
        it["live_sha256"], it["live_pinned"] = digest, today
        pinned += 1
        change = "" if before is None else (" (unchanged)" if before == digest
                                              else " (changed since the last pin)")
        print(f"  pinned      {it['item']}: {url} sha256 {digest}{change}")
    if not pinned and not failed:
        print("No item links to a web page (http or https). Nothing was pinned.")
        return 2
    if pinned:
        path.write_text(json.dumps(box, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
        print(f"{pinned} page(s) pinned in {path}. Drop the box again so its manifests "
              "cover the pins.")
    if failed:
        print(f"{failed} page(s) could not be fetched and were not pinned.")
        return 1
    return 0


def check_links(folder):
    """Fetch every pinned live page again and compare. Exit status: 0 every pinned page is
    unchanged (and the drop, if it has manifests, matches them), 1 a pinned page changed or
    cannot be reached (or the drop does not match), 2 no page is pinned."""
    folder = Path(folder)
    path = folder / "box.json"
    if not path.is_file():
        print(f"No box.json in {folder}: nothing to check.")
        return 2
    drop_status = 0
    if (folder / "manifest-sha256.txt").is_file():
        print("Checking the drop against its manifests first.")
        drop_status = check(folder)
        if drop_status == 1:
            print("The drop does not match its manifests, so its pins may have been edited; "
                  "the pages are checked against the pins box.json holds now.")
    box = json.loads(path.read_text(encoding="utf-8"))
    changed, gone, same, unpinned = [], [], [], []
    for it in box.get("items", []):
        url = it.get("live")
        if not web_link(url):
            continue
        pin = it.get("live_sha256")
        if pin is None:
            unpinned.append(it["item"])
            print(f"  not pinned   {it['item']}: {url}")
            continue
        when = it.get("live_pinned", "an unrecorded date")
        digest, why = fetch_digest(url)
        if digest is None:
            gone.append(it["item"])
            print(f"  UNREACHABLE  {it['item']}: {url} ({why}); pinned {when}")
        elif digest != pin:
            changed.append(it["item"])
            print(f"  CHANGED      {it['item']}: {url} now sha256 {digest}; pinned {when} "
                  f"as {pin}")
        else:
            same.append(it["item"])
            print(f"  unchanged    {it['item']}: {url} (pinned {when})")
    if not (changed or gone or same):
        print("No live page is pinned. Pin them with --pin-links before the drop.")
        return 2
    print(f"Result: {len(same)} unchanged, {len(changed)} changed, {len(gone)} unreachable"
          + (f", {len(unpinned)} not pinned" if unpinned else "") + ". A page that changed may "
          "only show a new date or layout; compare it with the snapshot before relying on it.")
    return 1 if changed or gone or drop_status == 1 else 0

# ---------------------------------------------------------------------------------------
# Registry receipts. An ORCID iD, ISNI or DOI in a box is a claim about the world outside
# it. --lookup asks each claim's registry once and saves what it answered as a receipt:
# the claim, the registry and address asked, the time, the HTTP status, the response
# bytes in a file under receipts/ and their SHA-256, and the answer read from them.
# --check-receipts replays the receipts offline: each saved response must still match its
# digest and still give the recorded answer, and registries that disagree are shown.
# --lookup is the only option here that uses the network.

RECEIPTS_DIR = "receipts"
RECEIPTS_INDEX = "receipts/receipts.json"
RECEIPTS_TYPE = "research-box-receipts/v1"
RECEIPTS_CLAIM = ("Each receipt records what a registry answered when it was asked, with the "
                  "response saved byte for byte. Registries do not sign their answers, so a "
                  "receipt shows what whoever made the lookup saved, not that the registry "
                  "said it; asking the registry again shows whether its answer still holds.")
RECEIPT_KEYS = ["claim", "item", "registry", "url", "asked", "status", "response",
                "response_sha256", "answer"]
RECEIPT_MAX_BYTES = 5 << 20
# Per scheme: (registry name, address with {id}, Accept header). Every one is public and
# needs no account or key.
REGISTRIES = {
    "orcid": [("orcid-public-api", "https://pub.orcid.org/v3.0/{id}/person",
               "application/json")],
    "isni": [("isni-sru", "https://isni.oclc.org/sru/?query=pica.isn+%3D+%22{id}%22"
              "&operation=searchRetrieve&recordSchema=isni-b", "application/xml")],
    "doi": [("doi-handle", "https://doi.org/api/handles/{id}", "application/json"),
            ("doi-metadata", "https://doi.org/{id}", "application/vnd.citationstyles.csl+json")],
}
REGISTRY_URLS = {name: url for entries in REGISTRIES.values() for name, url, _ in entries}
SCHEME_LABELS = {"orcid": "ORCID", "isni": "ISNI", "doi": "DOI"}
DOI_LINK = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:)(10\.\d{4,9}/\S+)$", re.I)


def doi_of(link):
    """The DOI an item's live link names (https://doi.org/10..., doi:10...), or None."""
    if not isinstance(link, str):
        return None
    m = DOI_LINK.match(link.strip())
    return urllib.parse.unquote(m.group(1)) if m else None


def claim_key(scheme, value):
    """Claims compare by scheme and identifier, ignoring case and ISNI spacing."""
    value = value.replace(" ", "") if scheme == "isni" else value
    return scheme, value.upper()


def box_claims(box):
    """The registry claims a box makes: [(scheme, id, item name or None)]."""
    claims = []
    for entry in box.get("author_ids", []) or []:
        if isinstance(entry, dict) and isinstance(entry.get("scheme"), str) \
                and entry["scheme"].lower() in ("orcid", "isni") and isinstance(entry.get("id"), str):
            scheme = entry["scheme"].lower()
            value = entry["id"].replace(" ", "") if scheme == "isni" else entry["id"]
            claims.append((scheme, value, None))
    for it in box.get("items", []) or []:
        doi = doi_of(it.get("live"))
        if doi:
            claims.append(("doi", doi, it.get("item")))
    return claims


def registry_url(registry, value):
    return REGISTRY_URLS[registry].replace(
        "{id}", urllib.parse.quote(value, safe="/:;()._-"))


def registry_get(url, accept, timeout=LINK_TIMEOUT):
    """GET a registry address. Returns (HTTP status, response bytes, None), or
    (None, None, reason) when no answer came back."""
    request = urllib.request.Request(url, headers={
        "Accept": accept, "User-Agent": f"research-box/{VERSION} (registry receipt)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(RECEIPT_MAX_BYTES + 1)
            status = response.status
    except urllib.error.HTTPError as err:
        status, body = err.code, err.read(RECEIPT_MAX_BYTES + 1)
    except urllib.error.URLError as err:
        return None, None, str(err.reason)
    except (OSError, ValueError, http.client.HTTPException) as err:
        return None, None, str(err) or type(err).__name__
    if len(body) > RECEIPT_MAX_BYTES:
        return None, None, f"answer larger than {RECEIPT_MAX_BYTES >> 20} MB"
    return status, body, None


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def read_answer(registry, status, body):
    """What a saved response says, worked out from its bytes alone: {"found": True or
    False, ...details}, or None when the response is not a definite answer."""
    if status == 404:
        return {"found": False}
    if status != 200:
        return None
    try:
        if registry == "isni-sru":
            root = ET.fromstring(body)
            count = next((el.text for el in root.iter() if _local(el.tag) == "numberOfRecords"),
                         None)
            if count is None or not count.strip().isdigit():
                return None
            answer = {"found": int(count) > 0}
            if answer["found"]:
                surname = next((el.text for el in root.iter()
                                if _local(el.tag) == "surname" and el.text), None)
                forename = next((el.text for el in root.iter()
                                 if _local(el.tag) == "forename" and el.text), None)
                name = " ".join(p.strip() for p in (forename, surname) if p)
                if name:
                    answer["name"] = name
            return answer
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, ET.ParseError):
        return None
    if not isinstance(data, dict):
        return None
    if registry == "orcid-public-api":
        name = data.get("name") or {}
        def part(key):
            v = name.get(key) if isinstance(name, dict) else None
            return v.get("value") if isinstance(v, dict) and isinstance(v.get("value"), str) \
                else None
        full = part("credit-name") or " ".join(p for p in (part("given-names"),
                                                           part("family-name")) if p)
        return {"found": True, **({"name": full} if full else {})}
    if registry == "doi-handle":
        if data.get("responseCode") == 100:
            return {"found": False}
        if data.get("responseCode") != 1:
            return None
        url = next((v.get("data", {}).get("value") for v in data.get("values", [])
                    if isinstance(v, dict) and v.get("type") == "URL"
                    and isinstance(v.get("data"), dict)), None)
        return {"found": True, **({"url": url} if isinstance(url, str) else {})}
    if registry == "doi-metadata":
        answer = {"found": True}
        title = data.get("title")
        if isinstance(title, list):
            title = title[0] if title else None
        if isinstance(title, str) and title.strip():
            answer["title"] = title.strip()
        authors = []
        for a in data.get("author", []) if isinstance(data.get("author"), list) else []:
            if isinstance(a, dict):
                n = a.get("literal") or " ".join(p for p in (a.get("given"), a.get("family"))
                                                 if isinstance(p, str))
                if n:
                    authors.append(n)
        if authors:
            answer["authors"] = authors[:20]
        return answer
    return None


def answer_text(answer):
    if answer is None:
        return "no definite answer"
    if not answer.get("found"):
        return "not found"
    details = [f"name {answer['name']}" if answer.get("name") else "",
               f"title {answer['title']}" if answer.get("title") else "",
               f"points to {answer['url']}" if answer.get("url") else ""]
    return "found" + "".join(f", {d}" for d in details if d)


def read_receipts(folder):
    """The receipts index of a folder: (list of receipts, None), (None, None) when the
    folder has none, or (None, reason) when it cannot be read."""
    path = Path(folder) / RECEIPTS_INDEX
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as err:
        return None, f"{RECEIPTS_INDEX} cannot be read ({err})"
    if not isinstance(data, dict) or data.get("type") != RECEIPTS_TYPE \
            or not isinstance(data.get("receipts"), list):
        return None, f"{RECEIPTS_INDEX} is not a receipts index (type {RECEIPTS_TYPE})"
    for i, r in enumerate(data["receipts"], 1):
        why = receipt_problem(folder, r)
        if why:
            return None, f"{RECEIPTS_INDEX} receipt {i}: {why}"
    return data["receipts"], None


def receipt_problem(folder, r):
    """Why a receipt is malformed, or None. Says nothing about whether it still holds."""
    if not isinstance(r, dict):
        return "not an object"
    unknown = [k for k in r if k not in RECEIPT_KEYS]
    if unknown:
        return f"unknown field(s) {', '.join(unknown)}"
    claim = r.get("claim")
    if not isinstance(claim, dict) or claim.get("scheme") not in REGISTRIES \
            or not isinstance(claim.get("id"), str) or not claim["id"]:
        return "claim must be {\"scheme\": \"orcid\", \"isni\" or \"doi\", \"id\": ...}"
    if r.get("registry") not in [n for n, _, _ in REGISTRIES[claim["scheme"]]]:
        return f"registry {r.get('registry')!r} does not answer {claim['scheme']} claims"
    if not isinstance(r.get("status"), int) or not isinstance(r.get("asked"), str) \
            or not isinstance(r.get("url"), str):
        return "status, asked and url are required"
    rel = r.get("response")
    if not isinstance(rel, str) or not rel.startswith(RECEIPTS_DIR + "/") \
            or not inside(folder, rel):
        return "response must be a file under receipts/"
    if not isinstance(r.get("response_sha256"), str) \
            or not re.fullmatch(r"[0-9a-f]{64}", r["response_sha256"]):
        return "response_sha256 must be 64 lowercase hex digits"
    if "item" in r and not isinstance(r["item"], str):
        return "item must be the item's name"
    if not isinstance(r.get("answer"), dict):
        return "answer is required"
    return None


def receipts_text(receipts):
    return json.dumps({"type": RECEIPTS_TYPE, "claim": RECEIPTS_CLAIM, "receipts": receipts},
                      indent=2, ensure_ascii=False) + "\n"


def lookup_receipts(folder):
    """Ask each claim's registries and save receipts under receipts/. Exit status: 0 every
    claim found and the registries agree, 1 a claim was not found, registries disagree or a
    registry gave no answer (what did answer is saved), 2 nothing to look up."""
    folder = Path(folder)
    path = folder / "box.json"
    if not path.is_file():
        print(f"No box.json in {folder}: nothing to look up.")
        return 2
    box = json.loads(path.read_text(encoding="utf-8"))
    claims = box_claims(box)
    if not claims:
        print("The box names no ORCID iD or ISNI in author_ids and no DOI as an item's live "
              "link (https://doi.org/10...). Nothing was looked up.")
        return 2
    receipts, why = read_receipts(folder)
    if why:
        print(f"{why}. Nothing was looked up.")
        return 2
    receipts = receipts or []
    (folder / RECEIPTS_DIR).mkdir(exist_ok=True)
    unanswered, saved = 0, []
    for scheme, value, item in claims:
        for registry, _, accept in REGISTRIES[scheme]:
            url = registry_url(registry, value)
            asked = datetime.now(timezone.utc).isoformat(timespec="seconds")
            status, body, failure = registry_get(url, accept)
            label = f"{SCHEME_LABELS[scheme]} {value}"
            if status is None:
                unanswered += 1
                print(f"  no answer   {label} from {registry}: {failure}")
                continue
            answer = read_answer(registry, status, body)
            if answer is None:
                unanswered += 1
                print(f"  no answer   {label} from {registry}: HTTP {status}, not a definite "
                      "answer (not saved)")
                continue
            stamp = asked.replace("-", "").replace(":", "").replace("+0000", "Z")
            stem = re.sub(r"[^A-Za-z0-9.-]+", "_", f"{scheme}-{value}")[:80]
            ext = ".xml" if registry == "isni-sru" else ".json"
            rel = f"{RECEIPTS_DIR}/{stem}-{registry}-{stamp}{ext}"
            n = 2
            while (folder / rel).exists():
                rel = f"{RECEIPTS_DIR}/{stem}-{registry}-{stamp}-{n}{ext}"
                n += 1
            (folder / rel).write_bytes(body)
            receipt = {"claim": {"scheme": scheme, "id": value}}
            if item is not None:
                receipt["item"] = item
            receipt.update(registry=registry, url=url, asked=asked, status=status,
                           response=rel, response_sha256=hashlib.sha256(body).hexdigest(),
                           answer=answer)
            receipts.append(receipt)
            saved.append(receipt)
            print(f"  saved       {label} from {registry}: {answer_text(answer)} ({rel})")
    if saved:
        (folder / RECEIPTS_INDEX).write_text(receipts_text(receipts), encoding="utf-8",
                                             newline="\n")
        print(f"{len(saved)} receipt(s) saved in {folder / RECEIPTS_INDEX}. Drop the box again "
              "so its manifests cover them.")
    findings = receipt_findings(box, [r for r in receipts if r in saved])
    for line in findings["lines"]:
        print(line)
    if unanswered:
        print(f"{unanswered} lookup(s) got no answer and were not saved.")
    return 1 if unanswered or findings["problems"] else 0


def receipt_findings(box, receipts):
    """Compare the newest receipt from each registry for each claim the box makes.
    Returns {"lines": [...], "problems": n, "notes": n, "unchecked": [claims]}."""
    latest = {}
    for r in receipts:
        key = claim_key(r["claim"]["scheme"], r["claim"]["id"])
        prev = latest.setdefault(key, {}).get(r["registry"])
        if prev is None or r["asked"] >= prev["asked"]:
            latest[key][r["registry"]] = r
    lines, problems, notes, unchecked = [], 0, 0, []
    author = (box.get("author") or "").strip()
    for scheme, value, item in box_claims(box):
        key = claim_key(scheme, value)
        label = f"{SCHEME_LABELS[scheme]} {value}" + (f" ({item})" if item else "")
        answers = latest.get(key)
        if not answers:
            unchecked.append(label)
            lines.append(f"  not looked up  {label}")
            continue
        found = {reg: r["answer"].get("found") for reg, r in answers.items()}
        if len(set(found.values())) > 1:
            problems += 1
            lines.append(f"  DISAGREE       {label}: " + "; ".join(
                f"{reg} says {'found' if f else 'not found'} ({answers[reg]['asked']})"
                for reg, f in sorted(found.items())))
            continue
        if not any(found.values()):
            problems += 1
            lines.append(f"  NOT FOUND      {label}: " + "; ".join(
                f"{reg} ({r['asked']})" for reg, r in sorted(answers.items())))
            continue
        lines.append(f"  found          {label}: " + "; ".join(
            f"{reg} {answer_text(r['answer'])[len('found, '):] or 'found'} ({r['asked']})"
            for reg, r in sorted(answers.items())))
        names = {r["answer"]["name"] for r in answers.values() if r["answer"].get("name")}
        if scheme in ("orcid", "isni") and author and names and not any(
                " ".join(n.casefold().split()) == " ".join(author.casefold().split())
                for n in names):
            notes += 1
            lines.append(f"    note: the registry gives the name {', '.join(sorted(names))}; "
                         f"the box gives the author as {author}. Names can be written "
                         "differently; check that they are the same person.")
    return {"lines": lines, "problems": problems, "notes": notes, "unchecked": unchecked}


def replay_problem(folder, r):
    """Why a receipt no longer holds against its saved response, as (word, detail), or
    None when the saved bytes still give the recorded answer."""
    file = Path(folder) / r["response"]
    if not file.is_file():
        return "MISSING", r["response"]
    body = file.read_bytes()
    if hashlib.sha256(body).hexdigest() != r["response_sha256"]:
        return "CHANGED", f"{r['response']} no longer matches its SHA-256"
    if r["url"] != registry_url(r["registry"], r["claim"]["id"]):
        return "WRONG PLACE", f"asked {r['url']}, not the registry's address"
    again = read_answer(r["registry"], r["status"], body)
    if again != r["answer"]:
        return "MISREAD", (f"the saved response says {answer_text(again)}; the receipt says "
                           f"{answer_text(r['answer'])}")
    return None


def check_receipts(folder):
    """Replay a box's registry receipts offline. Exit status: 0 every receipt replays and
    every claim with receipts was found with registries agreeing (and the drop, if it has
    manifests, matches them); 1 a saved response changed or is missing, gives another
    answer than recorded, a claim was not found, registries disagree, or the drop does not
    match; 2 no receipts to check."""
    folder = Path(folder)
    path = folder / "box.json"
    if not path.is_file():
        print(f"No box.json in {folder}: nothing to check.")
        return 2
    drop_status = 0
    if (folder / "manifest-sha256.txt").is_file():
        print("Checking the drop against its manifests first.")
        drop_status = check(folder)
        if drop_status == 1:
            print("The drop does not match its manifests, so its receipts may have been "
                  "edited; they are replayed as the folder holds them now.")
    receipts, why = read_receipts(folder)
    if why:
        print(f"{why}. Not checked.")
        return 1
    if not receipts:
        print("The box has no registry receipts. Make them with --lookup before the drop.")
        return 2
    box = json.loads(path.read_text(encoding="utf-8"))
    claims = {claim_key(s, v) for s, v, _ in box_claims(box)}
    bad, good, stale = 0, [], 0
    for r in receipts:
        label = f"{SCHEME_LABELS[r['claim']['scheme']]} {r['claim']['id']} from {r['registry']}"
        why = replay_problem(folder, r)
        if why:
            bad += 1
            print(f"  {why[0]:<12} {label}: {why[1]}")
            continue
        if claim_key(r["claim"]["scheme"], r["claim"]["id"]) not in claims:
            stale += 1
        good.append(r)
    print(f"Replayed {len(receipts)} receipt(s): {len(good)} match their saved responses"
          + (f", {bad} do not" if bad else "")
          + (f", {stale} for claims the box no longer makes" if stale else "") + ".")
    findings = receipt_findings(box, good)
    for line in findings["lines"]:
        print(line)
    print("A receipt shows what was saved when the registry was asked; registries do not "
          "sign their answers. Run --lookup again to ask them now.")
    return 1 if bad or findings["problems"] or drop_status == 1 else 0


def parse(argv):
    p = argparse.ArgumentParser(
        prog="build_box.py",
        description="Build BOX.md, ro-crate-metadata.json and the checksum manifests for a "
                    "research box, or check a dropped box against its manifests.",
        epilog="The box.json format is in references/box-format.md. The script never deletes "
               "anything.")
    p.add_argument("folder", type=Path, help="the box folder (holds box.json, or a dropped box)")
    p.add_argument("--for", dest="audience", choices=list(LEVELS), default="private",
                   help="audience of the drop (default: private)")
    p.add_argument("--out", type=Path, metavar="NEW_FOLDER",
                   help="write a clean copy here; required when the drop holds anything back")
    p.add_argument("--strict", action="store_true",
                   help="refuse a shareable drop, writing nothing, if any watch word is found")
    p.add_argument("--allow-unscanned", action="store_true",
                   help="with --strict: accept files that could not be scanned for watch "
                        "words (PDFs, images), after checking them by eye")
    p.add_argument("--check", action="store_true",
                   help="compare a dropped box with its manifests; exit 0 match, 1 difference, "
                        "2 no manifest or no tag manifest (incomplete check)")
    p.add_argument("--reproduce", action="store_true",
                   help="run the drop's declared command on a temporary copy, after --check "
                        "passes and you confirm, and compare its output; exit 0 reproduced, "
                        "1 did not reproduce, 2 not run or inconclusive")
    p.add_argument("--yes", action="store_true",
                   help="with --reproduce: run without asking (for pipelines)")
    p.add_argument("--timeout", type=float, metavar="SECONDS",
                   help="with --reproduce: override the box's timeout")
    p.add_argument("--no-network", action="store_true",
                   help="with --reproduce: run the command with no network (Linux, in a new "
                        "network namespace); refuses to run where that is not available")
    p.add_argument("--report", type=Path, metavar="FILE",
                   help="with --reproduce: write the result as JSON here (outside the drop)")
    p.add_argument("--verify-report", type=Path, metavar="REPORT",
                   help="check a signed --report file against this drop without running "
                        "anything; exit 0 verified, 1 does not match, 2 could not check")
    p.add_argument("--allowed-signers", type=Path, metavar="FILE",
                   help="with --verify-report: OpenSSH allowed_signers file of trusted keys")
    p.add_argument("--signer", metavar="IDENTITY",
                   help="with --verify-report: the identity expected to have signed "
                        "(default: whichever allowed signer's key made the signature)")
    p.add_argument("--signature", type=Path, metavar="FILE",
                   help="with --verify-report: the signature file (default: REPORT.sig)")
    p.add_argument("--log-proof", type=Path, metavar="FILE",
                   help="with --verify-report: a transparency-log inclusion proof for the "
                        "report; it must verify")
    p.add_argument("--log-signers", type=Path, metavar="FILE",
                   help="with --log-proof: allowed_signers file of trusted log keys")
    p.add_argument("--attested-by", metavar="OWNER/REPO",
                   help="with --verify-report: instead of an SSH signature, check with the "
                        "GitHub CLI that this repository's rerun workflow attested to the "
                        "report (a GitHub Actions rerun)")
    p.add_argument("--signer-workflow", metavar="OWNER/REPO/.github/workflows/FILE",
                   help="with --attested-by: the workflow expected to have signed, for a "
                        "rerun run in your own repository (default: REPO's rerun.yml), "
                        "for example PaoloTCS/research-box/.github/workflows/rerun-own.yml")
    p.add_argument("--bundle", type=Path, metavar="FILE",
                   help="with --attested-by: the attestation bundle on disk (default: "
                        "fetch it from GitHub)")
    p.add_argument("--pin-links", action="store_true",
                   help="fetch every item's live web page and record its SHA-256 in "
                        "box.json (live_sha256); uses the network")
    p.add_argument("--check-links", action="store_true",
                   help="fetch every pinned live page again and report which changed or "
                        "cannot be reached; uses the network; exit 0 all unchanged, 1 changed "
                        "or unreachable, 2 none pinned")
    p.add_argument("--lookup", action="store_true",
                   help="ask the registries about each ORCID iD and ISNI in author_ids and "
                        "each DOI an item links to, and save their answers as receipts under "
                        "receipts/; uses the network; exit 0 all found, 1 not found, "
                        "disagreeing or unanswered, 2 nothing to look up")
    p.add_argument("--check-receipts", action="store_true",
                   help="replay the saved registry receipts offline: each saved response "
                        "must match its digest and give the recorded answer; exit 0 all "
                        "hold, 1 changed, not found or disagreeing, 2 no receipts")
    p.add_argument("--id", action="store_true",
                   help="print a dropped box's portable identifiers: its content id and "
                        "drop id (SHA-256 digests), author identifiers and the drop it "
                        "replaces")
    p.add_argument("--provenance", type=Path, metavar="FILE",
                   help="with --id: write them as a provenance record to sign with "
                        "ssh-keygen -Y sign -n research-box-provenance (outside the drop)")
    p.add_argument("--verify-provenance", type=Path, metavar="RECORD",
                   help="check a signed provenance record against this drop; exit 0 "
                        "verified, 1 does not match, 2 could not check")
    args = p.parse_args(argv[1:])
    alone = [args.pin_links, args.check_links, args.lookup, args.check_receipts]
    if any(alone):
        if sum(map(bool, alone)) > 1 or args.check or args.reproduce or args.id \
                or args.verify_report is not None or args.verify_provenance is not None \
                or args.out or args.strict or args.audience != "private":
            p.error("--pin-links, --check-links, --lookup and --check-receipts each run on "
                    "their own; use them without other commands, --for, --out or --strict")
    if args.provenance is not None and not args.id:
        p.error("--provenance goes with --id")
    if args.id and (args.check or args.reproduce or args.verify_report is not None
                    or args.verify_provenance is not None or args.out or args.strict
                    or args.audience != "private"):
        p.error("--id reads a dropped box on its own; use it without other commands, "
                "--for, --out or --strict")
    if args.verify_provenance is not None:
        if args.allowed_signers is None:
            p.error("--verify-provenance needs --allowed-signers FILE")
        if (args.check or args.reproduce or args.verify_report is not None or args.out
                or args.strict or args.log_proof is not None or args.log_signers is not None
                or args.attested_by is not None or args.audience != "private"):
            p.error("--verify-provenance checks a dropped box on its own; use it without "
                    "other commands, --for, --out, --strict, --log-proof or --attested-by")
    verify_only = (args.allowed_signers, args.signer, args.signature, args.log_proof,
                   args.log_signers)
    if args.verify_report is None and args.verify_provenance is None and \
            any(v is not None for v in verify_only):
        p.error("--allowed-signers, --signer, --signature, --log-proof and --log-signers "
                "go with --verify-report")
    if (args.log_proof is None) != (args.log_signers is None):
        p.error("--log-proof and --log-signers go together")
    if args.bundle is not None and args.attested_by is None:
        p.error("--bundle goes with --attested-by")
    if args.signer_workflow is not None and args.attested_by is None:
        p.error("--signer-workflow goes with --attested-by")
    if args.signer_workflow is not None and not re.fullmatch(
            r"[\w.-]+/[\w.-]+/\.github/workflows/[\w.-]+\.ya?ml", args.signer_workflow):
        p.error("--signer-workflow takes OWNER/REPO/.github/workflows/FILE.yml")
    if args.attested_by is not None and args.verify_report is None:
        p.error("--attested-by goes with --verify-report")
    if args.attested_by is not None and any(v is not None for v in verify_only):
        p.error("--attested-by replaces --allowed-signers, --signer, --signature, "
                "--log-proof and --log-signers; the attestation's log is Sigstore's")
    if args.attested_by is not None and not re.fullmatch(r"[\w.-]+/[\w.-]+",
                                                         args.attested_by):
        p.error("--attested-by takes OWNER/REPO, for example PaoloTCS/research-box")
    if args.verify_report is not None:
        if args.allowed_signers is None and args.attested_by is None:
            p.error("--verify-report needs --allowed-signers FILE or --attested-by "
                    "OWNER/REPO")
        if (args.reproduce or args.check or args.out or args.strict or args.yes
                or args.timeout is not None or args.report or args.no_network
                or args.audience != "private"):
            p.error("--verify-report checks a dropped box on its own; use it without "
                    "--check, --reproduce, --for, --out or --strict")
    if not args.reproduce and (args.yes or args.timeout is not None or args.report
                               or args.no_network):
        p.error("--yes, --timeout, --no-network and --report go with --reproduce")
    if args.reproduce and (args.check or args.out or args.strict or args.audience != "private"):
        p.error("--reproduce runs on a dropped box; use it without --check, --for, --out "
                "or --strict")
    if args.allow_unscanned and not args.strict:
        p.error("--allow-unscanned goes with --strict")
    if args.timeout is not None and args.timeout <= 0:
        p.error("--timeout must be a positive number of seconds")
    return args


def main(argv):
    args = parse(argv)
    folder = args.folder
    if args.check:
        sys.exit(check(folder))
    if args.reproduce:
        sys.exit(reproduce(folder, args))
    if args.verify_report is not None:
        sys.exit(verify_report(folder, args))
    if args.pin_links:
        sys.exit(pin_links(folder))
    if args.check_links:
        sys.exit(check_links(folder))
    if args.lookup:
        sys.exit(lookup_receipts(folder))
    if args.check_receipts:
        sys.exit(check_receipts(folder))
    if args.id:
        sys.exit(show_id(folder, args))
    if args.verify_provenance is not None:
        sys.exit(verify_provenance(folder, args))
    audience, out = args.audience, args.out
    if not (folder / "box.json").is_file():
        sys.exit(f"No box.json in {folder}.")
    box = json.loads((folder / "box.json").read_text(encoding="utf-8"))

    def refuse(message):
        sys.exit(message + " Nothing was written.")

    def cleared(level):
        return LEVELS[level] >= LEVELS[audience]

    # Every snapshot path must stay inside the box folder, so a box.json cannot pull in
    # (or, with --out, write) files elsewhere.
    bad = [rel for it in box["items"] for rel in it.get("snapshot", []) if not inside(folder, rel)]
    if bad:
        refuse("Snapshot paths must be relative paths inside the box folder; these are not: "
               + ", ".join(map(str, bad)) + ".")

    items = [dict(it, sharing=level_of(it.get("sharing"), box, f"item {it['item']!r}"))
             for it in box["items"]]
    author_ids, previous = read_identity(box, refuse)
    for it in items:
        if "live_sha256" in it and not (web_link(it.get("live")) and isinstance(
                it["live_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", it["live_sha256"])):
            refuse(f"item {it['item']!r}: live_sha256 must be 64 lowercase hex digits, on an "
                   "item whose live link is a web page.")

    # One file, one level: a file listed under items with different sharing levels is
    # ambiguous, so the drop is refused until the box says which level it has.
    levels_of = {}
    for it in items:
        for rel in it.get("snapshot", []):
            key = (folder / rel).resolve()
            levels_of.setdefault(key, {})[it["sharing"]] = rel
    conflicts = [", ".join(sorted(set(v.values()))) for v in levels_of.values() if len(v) > 1]
    if conflicts:
        refuse("These files are listed under items with different sharing levels: "
               + "; ".join(conflicts) + ". Give each file one level.")

    # Box details (title, description, topics, state) carry the box's default sharing.
    # For a wider audience, use the "public" block if there is one; otherwise show only
    # the box name.
    box_level = level_of(box.get("default_sharing"), box, "box")
    use_box = cleared(box_level)
    details = box if use_box else dict(box.get("public", {}))
    title = details.get("title", box["box"])
    description = details.get("description", "")
    topics = details.get("topics", [])
    state_entries, state_held = [], 0  # kept as written, so labels survive re-export
    for s in details.get("state", []):
        if isinstance(s, str) or cleared(level_of(s.get("sharing"), box, "state line")):
            state_entries.append(s)
        else:
            state_held += 1
    state = [s if isinstance(s, str) else s["text"] for s in state_entries]

    kept = [it for it in items if cleared(it["sharing"])]
    left_out = [it for it in items if not cleared(it["sharing"])]

    # Reproduce block: checked in every drop. It has its own sharing level (the box's
    # default unless given). When it is cleared, every box file it runs or reads must be a
    # snapshot this drop keeps, or the drop is refused: a command that cannot run in the
    # drop it ships with is worse than none.
    repro = read_reproduce(folder, box, refuse)
    repro_kept = False
    if repro is not None:
        repro_level = level_of(repro.get("sharing"), box, "reproduce")
        repro_kept = cleared(repro_level)
        if repro_kept:
            kept_files = {rel for it in kept for rel in it.get("snapshot", [])}
            needed = reproduce_files(folder, repro)
            not_kept = [rel for rel in needed if rel not in kept_files]
            if not_kept:
                refuse("The reproduce command needs " + ", ".join(not_kept) + ", which this "
                       f"{audience} drop does not hold (not cleared for {LABELS[audience]} "
                       "readers, or not a snapshot of any item). Clear the file, or give the "
                       "reproduce block a narrower sharing level.")
            repro = dict(repro, sharing=repro_level)

    # Registry receipts (receipts/receipts.json, written by --lookup) travel with the claims
    # they are about: author identifiers always, a DOI only with its item. Each must still
    # give its recorded answer from its saved response, or the drop is refused.
    receipts, why = read_receipts(folder)
    if why:
        refuse(why + ".")
    receipts = receipts or []
    receipts_kept, receipts_held, receipts_stale = [], 0, 0
    if receipts:
        clash = [rel for it in items for rel in it.get("snapshot", [])
                 if PurePath(rel).parts[:1] == (RECEIPTS_DIR,)]
        if clash:
            refuse(f"{RECEIPTS_DIR}/ holds this box's registry receipts; no item may list a "
                   f"snapshot there: {', '.join(clash)}.")
        for i, r in enumerate(receipts, 1):
            problem = replay_problem(folder, r)
            if problem:
                refuse(f"{RECEIPTS_INDEX} receipt {i} ({r['claim']['id']} from "
                       f"{r['registry']}): {problem[1]}. Run --lookup again.")
        kept_names = {it["item"] for it in kept}
        making = {(claim_key(sc, v), it) for sc, v, it in box_claims(box)}
        for r in receipts:
            key = (claim_key(r["claim"]["scheme"], r["claim"]["id"]), r.get("item"))
            if key not in making:
                receipts_stale += 1
            elif r.get("item") is None or r["item"] in kept_names:
                receipts_kept.append(r)
            else:
                receipts_held += 1

    # Fields outside the format are never copied into a drop for other readers.
    extra_box = [k for k in box if k not in BOX_KEYS]
    extra_items = sorted({k for it in box["items"] for k in it if k not in ITEM_KEYS})
    extra_fields = extra_box + [f"items.{k}" for k in extra_items]

    # A drop that holds anything back must be a clean copy: written in place, the folder
    # would still hold the full box.json and the held-back files beside what is shared.
    held = []
    if left_out:
        held.append(f"{len(left_out)} item(s)")
    if state_held:
        held.append(f"{state_held} state line(s)")
    if repro is not None and not repro_kept:
        held.append("the reproduce command")
    if not use_box:
        held.append("the box's own details")
    if extra_fields and audience != "private":
        held.append(f"field(s) outside the format ({', '.join(extra_fields)})")
    if receipts_held:
        held.append(f"{receipts_held} registry receipt(s)")
    if held and not out:
        refuse(f"This {audience} drop holds back {', '.join(held)}. Write it as a clean copy: "
               f"add --out <new folder>.")

    # Every snapshot a kept item declares must exist. A file that is deliberately not
    # copied belongs in the item's notes, with snapshot left empty.
    missing = [rel for it in kept for rel in it.get("snapshot", []) if not (folder / rel).is_file()]
    if missing:
        refuse("Snapshot files listed but not found: " + ", ".join(missing)
               + ". Export them, or move them to the item's notes if they are not copied.")
    if out and out.exists() and any(out.iterdir()):
        refuse(f"--out folder {out} is not empty.")

    sums = []
    for it in kept:
        for rel in it.get("snapshot", []):
            if rel not in [r for _, r, _ in sums]:
                sums.append((sha256(folder / rel), rel, (folder / rel).stat().st_size))

    written = {}  # files the drop writes from memory instead of copying
    if receipts:
        if out:
            index = receipts_text(receipts_kept).encode("utf-8")
            written[RECEIPTS_INDEX] = index
            sums.append((hashlib.sha256(index).hexdigest(), RECEIPTS_INDEX, len(index)))
            in_drop = receipts_kept
        else:
            sums.append((sha256(folder / RECEIPTS_INDEX), RECEIPTS_INDEX,
                         (folder / RECEIPTS_INDEX).stat().st_size))
            in_drop = receipts
        for r in in_drop:
            if r["response"] not in [rel for _, rel, _ in sums]:
                sums.append((r["response_sha256"], r["response"],
                             (folder / r["response"]).stat().st_size))

    # Everything the drop will say is built in memory first, so the watch-word check can
    # stop a strict drop before anything is written.
    texts = {}

    texts["manifest-sha256.txt"] = "".join(f"{h}  {rel}\n" for h, rel, _ in sums)

    lines = [f"# Box: {box['box']}", ""]
    if description:
        lines += [description, ""]
    lines += [f"- Title: {title}",
              f"- Created: {box.get('created', '')}; dropped: {box.get('dropped', '')}",
              f"- Prepared for: {LABELS[audience]} readers"]
    if use_box:
        lines.append(f"- Default sharing: {LABELS[box_level]}")
    if box.get("author"):
        lines.append(f"- Author: {box['author']}")
    if author_ids:
        lines.append("- Author identifiers: " + "; ".join(author_id_text(e) for e in author_ids))
    if previous:
        lines.append(f"- Replaces the drop: {previous}")
    if topics:
        lines.append(f"- Topics: {'; '.join(topics)}")
    lines += ["", "## Contents", "",
              "| Item | Kind | Live link | Snapshot | Sharing | Notes |",
              "| --- | --- | --- | --- | --- | --- |"]
    for it in kept:
        snap = ", ".join(it.get("snapshot", [])) or "none"
        live = it.get("live") or "none"
        note = it.get("notes", "")
        if it.get("revision"):
            note = f"Revision {it['revision']}. {note}".strip()
        if it.get("live_sha256"):
            when = f" {it['live_pinned']}" if it.get("live_pinned") else ""
            note = f"Live page pinned{when}: SHA-256 {it['live_sha256']}. {note}".strip()
        lines.append(f"| {it['item']} | {it.get('kind', '')} | {live} | {snap} | "
                     f"{LABELS[it['sharing']]} | {note} |")
    if state:
        lines += ["", "## State at drop time", ""] + [f"- {s}" for s in state]
    if repro_kept:
        lines += ["", "## Reproduce", "",
                  f"{repro.get('name') or 'The result'} can be rerun from this drop:", "",
                  "```", command_text(repro["command"]), "```", ""]
        if repro.get("requires"):
            lines.append(f"- Requires: {repro['requires']}")
        lines += [f"- Expected: {describe_expect(e)}" for e in repro["expect"]]
        lines += ["", "Run `build_box.py <this folder> --reproduce`: it checks the drop's "
                      "checksums, shows the command, asks before running it on a temporary "
                      "copy, and reports whether the result reproduced. Running it executes "
                      "this box's code with your permissions; it is not a sandbox."]
    if receipts_kept:
        shown = receipt_findings(dict(box, items=kept), receipts_kept)
        lines += ["", "## Registry receipts", "",
                  "What each registry answered when it was asked, saved under receipts/. "
                  "`build_box.py <this folder> --check-receipts` replays them offline; "
                  "registries do not sign their answers, so a receipt shows what was saved, "
                  "not that the registry said it.", ""]
        lines += ["- " + " ".join(line.split()) for line in shown["lines"]]
    lines += ["", "## Integrity", "",
              "manifest-sha256.txt holds a checksum of every snapshot file, and "
              "tagmanifest-sha256.txt covers this file, box.json and the RO-Crate description. "
              "They show whether the files still match what was dropped; they do not show that "
              "the content is correct. Cite a snapshot by box name, drop date and checksum."]
    if left_out:
        lines += ["", f"{len(left_out)} item(s) were left out because they are not cleared for "
                      f"{LABELS[audience]} readers."]
    texts["BOX.md"] = "\n".join(lines) + "\n"

    graph = [
        {"@id": "ro-crate-metadata.json", "@type": "CreativeWork",
         "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"}, "about": {"@id": "./"}},
    ]
    parts, entities = [], []
    for h, rel, size in sums:
        parts.append({"@id": rel})
        entities.append({"@id": rel, "@type": "File", "name": rel, "contentSize": str(size),
                         "sha256": h})
    for it in kept:
        if it.get("live"):
            parts.append({"@id": it["live"]})
            ent = {"@id": it["live"], "@type": "CreativeWork", "name": it["item"],
                   "additionalType": it.get("kind", ""),
                   "description": f"Live object; sharing: {it['sharing']}"}
            if it.get("revision"):
                ent["version"] = str(it["revision"])
            if it.get("live_sha256"):
                ent["sha256"] = it["live_sha256"]
                if it.get("live_pinned"):
                    ent["sdDatePublished"] = it["live_pinned"]
            entities.append(ent)
    root = {"@id": "./", "@type": "Dataset", "name": title, "identifier": box["box"],
            "description": description or f"Research box {box['box']}",
            "datePublished": box.get("dropped", box.get("created", "")), "hasPart": parts}
    if topics:
        root["keywords"] = topics
    if box.get("license"):
        root["license"] = box["license"]
    if box.get("author") or author_ids:
        root["author"] = {"@id": "#author"}
        person = {"@id": "#author", "@type": "Person",
                  "name": box.get("author") or "Unnamed author"}
        if author_ids:
            person["identifier"] = [author_id_uri(e) for e in author_ids]
        entities.append(person)
    if previous:
        root["isBasedOn"] = {"@id": "#previous-drop"}
        entities.append({"@id": "#previous-drop", "@type": "Dataset", "identifier": previous,
                         "name": f"The earlier drop of {box['box']} that this one replaces"})
    if repro_kept:
        # The author's recorded run, in the shape of the Workflow Run RO-Crate profiles
        # (CreateAction with instrument, object and result). The expected outputs are
        # PropertyValue entities, since the outputs themselves are not in the drop.
        files_in = reproduce_files(folder, repro)
        program = [a for a in repro["command"] if a in files_in][:1]
        if program:
            instrument = {"@id": program[0]}
        else:
            instrument = {"@id": "#reproduce-program"}
            entities.append({"@id": "#reproduce-program", "@type": "SoftwareApplication",
                             "name": repro["command"][0]})
        inputs = [rel for rel in files_in if rel not in program]
        results = []
        for i, e in enumerate(repro["expect"], 1):
            rules = e["values"] if e["match"] == "values" else [None]
            for j, v in enumerate(rules, 1):
                pid = f"#reproduce-expect-{i}" + (f"-{j}" if e["match"] == "values" else "")
                ent = {"@id": pid, "@type": "PropertyValue"}
                if v is None:
                    ent.update(name=f"{e['source']} ({e['match']})", value=e["value"],
                               propertyID=e["match"])
                else:
                    ent["name"] = f"{e['source']} {v['key']}"
                    if "equals" in v:
                        ent["value"] = v["equals"]
                        tol = max(v.get("abs", 0), v.get("rel", 0) * abs(v["equals"]))
                        if tol:
                            ent["minValue"], ent["maxValue"] = v["equals"] - tol, v["equals"] + tol
                    if "at_least" in v:
                        ent["minValue"] = max(ent.get("minValue", v["at_least"]), v["at_least"])
                    if "at_most" in v:
                        ent["maxValue"] = min(ent.get("maxValue", v["at_most"]), v["at_most"])
                    ent["description"] = value_rule(v)
                entities.append(ent)
                results.append({"@id": pid})
        action = {"@id": "#reproduce", "@type": "CreateAction",
                  "name": repro.get("name") or f"Reproduce {box['box']}",
                  "description": command_text(repro["command"]),
                  "instrument": instrument, "result": results}
        if inputs:
            action["object"] = [{"@id": rel} for rel in inputs]
        entities.append(action)
        root["mentions"] = [{"@id": "#reproduce"}]
    graph.append(root)
    graph += entities
    texts["ro-crate-metadata.json"] = json.dumps(
        {"@context": "https://w3id.org/ro/crate/1.1/context", "@graph": graph},
        indent=2, ensure_ascii=False)

    if out:
        # The copy's box.json holds only fields of the format and only the kept items, with
        # state lines as written, so a later export from the copy keeps every label.
        keep_settings = audience != "shareable"
        copy = {}
        for k in ("box", "created", "dropped", "author", "author_ids", "license", "previous"):
            if k in box:
                copy[k] = box[k]
        copy["default_sharing"] = box_level if use_box else audience
        for k, v in (("title", title), ("description", description), ("topics", topics),
                     ("state", state_entries)):
            if v:
                copy[k] = v
        if keep_settings:
            for k in ("public", "level_labels", "private_terms"):
                if k in box:
                    copy[k] = box[k]
        copy["prepared_for"] = audience
        if repro_kept:
            copy["reproduce"] = repro
        copy["items"] = [{k: it[k] for k in ITEM_KEYS if k in it} for it in kept]
        texts["box.json"] = json.dumps(copy, indent=2, ensure_ascii=False) + "\n"
    else:
        texts["box.json"] = (folder / "box.json").read_text(encoding="utf-8")

    # Watch words: terms that should not reach other assistants or people. A shareable drop
    # is scanned in full: every generated file and every text snapshot.
    terms = [t for t in box.get("private_terms", []) if t and t.strip()]
    patterns = [(t, term_pattern(t)) for t in terms]
    hits, unscanned = {}, []
    if terms and audience == "shareable":
        scan = dict(texts)
        for _, rel, _ in sums:
            path = folder / rel
            if path.suffix.lower() in OFFICE:
                text, skipped = office_text(path)
                if text is None:
                    unscanned.append(f"{rel} ({skipped})")
                    continue
                scan[rel] = text
                if skipped:
                    unscanned.append(f"{rel} (embedded: {', '.join(skipped[:3])}"
                                     + (f" and {len(skipped) - 3} more" if len(skipped) > 3
                                        else "") + ")")
                continue
            try:
                scan[rel] = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, ValueError):
                unscanned.append(rel)  # PDF, images, other binary files: check by eye
        for name, text in scan.items():
            found = find_terms(text, patterns)
            if found:
                hits[name] = found

    def describe(name, found):
        return "; ".join(f'{name} line {line}: {term} ("...{context}...")'
                         for term, line, context in found)

    if hits and args.strict:
        refuse("Strict mode: watch words found in "
               + "; ".join(describe(n, f) for n, f in hits.items()) + ".")
    if unscanned and args.strict and not args.allow_unscanned:
        refuse("Strict mode: these files could not be scanned for watch words: "
               + ", ".join(unscanned) + ". Check them by eye, then drop again with "
               "--allow-unscanned, or leave them out.")

    target = out or folder
    if out:
        out.mkdir(parents=True, exist_ok=True)
        for _, rel, _ in sums:
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            if rel in written:
                (out / rel).write_bytes(written[rel])
            else:
                shutil.copy2(folder / rel, out / rel)
    for name in ("manifest-sha256.txt", "BOX.md", "ro-crate-metadata.json"):
        (target / name).write_text(texts[name], encoding="utf-8", newline="\n")
    if out:
        (out / "box.json").write_text(texts["box.json"], encoding="utf-8", newline="\n")
    # Tag manifest (as in BagIt): checksums of the drop's own description files, so a
    # stale or edited BOX.md or box.json is caught as well as a changed snapshot.
    (target / "tagmanifest-sha256.txt").write_text(
        "".join(f"{sha256(target / n)}  {n}\n" for n in TAGGED if (target / n).is_file()),
        encoding="utf-8", newline="\n")

    print(f"{box['box']}: {len(kept)} item(s) kept, {len(sums)} file(s) checksummed"
          + (f", written to {out}" if out else ""))
    if left_out:
        print(f"Left out for {audience} readers: " + ", ".join(it["item"] for it in left_out))
    if state_held:
        print(f"Held back for {audience} readers: {state_held} state line(s)")
    if repro is not None and not repro_kept:
        print(f"Held back for {audience} readers: the reproduce command")
    if not use_box:
        print(f"Held back for {audience} readers: the box's own title, description, topics and "
              f"state" + (" (the public block was used instead)" if box.get("public") else ""))
    if receipts_held:
        print(f"Held back for {audience} readers: {receipts_held} registry receipt(s) about "
              "items left out")
    if receipts_stale and out:
        print(f"Left out of the copy: {receipts_stale} registry receipt(s) for claims the box "
              "no longer makes")
    if extra_fields and out:
        print("Fields outside the format, left out of the copy: " + ", ".join(extra_fields))
    for name, found in hits.items():
        print(f"Watch words in {describe(name, found)} (advisory; --strict refuses the drop)")
    if unscanned:
        print("Not scanned for watch words (check these by eye"
              + ("" if args.strict else "; --strict refuses the drop until you do") + "): "
              + ", ".join(unscanned))
    if not out:
        listed = {rel for _, rel, _ in sums}
        unlisted = [r for r in all_files(folder) if r not in listed and r not in GENERATED]
        if unlisted:
            print("Files in the folder that this drop does not list (remove them, or use --out, "
                  "before others read the folder): " + ", ".join(unlisted))


if __name__ == "__main__":
    main(sys.argv)
