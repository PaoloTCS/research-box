#!/usr/bin/env python3
"""Build BOX.md, ro-crate-metadata.json and manifest-sha256.txt for a research box.

Usage:
  python3 build_box.py <box-folder> [--for shareable|same-assistant|private] [--out <folder>]
                       [--strict [--allow-unscanned]]
  python3 build_box.py <box-folder> --check
  python3 build_box.py <dropped-box> --reproduce [--yes] [--timeout SECONDS] [--report FILE]
  python3 build_box.py <dropped-box> --verify-report REPORT --allowed-signers FILE
                       [--signer IDENTITY] [--signature FILE]
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
          report names this drop (drop_id), and that its verdict is reproduced. Exit status:
          0 verified, 1 a signature, drop, file or verdict does not match, 2 could not check
          (no manifests, unreadable report, no signature file, or no ssh-keygen).

A drop is also refused, writing nothing, when a snapshot path leaves the box folder, a
declared snapshot file is missing, one file is listed under items with different levels, or
a reproduce command uses a path outside the box or needs a file the drop does not hold.

The script never deletes anything. Runs of --reproduce are left in a temporary folder,
which it names, so their outputs can be inspected.
"""
import argparse
import base64
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
import xml.etree.ElementTree as ET
import zipfile
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
VERSION = "0.5.0"
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
    prefix = [exe, "--map-root-user", "--net", "--"]
    try:
        probe = subprocess.run(prefix + [sys.executable, "-c", ""], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return prefix if probe.returncode == 0 else None


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
    for label, expected, observed, matched in rows:
        print(f"  {'match ' if matched else 'DIFFERS'}  {label}: expected {expected}; "
              f"observed {observed}")
    print(f"Result: {verdict}. The run's files are in {work}.")

    if report is not None:
        result.update(ended=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      exit_code=exit_code, run_folder="<tmp>", verdict=verdict,
                      claim=REPORT_CLAIM,
                      expectations=[{"output": label, "expected": expected,
                                     "observed": observed, "matched": matched}
                                    for label, expected, observed, matched in rows])
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
# report says the result reproduced.

REPORT_NAMESPACE = "research-box-report"
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

    drop_id = "sha256:" + sha256(folder / "tagmanifest-sha256.txt")
    if data["drop_id"] != drop_id:
        print(f"The report is about another drop ({data['drop_id']}); this drop is "
              f"{drop_id}. Not verified.")
        return 1
    print(f"Drop: the report names this drop ({drop_id}).")

    logged = None
    if args.log_proof is not None:
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

    for e in data.get("expectations") or []:
        if isinstance(e, dict):
            mark = "match " if e.get("matched") else "DIFFERS"
            print(f"  {mark}  {e.get('output')}: expected {e.get('expected')}; "
                  f"observed {e.get('observed')}")
    when = data.get("started", "an unrecorded time")
    where = data.get("platform", "an unrecorded platform")
    if data["verdict"] != "reproduced":
        print(f"Result: the signed report says this drop did not reproduce "
              f"({data['verdict']}), run {when} on {where}.")
        return 1
    print(f"Result: verified. {identity} signed a report that this drop reproduced, run "
          f"{when} on {where}" + (f", logged as {logged}" if logged else "") + ". A "
          "signature shows who vouches for the run, not that the run was honest or the "
          "result is correct.")
    if data.get("claim"):
        print(f"What the report claims: {data['claim']}")
    return 0


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
    args = p.parse_args(argv[1:])
    verify_only = (args.allowed_signers, args.signer, args.signature, args.log_proof,
                   args.log_signers)
    if args.verify_report is None and any(v is not None for v in verify_only):
        p.error("--allowed-signers, --signer, --signature, --log-proof and --log-signers "
                "go with --verify-report")
    if (args.log_proof is None) != (args.log_signers is None):
        p.error("--log-proof and --log-signers go together")
    if args.verify_report is not None:
        if args.allowed_signers is None:
            p.error("--verify-report needs --allowed-signers FILE")
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
