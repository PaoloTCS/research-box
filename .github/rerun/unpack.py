#!/usr/bin/env python3
"""Unpack a drop archive safely: regular files and folders only, nothing outside the target,
and exactly one top-level folder holding box.json. Usage: unpack.py ARCHIVE TARGET_FOLDER"""
import shutil
import sys
import tarfile
from pathlib import Path

LIMIT = 500 * 1024 * 1024  # unpacked bytes


def main(argv):
    archive, target = Path(argv[1]), Path(argv[2])
    staging = target.with_name(target.name + ".unpack")
    staging.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if sum(m.size for m in members) > LIMIT:
            sys.exit("The archive unpacks to more than 500 MB.")
        if any(not (m.isfile() or m.isdir()) for m in members):
            sys.exit("The archive holds links or special files; a drop has only files.")
        tar.extractall(staging, filter="data")
    tops = [p for p in staging.iterdir()]
    if len(tops) == 1 and tops[0].is_dir() and (tops[0] / "box.json").is_file():
        shutil.move(str(tops[0]), str(target))
    elif (staging / "box.json").is_file():
        shutil.move(str(staging), str(target))
    else:
        sys.exit("The archive holds no drop: expected one folder with box.json in it.")
    shutil.rmtree(staging, ignore_errors=True)
    print(f"Unpacked to {target}.")


if __name__ == "__main__":
    main(sys.argv)
