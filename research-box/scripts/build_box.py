#!/usr/bin/env python3
"""Build BOX.md, ro-crate-metadata.json and manifest-sha256.txt for a research box.

Usage:
  python3 build_box.py <box-folder> [--for shareable|same-assistant|private] [--out <folder>] [--strict]
  python3 build_box.py <box-folder> --check
  python3 build_box.py <dropped-box> --reproduce [--yes] [--timeout SECONDS] [--report FILE]
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
          in any generated file or text snapshot. Without it, watch words are reported only.
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

A drop is also refused, writing nothing, when a snapshot path leaves the box folder, a
declared snapshot file is missing, one file is listed under items with different levels, or
a reproduce command uses a path outside the box or needs a file the drop does not hold.

The script never deletes anything. Runs of --reproduce are left in a temporary folder,
which it names, so their outputs can be inspected.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePath

LEVELS = {"private": 0, "same-assistant": 1, "shareable": 2}
LABELS = {"private": "Private", "same-assistant": "Same assistant", "shareable": "Shareable"}
BOX_KEYS = ["box", "title", "description", "created", "dropped", "author", "license",
            "default_sharing", "topics", "state", "public", "level_labels", "private_terms",
            "prepared_for", "reproduce", "items"]
ITEM_KEYS = ["item", "kind", "live", "revision", "snapshot", "sharing", "notes"]
REPRO_KEYS = ["name", "command", "inputs", "requires", "timeout_seconds", "sharing", "expect"]
GENERATED = {"BOX.md", "box.json", "ro-crate-metadata.json", "manifest-sha256.txt",
             "tagmanifest-sha256.txt"}
TAGGED = ["BOX.md", "box.json", "ro-crate-metadata.json", "manifest-sha256.txt"]


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
    """Match a watch word as a whole word or phrase, any case. Spaces inside a phrase
    match any run of whitespace, so a phrase broken across lines is still found."""
    words = [re.escape(w) for w in term.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


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
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*")
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
        ok = True
        if "equals" in v:
            allowed = max(v.get("abs", 0), v.get("rel", 0) * abs(v["equals"]))
            ok = abs(got - v["equals"]) <= allowed
        if "at_least" in v:
            ok = ok and got >= v["at_least"]
        if "at_most" in v:
            ok = ok and got <= v["at_most"]
        rows.append((name, value_rule(v), got, ok))
    return rows


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
    print("")
    print(f"Reproduce: {block.get('name', box.get('box', ''))}")
    print(f"  Command:  {command_text(command)}")
    print(f"  Runs in:  a temporary copy of {folder}")
    print(f"  Timeout:  {timeout:g} seconds")
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
              "started": started.isoformat(timespec="seconds")}
    rows, verdict, code, exit_code = [], "inconclusive", 2, None
    try:
        proc = subprocess.run(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        print(f"The command did not finish within {timeout:g} seconds. Inconclusive.")
    except OSError as err:
        print(f"The command could not start ({err.strerror or err}). Inconclusive: check "
              f"that this machine has what the box requires.")
    else:
        exit_code = proc.returncode
        for e in block["expect"]:
            if e["source"] == "stdout":
                raw = proc.stdout
            else:
                path = work / e["source"]
                raw = path.read_bytes() if path.is_file() else None
            rows += compare(e, raw)
        if exit_code != 0:
            tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-5:]
            print(f"The command exited with status {exit_code}." +
                  ("\n  " + "\n  ".join(tail) if tail else ""))
        ok = exit_code == 0 and all(r[3] for r in rows)
        verdict, code = ("reproduced", 0) if ok else ("did not reproduce", 1)
    for label, expected, observed, matched in rows:
        print(f"  {'match ' if matched else 'DIFFERS'}  {label}: expected {expected}; "
              f"observed {observed}")
    print(f"Result: {verdict}. The run's files are in {work}.")

    if report is not None:
        result.update(ended=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      exit_code=exit_code, run_folder="<tmp>", verdict=verdict,
                      expectations=[{"output": label, "expected": expected,
                                     "observed": observed, "matched": matched}
                                    for label, expected, observed, matched in rows])
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
        print(f"Report written to {report}.")
    return code


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
    p.add_argument("--report", type=Path, metavar="FILE",
                   help="with --reproduce: write the result as JSON here (outside the drop)")
    args = p.parse_args(argv[1:])
    if not args.reproduce and (args.yes or args.timeout is not None or args.report):
        p.error("--yes, --timeout and --report go with --reproduce")
    if args.reproduce and (args.check or args.out or args.strict or args.audience != "private"):
        p.error("--reproduce runs on a dropped box; use it without --check, --for, --out "
                "or --strict")
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
            entities.append(ent)
    root = {"@id": "./", "@type": "Dataset", "name": title, "identifier": box["box"],
            "description": description or f"Research box {box['box']}",
            "datePublished": box.get("dropped", box.get("created", "")), "hasPart": parts}
    if topics:
        root["keywords"] = topics
    if box.get("license"):
        root["license"] = box["license"]
    if box.get("author"):
        root["author"] = {"@id": "#author"}
        entities.append({"@id": "#author", "@type": "Person", "name": box["author"]})
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
        for k in ("box", "created", "dropped", "author", "license"):
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
    hits, binaries = {}, []
    if terms and audience == "shareable":
        scan = dict(texts)
        for _, rel, _ in sums:
            try:
                scan[rel] = (folder / rel).read_text(encoding="utf-8")
            except (UnicodeDecodeError, ValueError):
                binaries.append(rel)  # PDF, XLSX, PNG: check those by eye
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

    target = out or folder
    if out:
        out.mkdir(parents=True, exist_ok=True)
        for _, rel, _ in sums:
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / rel, out / rel)
    for name in ("manifest-sha256.txt", "BOX.md", "ro-crate-metadata.json"):
        (target / name).write_text(texts[name], encoding="utf-8")
    if out:
        (out / "box.json").write_text(texts["box.json"], encoding="utf-8")
    # Tag manifest (as in BagIt): checksums of the drop's own description files, so a
    # stale or edited BOX.md or box.json is caught as well as a changed snapshot.
    (target / "tagmanifest-sha256.txt").write_text(
        "".join(f"{sha256(target / n)}  {n}\n" for n in TAGGED if (target / n).is_file()),
        encoding="utf-8")

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
    if extra_fields and out:
        print("Fields outside the format, left out of the copy: " + ", ".join(extra_fields))
    for name, found in hits.items():
        print(f"Watch words in {describe(name, found)} (advisory; --strict refuses the drop)")
    if binaries:
        print("Not scanned for watch words (binary): " + ", ".join(binaries))
    if not out:
        listed = {rel for _, rel, _ in sums}
        unlisted = [r for r in all_files(folder) if r not in listed and r not in GENERATED]
        if unlisted:
            print("Files in the folder that this drop does not list (remove them, or use --out, "
                  "before others read the folder): " + ", ".join(unlisted))


if __name__ == "__main__":
    main(sys.argv)
