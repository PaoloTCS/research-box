#!/usr/bin/env python3
"""Build BOX.md, ro-crate-metadata.json and manifest-sha256.txt for a research box.

Usage:
  python3 build_box.py <box-folder> [--for shareable|same-assistant|private] [--out <folder>]
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
          listed, 2 = no manifest to check against. Run it after every copy and before a
          refresh.

The script never deletes anything.
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path, PurePath

LEVELS = {"private": 0, "same-assistant": 1, "shareable": 2}
LABELS = {"private": "Private", "same-assistant": "Same assistant", "shareable": "Shareable"}
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
    recorded = {}
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
        print("No tagmanifest-sha256.txt: BOX.md and box.json are not covered by this check.")
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
    p.add_argument("--check", action="store_true",
                   help="compare a dropped box with its manifests; exit 0 match, 1 difference, "
                        "2 no manifest")
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

    # Every snapshot path must stay inside the box folder, so a box.json cannot pull in
    # (or, with --out, write) files elsewhere. Checked for all items before anything is written.
    bad = [rel for it in box["items"] for rel in it.get("snapshot", []) if not inside(folder, rel)]
    if bad:
        sys.exit("Snapshot paths must be relative paths inside the box folder; these are not: "
                 + ", ".join(map(str, bad)) + ". Nothing was written.")

    def cleared(level):
        return LEVELS[level] >= LEVELS[audience]

    # Box details (title, description, topics, state) carry the box's default sharing.
    # For a wider audience, use the "public" block if there is one; otherwise show only
    # the box name.
    box_level = level_of(box.get("default_sharing"), box, "box")
    use_box = cleared(box_level)
    details = box if use_box else dict(box.get("public", {}))
    title = details.get("title", box["box"])
    description = details.get("description", "")
    topics = details.get("topics", [])
    state, state_held = [], 0
    for s in details.get("state", []):
        if isinstance(s, str):
            state.append(s)  # inherits the level of the block it sits in
        elif cleared(level_of(s.get("sharing"), box, "state line")):
            state.append(s["text"])
        else:
            state_held += 1

    kept, left_out = [], []
    for it in box["items"]:
        it = dict(it, sharing=level_of(it.get("sharing"), box, f"item {it['item']!r}"))
        (kept if cleared(it["sharing"]) else left_out).append(it)

    # A drop that holds anything back must be a clean copy: written in place, the folder
    # would still hold the full box.json and the held-back files beside what is shared.
    held = []
    if left_out:
        held.append(f"{len(left_out)} item(s)")
    if state_held:
        held.append(f"{state_held} state line(s)")
    if not use_box:
        held.append("the box's own details")
    if held and not out:
        sys.exit(f"This {audience} drop holds back {', '.join(held)}. Write it as a clean copy: "
                 f"add --out <new folder>. Nothing was written.")

    # Checksums of kept snapshot files.
    sums, missing = [], []
    for it in kept:
        for rel in it.get("snapshot", []):
            p = folder / rel
            if p.is_file():
                sums.append((sha256(p), rel, p.stat().st_size))
            else:
                missing.append(rel)

    target = folder
    if out:
        if out.exists() and any(out.iterdir()):
            sys.exit(f"--out folder {out} is not empty")
        out.mkdir(parents=True, exist_ok=True)
        for _, rel, _ in sums:
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / rel, out / rel)
        target = out

    (target / "manifest-sha256.txt").write_text(
        "".join(f"{h}  {rel}\n" for h, rel, _ in sums), encoding="utf-8")

    # BOX.md
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
              "Checksums of every snapshot file are in manifest-sha256.txt. "
              "Cite a snapshot by box name, drop date and checksum, not by its live link."]
    if left_out:
        lines += ["", f"{len(left_out)} item(s) were left out because they are not cleared for "
                      f"{LABELS[audience]} readers."]
    (target / "BOX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ro-crate-metadata.json (RO-Crate 1.1)
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
    (target / "ro-crate-metadata.json").write_text(
        json.dumps({"@context": "https://w3id.org/ro/crate/1.1/context", "@graph": graph},
                   indent=2, ensure_ascii=False), encoding="utf-8")

    if out:
        # A filtered box.json, so the copy does not list what was left out.
        # Drops that stay with the same assistant keep the drop settings, so the drop can
        # be refreshed in place; shareable drops lose them, since they name what is held back.
        held = ("items", "title", "description", "topics", "state")
        if audience == "shareable":
            held += ("public", "level_labels", "private_terms")
        filtered = {k: v for k, v in box.items() if k not in held}
        filtered.update({k: v for k, v in (("title", title), ("description", description),
                                           ("topics", topics), ("state", state)) if v})
        filtered.update({"prepared_for": audience, "items": kept})
        if not use_box:
            filtered.pop("default_sharing", None)
        (out / "box.json").write_text(json.dumps(filtered, indent=2, ensure_ascii=False) + "\n",
                                      encoding="utf-8")

    # Tag manifest (as in BagIt): checksums of the drop's own description files, so a
    # stale or edited BOX.md or box.json is caught as well as a changed snapshot.
    (target / "tagmanifest-sha256.txt").write_text(
        "".join(f"{sha256(target / n)}  {n}\n" for n in TAGGED if (target / n).is_file()),
        encoding="utf-8")

    print(f"{box['box']}: {len(kept)} item(s) kept, {len(sums)} file(s) checksummed"
          + (f", written to {out}" if out else ""))
    if missing:
        print("Snapshot files listed but not found: " + ", ".join(missing))
    if left_out:
        print(f"Left out for {audience} readers: " + ", ".join(it["item"] for it in left_out))
    # Watch words: terms that should not reach other assistants or people. Scan what a
    # shareable drop contains (names, notes, state and text files) and report every hit.
    terms = [t for t in box.get("private_terms", []) if t]
    if terms and audience == "shareable":
        hits = {}
        texts = [("BOX.md", (target / "BOX.md").read_text(encoding="utf-8"))]
        for _, rel, _ in sums:
            try:
                texts.append((rel, (folder / rel).read_text(encoding="utf-8")))
            except (UnicodeDecodeError, ValueError):
                pass  # binary snapshot (PDF, XLSX, PNG): check those by eye
        for name, text in texts:
            low = text.lower()
            found = [t for t in terms if t.lower() in low]
            if found:
                hits[name] = found
        for name, found in hits.items():
            print(f"Watch words in {name}: {', '.join(found)}")
        binaries = [rel for _, rel, _ in sums if rel not in dict(texts)]
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
