"""Tests for research-box/scripts/build_box.py. Standard library only.

Run from the repository root:  python3 -m unittest discover tests
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "research-box" / "scripts" / "build_box.py"
EXAMPLE = ROOT / "examples" / "prime-numbers"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


class BuildBoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.box = self.tmp / "box"
        shutil.copytree(EXAMPLE, self.box)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def edit_box(self, change):
        data = json.loads((self.box / "box.json").read_text())
        change(data)
        (self.box / "box.json").write_text(json.dumps(data))

    # Command line
    def test_help_exits_cleanly(self):
        r = run("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage:", r.stdout)

    def test_bare_for_prints_usage(self):
        r = run(self.box, "--for")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    # Drops
    def test_private_drop_in_place_is_allowed(self):
        r = run(self.box)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.box / "manifest-sha256.txt").is_file())
        self.assertTrue((self.box / "tagmanifest-sha256.txt").is_file())

    def test_held_back_drop_in_place_is_refused(self):
        before = sorted(p.name for p in self.box.rglob("*"))
        r = run(self.box, "--for", "shareable")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Nothing was written", r.stderr)
        self.assertEqual(before, sorted(p.name for p in self.box.rglob("*")))

    def test_shareable_copy_holds_only_cleared_items(self):
        out = self.tmp / "share"
        r = run(self.box, "--for", "shareable", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((out / "doc" / "working-notes.md").exists())
        self.assertTrue((out / "sheet" / "results.csv").is_file())
        copy = json.loads((out / "box.json").read_text())
        self.assertEqual([i["item"] for i in copy["items"]], ["Results table", "Simulation"])
        for key in ("private_terms", "public", "level_labels"):
            self.assertNotIn(key, copy)
        box_md = (out / "BOX.md").read_text()
        self.assertNotIn("Working notes", box_md)
        self.assertNotIn("Open question", box_md)
        self.assertIn("Prime-based addressing", box_md)
        self.assertIn("Watch words in code/simulate.py: Project Lantern", r.stdout)

    def test_out_folder_must_be_empty(self):
        out = self.tmp / "share"
        out.mkdir()
        (out / "old.txt").write_text("x")
        r = run(self.box, "--for", "shareable", "--out", out)
        self.assertNotEqual(r.returncode, 0)

    # Paths
    def test_parent_path_is_rejected(self):
        (self.tmp / "secret.txt").write_text("outside the box")
        self.edit_box(lambda d: d["items"][1]["snapshot"].append("../secret.txt"))
        out = self.tmp / "share"
        r = run(self.box, "--for", "shareable", "--out", out)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Nothing was written", r.stderr)
        self.assertFalse(out.exists())

    def test_absolute_path_is_rejected(self):
        self.edit_box(lambda d: d["items"][1]["snapshot"].append(str(self.tmp / "secret.txt")))
        r = run(self.box, "--for", "shareable", "--out", self.tmp / "share")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Nothing was written", r.stderr)

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_symlink_leading_outside_is_rejected(self):
        (self.tmp / "secret.txt").write_text("outside the box")
        (self.box / "sheet" / "link.csv").symlink_to(self.tmp / "secret.txt")
        self.edit_box(lambda d: d["items"][1]["snapshot"].append("sheet/link.csv"))
        r = run(self.box, "--for", "shareable", "--out", self.tmp / "share")
        self.assertEqual(r.returncode, 1)

    # Check
    def make_drop(self):
        out = self.tmp / "share"
        self.assertEqual(run(self.box, "--for", "shareable", "--out", out).returncode, 0)
        return out

    def test_check_passes_on_untouched_drop(self):
        r = run(self.make_drop(), "--check")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_check_fails_on_edited_snapshot(self):
        out = self.make_drop()
        with open(out / "code" / "simulate.py", "a") as f:
            f.write("# edited\n")
        self.assertEqual(run(out, "--check").returncode, 1)

    def test_check_fails_on_edited_box_json(self):
        out = self.make_drop()
        with open(out / "box.json", "a") as f:
            f.write(" ")
        self.assertEqual(run(out, "--check").returncode, 1)

    def test_check_fails_on_extra_file(self):
        out = self.make_drop()
        (out / "code" / "extra.py").write_text("x")
        self.assertEqual(run(out, "--check").returncode, 1)

    def test_check_fails_on_path_outside_folder(self):
        out = self.make_drop()
        with open(out / "manifest-sha256.txt", "a") as f:
            f.write("0" * 64 + "  ../secret.txt\n")
        r = run(out, "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("outside the folder", r.stdout)

    def test_check_without_tag_manifest_is_incomplete(self):
        out = self.make_drop()
        (out / "tagmanifest-sha256.txt").unlink()
        with open(out / "BOX.md", "a") as f:
            f.write("edited\n")
        self.assertEqual(run(out, "--check").returncode, 2)

    # Labels, fields and files (findings from a second review)
    def test_private_state_line_survives_re_export(self):
        first = self.tmp / "private-copy"
        self.assertEqual(run(self.box, "--out", first).returncode, 0)
        second = self.tmp / "assistant-copy"
        r = run(first, "--for", "same-assistant", "--out", second)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Open question", (second / "BOX.md").read_text())
        self.assertNotIn("Open question", (second / "box.json").read_text())

    def test_fields_outside_the_format_are_not_copied(self):
        def add(d):
            d["internal_note"] = "budget"
            d["items"][1]["secret_tag"] = "x"
        self.edit_box(add)
        out = self.tmp / "share"
        r = run(self.box, "--for", "shareable", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        copy = (out / "box.json").read_text()
        self.assertNotIn("internal_note", copy)
        self.assertNotIn("secret_tag", copy)

    def test_fields_outside_the_format_require_out(self):
        self.edit_box(lambda d: d.update(internal_note="budget"))
        r = run(self.box, "--for", "same-assistant")
        self.assertEqual(r.returncode, 1)
        self.assertIn("outside the format", r.stderr)

    def test_missing_snapshot_is_refused(self):
        (self.box / "sheet" / "results.csv").unlink()
        out = self.tmp / "share"
        r = run(self.box, "--for", "shareable", "--out", out)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not found", r.stderr)
        self.assertFalse(out.exists())

    def test_one_file_under_two_levels_is_refused(self):
        self.edit_box(lambda d: d["items"][1]["snapshot"].append("doc/./working-notes.md"))
        out = self.tmp / "share"
        r = run(self.box, "--for", "shareable", "--out", out)
        self.assertEqual(r.returncode, 1)
        self.assertIn("different sharing levels", r.stderr)
        self.assertFalse(out.exists())

    def test_strict_mode_refuses_watch_words(self):
        out = self.tmp / "share"
        r = run(self.box, "--for", "shareable", "--out", out, "--strict")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Strict mode", r.stderr)
        self.assertFalse(out.exists())

    def test_watch_words_are_found_in_generated_files(self):
        self.edit_box(lambda d: d["items"][1].update(notes="from Project Lantern"))
        r = run(self.box, "--for", "shareable", "--out", self.tmp / "share")
        self.assertIn("Watch words in BOX.md", r.stdout)
        self.assertIn("Watch words in box.json", r.stdout)

    def test_check_without_manifest_exits_2(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(run(empty, "--check").returncode, 2)


if __name__ == "__main__":
    unittest.main()
