#!/usr/bin/env python3
"""Build BOX.md, ro-crate-metadata.json and manifest-sha256.txt for a research box.

Usage:
  python3 build_box.py <box-folder> [--for shareable|same-assistant|private] [--out <folder>] [--strict]
  python3 build_box.py <box-folder> --check
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

A drop is also refused, writing nothing, when a snapshot path leaves the box folder, a
declared snapshot file is missing, or one file is listed under items with different levels.

The script never deletes anything.
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path, PurePath

LEVELS = {"private": 0, "same-assistant": 1, "shareable": 2}
LABELS = {"private": "Private", "same-assistant": "Same assistant", "shareable": "Shareable"}
BOX_KEYS = ["box", "title", "description", "created", "dropped", "author", "license",
            "default_sharing", "topics", "state", "public", "level_labels", "private_terms",
            "prepared_for", "items"]
ITEM_KEYS = ["item", "kind", "live", "revision", "snapshot", "sharing", "notes"]
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
    return p.parse_args(argv[1:])


def main(argv):
    args = parse(argv)
    folder = args.folder
    if args.check:
        sys.exit(check(folder))
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
