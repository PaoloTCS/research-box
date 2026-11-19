# research-box

An Agent Skill for keeping related research work together: documents, spreadsheets, diagrams, code, data, chats and agent tasks bundled into a named **box**, with sharing levels that only narrow.

Say "make a box called prime-numbers", "add this sheet to the box", "drop the box about prime numbers", "open box prime-numbers" or "give the box to my other agent", and the agent knows what to do.

- **Live layer:** links that stay current. Agents of the same assistant hand each other links, not files.
- **Snapshot layer:** a dropped folder with exported files, a readable `BOX.md`, an RO-Crate 1.1 `ro-crate-metadata.json`, and SHA-256 checksums of every file.
- **Sharing levels:** private, same assistant, shareable. A drop for wider readers leaves out what is not cleared for them, writes a clean copy that holds nothing else, and says what it left out.
- **Checks:** `--check` tells you whether any file in a drop, including its own description, changed or was copied stale, and a list of watch words flags private terms that are about to leave in a shareable drop.

## Contents

```
research-box/
  SKILL.md                     instructions the agent follows
  references/box-format.md     the box.json format and how it relates to RO-Crate and BagIt
  scripts/build_box.py         writes BOX.md, ro-crate-metadata.json and manifest-sha256.txt
  assets/box.example.json      an example box
  LICENSE                      Apache License 2.0
  NOTICE                       copyright and attribution
```

`build_box.py` uses the Python standard library only. Manifests can also be verified with `shasum -a 256 -c manifest-sha256.txt`.

## Install

The skill follows the open Agent Skills format (agentskills.io). Copy the `research-box` folder into your agent's skills directory, or upload it as a skill in an app that supports skills.

## Versions

- **0.2** (November 19, 2026), after the first real drops: box details and state lines follow the box's sharing level, with an optional `public` block for wider drops; `--out` writes a clean filtered copy; `--check` compares a drop with its manifest and a BagIt-style tag manifest, after every copy and before a refresh; watch words (`private_terms`); item revisions; registry label mapping (`level_labels`).
- **0.1** (November 19, 2026): first version.

## Dedication

Released November 19, 2026, with thanks to the people at Anthropic who made Claude, which helped build it.

## Credits

Created by **Paolo Pignatelli**. Developed in collaboration with **Claude**, an AI model made by **Anthropic**, which drafted and tested the skill text, the manifest format and the build script (November 2026).

This project is not affiliated with or endorsed by Anthropic.

## License

Apache License 2.0. See `research-box/LICENSE` and `research-box/NOTICE`. If you redistribute the skill, keep the NOTICE file, which carries the attribution above.
