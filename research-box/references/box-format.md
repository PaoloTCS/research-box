# Box format

A dropped box is a folder:

```
<box>/
  BOX.md                    human-readable contents and state at drop time
  box.json                  the item list the manifest was built from
  ro-crate-metadata.json    RO-Crate 1.1 description of the box
  manifest-sha256.txt       SHA-256 checksum of every snapshot file
  tagmanifest-sha256.txt    SHA-256 checksums of BOX.md, box.json, ro-crate-metadata.json and the manifest
  doc/  sheet/  code/  data/  sources/    snapshot files, as needed
```

## box.json

```json
{
  "box": "prime-numbers",
  "title": "Prime numbers and addressing",
  "description": "One or two sentences on what the box is for.",
  "created": "2026-11-19",
  "dropped": "2026-11-19",
  "author": "Name of the person the box belongs to",
  "default_sharing": "same-assistant",
  "topics": ["primes", "addressing"],
  "state": [
    "One line per fact a later reader needs about where the work stands.",
    {"text": "A line narrower than the box itself.", "sharing": "private"}
  ],
  "public": {
    "title": "Wording cleared for shareable drops",
    "description": "Used instead of the fields above when the drop is wider than default_sharing.",
    "topics": ["primes"],
    "state": ["Lines cleared for those readers."]
  },
  "private_terms": ["internal project name"],
  "level_labels": {"Claude only": "same-assistant", "Other engines OK": "shareable"},
  "items": [
    {
      "item": "Working notes",
      "kind": "document",
      "live": "https://example.com/link-to-the-live-document",
      "revision": 12,
      "snapshot": ["doc/working-notes.pdf"],
      "sharing": "same-assistant",
      "notes": "Optional"
    }
  ]
}
```

- `sharing` is one of `private`, `same-assistant` or `shareable`.
- `live` is a URL or `null`.
- `snapshot` lists paths relative to the box folder, or is empty.
- `kind` is free text: document, spreadsheet, diagram, code, data, source file, chat, agent task, scheduled task.
- `revision` (optional) is the live object's revision when the snapshot was made.
- `state` lines are plain strings, which carry the box's default sharing, or objects with their own `sharing`.
- `public` (optional) replaces title, description, topics and state in drops wider than `default_sharing`. Without it, such drops show only the box name.
- `private_terms` (optional) are words the script looks for in shareable drops: item names, notes, state lines and text files. It reports each hit; binary files are listed for checking by eye.
- `level_labels` (optional) maps the labels of the user's registry to the three levels.

## Building the manifest

```
python3 scripts/build_box.py <box-folder> [--for shareable|same-assistant|private] [--out <new-folder>]
python3 scripts/build_box.py <box-folder> --check
```

The script reads `<box-folder>/box.json` and writes `BOX.md`, `ro-crate-metadata.json` and `manifest-sha256.txt`.

- `--for` sets the audience. Items, state lines and box details narrower than the audience are left out of every output.
- `--out` writes a clean copy into a new or empty folder: the cleared snapshot files, the generated files and a `box.json` filtered to the kept items. Shareable copies also lose `public`, `level_labels` and `private_terms`, since those name what was held back. `--out` is required whenever the drop holds anything back (items, state lines or the box's own details): without it the script refuses and writes nothing, so held-back files and a full `box.json` can never sit beside what is shared. A drop that holds nothing back may be written in place; the script then lists any files in the folder that the drop does not cover.
- `--check` compares a dropped folder with `manifest-sha256.txt` and `tagmanifest-sha256.txt` and reports changed, missing and unlisted files. It writes nothing. Exit status: 0 when everything matches, 1 when a file changed, went missing or is not listed, 2 when there is no manifest. Scripts and pipelines can gate on it. Run it after every copy, at the destination, and before a refresh.

The script never deletes files. A manifest can also be checked without it: `shasum -a 256 -c manifest-sha256.txt` (macOS) or `sha256sum -c manifest-sha256.txt` (Linux).

## Relation to existing standards

- **RO-Crate** (Research Object Crate) packages research artefacts with JSON-LD metadata based on schema.org. The box's `ro-crate-metadata.json` follows RO-Crate 1.1, so RO-Crate tools and repositories can read a dropped box.
- **BagIt** (RFC 8493) is about reliable transfer with checksums. `manifest-sha256.txt` and `tagmanifest-sha256.txt` use the same line format and roles as a BagIt payload manifest and tag manifest. A full BagIt bag would also need `bagit.txt` and the payload under `data/`, which a box does not require.

What a box adds to these standards is the live layer (links that stay current), sharing levels that only narrow, and the agent commands in `SKILL.md`.
