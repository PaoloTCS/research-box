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
  "author_ids": [{"scheme": "orcid", "id": "0000-0002-1825-0097"}],
  "previous": "sha256:<drop id of the drop this one replaces>",
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
- `private_terms` (optional) are words or phrases the script looks for in shareable drops: every generated file and every text snapshot. Each term matches as a whole word, in any case, so `ODE` finds "ODE" but not "code"; `_` counts as a word boundary, so `bluefin` finds `bluefin_v2`, and a phrase also matches across a line break, `_` or `-`. Each hit is reported with its file, line and surrounding text; Word, Excel, PowerPoint and OpenDocument files are read as text; other binary files are listed for checking by eye.
- `author_ids` (optional) lists the author's identifiers, each `{"scheme": ..., "id": ...}`. An `orcid` id is the bare iD (`0000-0002-1825-0097`, not the URL) and an `isni` id its 16 characters; both have their check digit verified and a drop with a wrong one is refused. Other schemes are recorded as written. Nothing is minted or looked up in a registry. The drop's `BOX.md` lists them and `ro-crate-metadata.json` gives the author entity an `identifier` list (ORCID and ISNI as their usual URLs).
- `previous` (optional) is the drop id of the drop this one replaces (`sha256:` and 64 lowercase hex digits), so versions of a box form a chain. The RO-Crate root records it under `isBasedOn`.
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
python3 <skill folder>/scripts/build_box.py <box-folder> [--for shareable|same-assistant|private] [--out <new-folder>] [--strict [--allow-unscanned]]
python3 <skill folder>/scripts/build_box.py <box-folder> --check
python3 <skill folder>/scripts/build_box.py <dropped-box> --reproduce [--yes] [--timeout SECONDS] [--no-network] [--report FILE]
python3 <skill folder>/scripts/build_box.py <dropped-box> --verify-report REPORT --allowed-signers FILE [--signer ID] [--signature FILE] [--log-proof FILE --log-signers FILE]
python3 <skill folder>/scripts/build_box.py <dropped-box> --id [--provenance FILE]
python3 <skill folder>/scripts/build_box.py <dropped-box> --verify-provenance RECORD --allowed-signers FILE [--signer ID] [--signature FILE]
python3 <skill folder>/scripts/build_box.py --help
```

The script reads `<box-folder>/box.json` and writes `BOX.md`, `ro-crate-metadata.json` and `manifest-sha256.txt`.

- `--for` sets the audience. Items, state lines and box details narrower than the audience are left out of every output.
- `--out` writes a clean copy into a new or empty folder: the cleared snapshot files, the generated files and a `box.json` filtered to the kept items. Shareable copies also lose `public`, `level_labels` and `private_terms`, since those name what was held back. `--out` is required whenever the drop holds anything back (items, state lines or the box's own details): without it the script refuses and writes nothing, so held-back files and a full `box.json` can never sit beside what is shared. A drop that holds nothing back may be written in place; the script then lists any files in the folder that the drop does not cover.
- `--strict` refuses a shareable drop, writing nothing, when a watch word appears in any generated file, text snapshot, or the text and properties of a Word, Excel, PowerPoint or OpenDocument file. It also refuses while any file could not be scanned (PDF, images, archives, unreadable files, Office files with embedded media), listing them; after checking those by eye, add `--allow-unscanned` to drop anyway. Without `--strict`, watch words and unscanned files are only reported.
- `--check` compares a dropped folder with `manifest-sha256.txt` and `tagmanifest-sha256.txt` and reports changed, missing and unlisted files. It writes nothing. Exit status: 0 when everything matches, 1 when a file changed, went missing or is not listed (or a manifest names a path outside the folder), 2 when the check is incomplete (no manifest or no tag manifest) or the command line is wrong. Scripts and pipelines can gate on it. Run it after every copy, at the destination, and before a refresh.

- `--reproduce` reruns a dropped box's declared command and compares its output with `expect`. It runs `--check` first and stops unless the drop matches (exit 1, or 2 without manifests), then shows the command, the timeout and `requires` and asks before running (`--yes` skips the question; `--timeout` overrides the box's). The command runs on a temporary copy of the drop, so no output lands in the drop, with only `PATH`, `LANG` and a temporary `HOME` from the environment. Declared output files that are already in the drop are removed from the copy before the run (unless the command reads them), so a command that never writes them does not pass. This is not a sandbox: the box's code runs with the opener's permissions and can reach the network, so run boxes from strangers in a container or virtual machine. `--no-network` runs the command in a new Linux network namespace with no network at all (it needs `unshare` and unprivileged user namespaces, and refuses to run, exit 2, where they are missing). Exit status: 0 reproduced, 1 did not reproduce (an output differed or the command failed), 2 not run or inconclusive (no reproduce block, declined, could not start, or timed out). `--report <file>` (outside the drop) writes the result as JSON: box name, drop date, `drop_id` (the SHA-256 of `tagmanifest-sha256.txt`, which identifies the drop), command, platform, Python version, times, each expectation with its observed value, and the verdict; also the research-box version (`tool`), `network` (`not restricted` or `none (Linux network namespace)`), the names of the `environment` variables passed, `cleared_outputs`, `stored_values` and `claim`. `stored_values` is a best-effort scan: declared output values (numbers of four or more digits, texts of eight or more characters, or files with the expected SHA-256) that already appear in the box's own files, with the files. A hit does not prove a box is faked and no hit does not prove it is honest; it shows a reader where to look. `claim` states what the run shows: running these exact files with the declared command produced the declared outputs within the declared tolerances, and not that they were computed rather than stored or fetched, that the method is correct, or that the result holds elsewhere. The temporary folder is reported as `<tmp>`.

- `--verify-report <report> --allowed-signers <file>` checks a reproduction report that someone signed, without running anything from the box. Whoever ran `--reproduce --report report.json` signs the report with an SSH key: `ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n research-box-report report.json`, which writes `report.json.sig` beside it. The opener lists the keys they trust in an OpenSSH `allowed_signers` file, one line per key, for example `alice@example.org namespaces="research-box-report" ssh-ed25519 AAAA...`. The command checks, in order: the drop against its manifests; the signature, with the local `ssh-keygen` (OpenSSH 8.2 or later), against those keys and the `research-box-report` namespace (`--signer <identity>` requires a particular signer; `--signature <file>` overrides the default `<report>.sig`); that the report's `drop_id` is this drop's; that its verdict is `reproduced`; and that the report's own contents support that verdict: the comparison is worked out again from the drop's declared `expect` block and the report's observed values, with the same tolerances as `--reproduce`, and the report fails if a row is missing, extra or labelled differently from what the drop declares, if a row's `matched` disagrees with that comparison, if any output differs, if the command is not the drop's, if the exit code is missing or not 0, or (for a multi-run rerun report) if any run did not reproduce. Exit status: 0 verified, 1 the drop, signature, drop id, verdict or recomputed comparison does not match, 2 the check could not be made (no manifests, an unreadable report, no signature or allowed-signers file, or no `ssh-keygen`). `--log-proof <proof.json> --log-signers <file>` also requires that the report and its signature are recorded in a transparency log: the proof holds the log's name (`log`), `leaf_index`, `tree_size`, the `inclusion` hashes (hex) of an RFC 9162 Merkle inclusion proof, a C2SP-style `checkpoint` (log name, tree size and base64 root hash, one per line) and its `checkpoint_signature`, made with `ssh-keygen -Y sign -n research-box-log`. The leaf is the text `research-box-report/v1`, then the SHA-256 of the report bytes and of the signature bytes, one per line. The checkpoint must be signed by a key in the log signers file, and the proof must lead from that leaf to its root; otherwise exit 1. Without a log proof the report's times are only the signer's word, and the command says so. A valid signature shows which key vouches for the run. It does not show that the run was honest or that the result is correct. Keep the report and its signature outside the drop, so the drop still matches its manifests.

- `--id` prints a dropped box's portable identifiers, after checking the drop against its manifests (exit 1 or 2 as `--check` when it does not match). Both are SHA-256 digests that anyone can work out again from the files alone, on any machine:
  - the **content id**, the SHA-256 of the manifest's lines (`<hash>  <path>`, each ending in a newline) sorted by path: it names the snapshot files and nothing else, so a reworded `BOX.md` or a different item order keeps it, and any changed file changes it;
  - the **drop id**, the SHA-256 of `tagmanifest-sha256.txt`: this exact drop with its description, the id a reproduction report names.
  It also prints the author, `author_ids` and `previous`. `--provenance <file>` (outside the drop) writes them as a provenance record (`type` `research-box-provenance/v1`, with `box`, `dropped`, `content_id`, `drop_id`, `files`, `author`, `author_ids`, `previous`, `tool`, `written` and `claim`). Whoever vouches for it signs it with an SSH key, Ed25519 for example: `ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n research-box-provenance provenance.json`.
- `--verify-provenance <record> --allowed-signers <file>` checks a signed provenance record without running anything: the drop against its manifests, the signature against the allowed keys in the `research-box-provenance` namespace (`--signer` and `--signature` as for reports), and that every id in the record (box, drop date, content id, drop id, file count, author, author ids, previous) is this drop's own. Exit status: 0 verified, 1 the drop, signature or an id does not match, 2 the check could not be made. A valid signature shows who vouches for the record; the author identifiers are not looked up in any registry, so it does not show that the named person wrote the box.

The script never deletes files. Runs of `--reproduce` stay in the temporary folder the script names. A manifest can also be checked without it: `shasum -a 256 -c manifest-sha256.txt` (macOS) or `sha256sum -c manifest-sha256.txt` (Linux).

## Relation to existing standards

- **RO-Crate** (Research Object Crate) packages research artefacts with JSON-LD metadata based on schema.org. The box's `ro-crate-metadata.json` follows RO-Crate 1.1, so RO-Crate tools and repositories can read a dropped box.
- **Reproduce.** When a drop keeps a `reproduce` block, `ro-crate-metadata.json` records it as a `CreateAction` (`#reproduce`, mentioned by the root dataset) with `instrument` (the script it runs, or the program), `object` (its inputs), `description` (the command line) and `result` (one schema.org `PropertyValue` per expected output, with `value`, `minValue` and `maxValue` where they apply). This follows the shape of the Workflow Run RO-Crate profiles, but a box does not claim conformance to them: the Process Run Crate profile (version 0.6) builds on RO-Crate 1.3, and a box declares 1.1.
- **BagIt** (RFC 8493) is about reliable transfer with checksums. `manifest-sha256.txt` and `tagmanifest-sha256.txt` use the same line format and roles as a BagIt payload manifest and tag manifest. A full BagIt bag would also need `bagit.txt` and the payload under `data/`, which a box does not require.

What a box adds to these standards is the live layer (links that stay current), sharing levels that only narrow, and the agent commands in `SKILL.md`.
