---
name: research-box
description: Bundle related research work (documents, sheets, diagrams, code, data, chat and task links) into a named box with sharing levels, then make, add to, drop, open, refresh or hand off the box. Use when the user mentions a box, e.g. "drop the box about prime numbers".
license: Apache-2.0
metadata:
  author: Paolo Pignatelli
  developed-with: Claude (Anthropic)
  version: "0.2"
---

# Research boxes

A **box** is a named set of closely related work objects that travel together. It has two layers:

- **Live layer:** links to the objects themselves (documents, spreadsheets, chats, agent tasks, published pages). Links stay current and are the source of truth.
- **Snapshot layer:** portable files exported from those objects at one moment (PDF, XLSX, PNG, code, data, source files), plus a manifest. Writing the snapshot to a folder is called **dropping** the box.

Boxes are listed in a **registry**: a spreadsheet the user keeps, or a `boxes.json` file if they prefer files. If no registry exists when the user first asks for a box, create one with two tables:

- **Boxes:** Box, Topics, Default sharing, Created, Last dropped, Drop location.
- **Items:** Box, Item, Kind, Live link, Snapshot in the drop, Sharing, Notes.

Box names are lowercase-hyphen slugs, such as `prime-numbers` or `lab-notebook`. Topics are plain words, so that "the box about X" can be matched later.

## Sharing levels

Every item carries one of three levels:

| Level | Who may receive it |
| --- | --- |
| Private | The user only. Never placed in a drop that anyone else reads. |
| Same assistant | Other sessions and agents of the same assistant and account. |
| Shareable | Other assistants, collaborators and shared folders. |

Two rules hold everywhere:

1. **Sharing only narrows.** An item keeps its level inside any box, and a box shared more widely than an item still excludes that item.
2. **Drops filter by destination.** A drop into a location others can read contains only the items cleared for those readers, and the reply lists what was left out. A folder that another agent reads counts as read by others: drop there for Same assistant, not Private.

The box's own details (title, description, topics, state lines) carry the box's default sharing. For a wider drop, give a `public` block in `box.json` with wording cleared for those readers; without one, a wider drop shows only the box name.

The user may mark topics or folders as never leaving their machine. Respect that above any level. Terms that must not reach other assistants can be listed as `private_terms`; the script reports every place a shareable drop still contains them, including files whose own level says Shareable.

## Commands

### Make a box ("make a box called X", "put this in a box")
1. Choose the slug from the user's words or the topic.
2. Add a Boxes row and one Items row per object, each with its live link and sharing level. Default to the box's default sharing; ask only when an item's level is genuinely unclear.
3. Do not drop unless asked.

### Add to a box ("add Y to box X")
Add an Items row. A file with no live object gets its file name under "Snapshot in the drop".

### Drop a box ("drop box X [somewhere]")
1. **Find the box.** Match X against box names first, then topics. If several match, take the best match and say which one you took.
2. **Choose the location and the audience.** Use the place the user names; otherwise a `Boxes/<box>/` folder in their downloads or working directory. Decide who will read that location (the user alone, agents of the same assistant, anyone) and drop for that audience.
3. **Build fresh snapshots** in a working folder `Boxes/<box>/`:
   - documents: export to PDF (and Markdown when other assistants will read them) into `doc/`;
   - spreadsheets: export to XLSX, keeping formulas, into `sheet/`; if no XLSX export is available, rebuild the file from cell values and formulas and record the sheet's revision;
   - diagrams and charts: PNG or SVG into `doc/`;
   - code, data and source files: copy into `code/`, `data/` and `sources/`;
   - links with nothing to export (chats, agent tasks, scheduled jobs): listed in the manifest only;
   - large or sensitive files you are not copying: list them with `snapshot` empty and their location in `notes`.
   Record each live object's revision in the item's `revision` field when the platform shows one. If an export cannot be saved byte for byte, skip it and say so.
4. **Write the manifest.** Run `scripts/build_box.py` on a `box.json` describing the items (format in `references/box-format.md`; example in `assets/box.example.json`). It writes:
   - `BOX.md`, the human-readable contents and state at drop time;
   - `ro-crate-metadata.json`, an RO-Crate 1.1 description, so archives and research tools can read the box;
   - `manifest-sha256.txt`, checksums of every snapshot file, so a later reader can tell whether a file changed;
   - `tagmanifest-sha256.txt`, checksums of the files above, so a stale or edited `BOX.md` or `box.json` is caught too.
   Use `--for <audience> --out <new folder>`: the clean copy holds only the cleared files and a `box.json` filtered to them, so nothing held back sits in the folder. If the script cannot run, write the same files by hand.
5. **Copy, verify and record.** Copy the clean folder to the location and run `--check` there; a copy is not done until the check passes at the destination. Then update Last dropped and Drop location in the registry. The registry keeps the full item list; a drop's `box.json` lists only what that drop holds.
6. **Reply** in two or three lines: where it went, and anything left out and why.

### Open a box ("open box X")
Read its registry rows (all of them: a box's items need not be adjacent), or a `BOX.md` or `box.json` the user gives you. Open the live links you can reach and note each object's current revision; for a link this surface cannot open (another app's task, a private chat), say so and rely on the snapshot. Compare the revisions with the snapshot's and run `--check` on the dropped folder. Summarize the current state in a few lines; do not recap history. Continue the work only if the user asks.

### Hand off a box ("give box X to <agent or colleague>")
- For another session or agent of the same assistant, hand over **live links**, not files, so both sides work on the same objects. Include a one-paragraph brief and the sharing levels. Give folder paths as the receiver sees them: a sandboxed agent may mount the user's folders somewhere other than `~`.
- For anyone else, hand over a drop filtered to items marked Shareable.

### Refresh a box ("refresh box X")
1. Run `scripts/build_box.py <dropped folder> --check`. It compares the files with the last manifest and reports changes, missing files and files the manifest does not list.
2. If a snapshot was edited by hand since the last drop, say so instead of overwriting it. If there is no earlier manifest, compare against a copy you kept; otherwise keep each old file as `<name>.prev`.
3. Rebuild the snapshots from the live objects (re-export only what changed; the revision tells you), regenerate the manifest for the same audience, copy, and update Last dropped.

## Citing a box

When a result depends on a box's content, cite the **snapshot** (box name, drop date and the file's checksum), not the live link. A live object can change after the result was derived; a snapshot cannot.

## Style

Say what was done, not how. Ask only when a choice cannot be undone; otherwise take the most reasonable reading and say which one you took.
