# Example box

`prime-numbers/` is a small box to try the script on. It has one item at each sharing level, a private state line, a `public` block for wider drops, and a watch word (`Project Lantern`) left inside a file marked shareable, so you can see each safeguard work.

```
python3 research-box/scripts/build_box.py examples/prime-numbers --for shareable --out /tmp/prime-share
python3 research-box/scripts/build_box.py /tmp/prime-share --check
```

The first command keeps the two shareable items, leaves out the rest, and reports the watch word in `code/simulate.py`. The second exits 0 while the copy is untouched. Add `--strict` to the first command and the drop is refused until the watch word is removed.

`harmonic-numbers/` is a second, smaller box. Its command writes `out/results.json`, and the box declares three named numbers in it with tolerances (`"match": "values"`), plus a line of stdout. Use it to see a named-number check reproduce:

```
python3 research-box/scripts/build_box.py examples/harmonic-numbers --for shareable --out /tmp/harmonic
python3 research-box/scripts/build_box.py /tmp/harmonic --reproduce --yes
```
