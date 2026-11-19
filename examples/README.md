# Example box

`prime-numbers/` is a small box to try the script on. It has one item at each sharing level, a private state line, a `public` block for wider drops, and a watch word (`Project Lantern`) left inside a file marked shareable, so you can see each safeguard work.

```
python3 research-box/scripts/build_box.py examples/prime-numbers --for shareable --out /tmp/prime-share
python3 research-box/scripts/build_box.py /tmp/prime-share --check
```

The first command keeps the two shareable items, leaves out the rest, and reports the watch word in `code/simulate.py`. The second exits 0 while the copy is untouched.
