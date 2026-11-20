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
  "reproduce": {
    "name": "Stretch table",
    "command": ["python3", "code/simulate.py"],
    "inputs": ["data/params.json"],
    "requires": "Python 3.10 or later, standard library only",
    "timeout_seconds": 600,
    "sharing": "shareable",
    "expect": [
      {"source": "stdout", "match": "text", "value": "[487, 267]"},
      {"source": "out/results.json", "match": "values", "values": [
        {"key": "mean_stretch", "equals": 2.84, "abs": 0.01},
        {"key": "false_promotions", "at_most": 9}
      ]},
      {"source": "out/table.csv", "match": "sha256", "value": "<64 lowercase hex digits>"}
    ]
  },
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
- `snapshot` lists paths relative to the box folder, or is empty. Each path must stay inside the folder: absolute paths, `..` and symlinks that lead outside are refused before anything is written. Every listed file must exist when a drop includes the item; a file you deliberately do not copy goes in `notes`, with `snapshot` empty. One file may not be listed under items with different sharing levels.
- `kind` is free text: document, spreadsheet, diagram, code, data, source file, chat, agent task, scheduled task.
- `revision` (optional) is the live object's revision when the snapshot was made.
- `state` lines are plain strings, which carry the box's default sharing, or objects with their own `sharing`. Drops keep each line as written, so a label survives later exports.
- `public` (optional) replaces title, description, topics and state in drops wider than `default_sharing`. Without it, such drops show only the box name.
- `private_terms` (optional) are words or phrases the script looks for in shareable drops: every generated file and every text snapshot. Each term matches as a whole word, in any case, so `ODE` finds "ODE" but not "code"; a phrase also matches across a line break. Each hit is reported with its file, line and surrounding text; binary files are listed for checking by eye.
- `level_labels` (optional) maps the labels of the user's registry to the three levels.
- `reproduce` (optional) declares one command whose output proves a result, so whoever opens a drop can rerun it with `--reproduce`:
  - `command` is a list: the program, then its arguments. It runs without a shell, so pipes, globs and `$VARS` are not expanded; put several steps in a script under `code/`. No argument may be an absolute or home path or contain `..`, and an argument naming a box file must be a snapshot the drop holds.
  - `inputs` (optional) lists other box files the command reads; each must be in the drop.
  - `expect` lists what the output should be. `source` is `stdout` or a file the command writes, relative to the box folder. `match` is `sha256` (exact bytes; `value` is the hex digest), `text` (exact text after normalizing line endings and trailing spaces) or `values` (named numbers read from JSON). Each `values` entry has a `key` (a dotted path; list positions are numbers, e.g. `rows.0.n`) and `equals` (with optional `abs` or `rel` tolerance), `at_most`, `at_least`, or a combination of the two bounds. For research results, prefer `values`: byte hashes break on timestamps, float formatting and key order.
  - `requires` (optional) is free text shown before running, e.g. the Python version. `timeout_seconds` defaults to 600.
  - `sharing` (optional, default the box's default) works like an item's: a drop for wider readers holds the block back, which requires `--out`. When the block is kept, a file it runs or reads that the drop does not hold refuses the drop.
- Fields other than those above are never copied into a drop's `box.json`, and a drop for readers other than the user that would carry them must use `--out`.

## Building the manifest

```
python3 scripts/build_box.py <box-folder> [--for shareable|same-assistant|private] [--out <new-folder>] [--strict]
python3 scripts/build_box.py <box-folder> --check
python3 scripts/build_box.py <dropped-box> --reproduce [--yes] [--timeout SECONDS] [--report FILE]
python3 scripts/build_box.py --help
```

The script reads `<box-folder>/box.json` and writes `BOX.md`, `ro-crate-metadata.json` and `manifest-sha256.txt`.

- `--for` sets the audience. Items, state lines and box details narrower than the audience are left out of every output.
- `--out` writes a clean copy into a new or empty folder: the cleared snapshot files, the generated files and a `box.json` filtered to the kept items. Shareable copies also lose `public`, `level_labels` and `private_terms`, since those name what was held back. `--out` is required whenever the drop holds anything back (items, state lines or the box's own details): without it the script refuses and writes nothing, so held-back files and a full `box.json` can never sit beside what is shared. A drop that holds nothing back may be written in place; the script then lists any files in the folder that the drop does not cover.
- `--strict` refuses a shareable drop, writing nothing, when a watch word appears in any generated file or text snapshot. Without it, watch words are only reported.
- `--check` compares a dropped folder with `manifest-sha256.txt` and `tagmanifest-sha256.txt` and reports changed, missing and unlisted files. It writes nothing. Exit status: 0 when everything matches, 1 when a file changed, went missing or is not listed (or a manifest names a path outside the folder), 2 when the check is incomplete (no manifest or no tag manifest) or the command line is wrong. Scripts and pipelines can gate on it. Run it after every copy, at the destination, and before a refresh.

- `--reproduce` reruns a dropped box's declared command and compares its output with `expect`. It runs `--check` first and stops unless the drop matches (exit 1, or 2 without manifests), then shows the command, the timeout and `requires` and asks before running (`--yes` skips the question; `--timeout` overrides the box's). The command runs on a temporary copy of the drop, so no output lands in the drop, with only `PATH`, `LANG` and a temporary `HOME` from the environment. This is not a sandbox: the box's code runs with the opener's permissions and can reach the network, so run boxes from strangers in a container or virtual machine. Exit status: 0 reproduced, 1 did not reproduce (an output differed or the command failed), 2 not run or inconclusive (no reproduce block, declined, could not start, or timed out). `--report <file>` (outside the drop) writes the result as JSON: box name, drop date, `drop_id` (the SHA-256 of `tagmanifest-sha256.txt`, which identifies the drop), command, platform, Python version, times, each expectation with its observed value, and the verdict. The temporary folder is reported as `<tmp>`.

The script never deletes files. Runs of `--reproduce` stay in the temporary folder the script names. A manifest can also be checked without it: `shasum -a 256 -c manifest-sha256.txt` (macOS) or `sha256sum -c manifest-sha256.txt` (Linux).

## Relation to existing standards

- **RO-Crate** (Research Object Crate) packages research artefacts with JSON-LD metadata based on schema.org. The box's `ro-crate-metadata.json` follows RO-Crate 1.1, so RO-Crate tools and repositories can read a dropped box.
- **Reproduce.** When a drop keeps a `reproduce` block, `ro-crate-metadata.json` records it as a `CreateAction` (`#reproduce`, mentioned by the root dataset) with `instrument` (the script it runs, or the program), `object` (its inputs), `description` (the command line) and `result` (one schema.org `PropertyValue` per expected output, with `value`, `minValue` and `maxValue` where they apply). This follows the shape of the Workflow Run RO-Crate profiles, but a box does not claim conformance to them: the Process Run Crate profile (version 0.6) builds on RO-Crate 1.3, and a box declares 1.1.
- **BagIt** (RFC 8493) is about reliable transfer with checksums. `manifest-sha256.txt` and `tagmanifest-sha256.txt` use the same line format and roles as a BagIt payload manifest and tag manifest. A full BagIt bag would also need `bagit.txt` and the payload under `data/`, which a box does not require.

What a box adds to these standards is the live layer (links that stay current), sharing levels that only narrow, and the agent commands in `SKILL.md`.
