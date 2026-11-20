"""Tests for research-box/scripts/build_box.py. Standard library only.

Run from the repository root:  python3 -m unittest discover tests
"""
import hashlib
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
        self.assertIn("Watch words in code/simulate.py line 1: Project Lantern", r.stdout)

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

    def test_watch_word_does_not_match_inside_other_words(self):
        # "ODE" is inside "code", which every drop of this box writes (an item's kind).
        self.edit_box(lambda d: d.update(private_terms=["ODE"]))
        r = run(self.box, "--for", "shareable", "--out", self.tmp / "share", "--strict")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Watch words", r.stdout)

    def test_watch_word_matches_as_a_whole_word(self):
        self.edit_box(lambda d: d.update(private_terms=["ODE"]))
        with open(self.box / "code" / "simulate.py", "a") as f:
            f.write("# solve the ODE numerically\n")
        r = run(self.box, "--for", "shareable", "--out", self.tmp / "share", "--strict")
        self.assertEqual(r.returncode, 1)
        self.assertIn("code/simulate.py line", r.stderr)
        self.assertIn("solve the ODE numerically", r.stderr)

    def test_watch_phrase_is_found_across_a_line_break(self):
        with open(self.box / "sheet" / "results.csv", "a") as f:
            f.write("notes from Project\nLantern work\n")
        # leave only the CSV hit (and drop the reproduce block, which runs simulate.py)
        self.edit_box(lambda d: (d["items"][2].update(snapshot=[]), d.pop("reproduce")))
        r = run(self.box, "--for", "shareable", "--out", self.tmp / "share", "--strict")
        self.assertEqual(r.returncode, 1)
        self.assertIn("sheet/results.csv", r.stderr)

    def test_held_back_state_line_is_reported(self):
        r = run(self.box, "--for", "same-assistant", "--out", self.tmp / "assistant")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Held back for same-assistant readers: 1 state line(s)", r.stdout)

    def test_watch_words_are_found_in_generated_files(self):
        self.edit_box(lambda d: d["items"][1].update(notes="from Project Lantern"))
        r = run(self.box, "--for", "shareable", "--out", self.tmp / "share")
        self.assertIn("Watch words in BOX.md", r.stdout)
        self.assertIn("Watch words in box.json", r.stdout)

    def test_check_without_manifest_exits_2(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(run(empty, "--check").returncode, 2)


def run_input(*args, stdin=""):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True, input=stdin)


VALUES_SCRIPT = """import json, sys
data = json.load(open("data/params.json"))
out = {"mean_stretch": 2.8412, "false_promotions": 7, "rows": [{"n": data["n"]}]}
json.dump(out, open("out.json", "w"))
open("out.txt", "w").write("table\\n")
print(json.dumps(out))
"""


class ReproduceTests(unittest.TestCase):
    """reproduce on open: one declared command, checked at drop time, run only on a yes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.box = self.tmp / "box"
        shutil.copytree(EXAMPLE, self.box)
        # Runs go to a temporary folder the script never deletes; keep them in self.tmp.
        self.saved_tmpdir = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = str(self.tmp)

    def tearDown(self):
        if self.saved_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = self.saved_tmpdir
        shutil.rmtree(self.tmp)

    def edit_box(self, change):
        data = json.loads((self.box / "box.json").read_text())
        change(data)
        (self.box / "box.json").write_text(json.dumps(data))

    def drop(self, audience="shareable", name="drop"):
        out = self.tmp / name
        r = run(self.box, "--for", audience, "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        return out

    def add_values_box(self, expect):
        """Add a script that writes JSON and a data file it reads, both shareable."""
        (self.box / "code" / "values.py").write_text(VALUES_SCRIPT)
        (self.box / "data").mkdir()
        (self.box / "data" / "params.json").write_text('{"n": 1024}')

        def change(d):
            d["items"].append({"item": "Values", "kind": "code", "live": None,
                               "snapshot": ["code/values.py", "data/params.json"],
                               "sharing": "shareable"})
            d["reproduce"] = {"command": ["python3", "code/values.py"],
                              "inputs": ["data/params.json"], "sharing": "shareable",
                              "expect": expect}
        self.edit_box(change)

    def test_block_is_kept_in_cleared_drops(self):  # 1
        for audience in ("same-assistant", "shareable"):
            out = self.drop(audience, f"drop-{audience}")
            copy = json.loads((out / "box.json").read_text())
            self.assertEqual(copy["reproduce"]["command"], ["python3", "code/simulate.py"])
            self.assertIn("## Reproduce", (out / "BOX.md").read_text())

    def test_narrower_block_is_held_back_and_needs_out(self):  # 2
        self.edit_box(lambda d: d["reproduce"].update(sharing="same-assistant"))
        self.edit_box(lambda d: d.pop("public"))  # so the block is the only thing held back
        self.edit_box(lambda d: d.update(default_sharing="shareable", state=[]))
        self.edit_box(lambda d: [it.update(sharing="shareable") for it in d["items"]])
        r = run(self.box, "--for", "shareable")
        self.assertEqual(r.returncode, 1)
        self.assertIn("the reproduce command", r.stderr)
        out = self.drop()
        self.assertNotIn("reproduce", json.loads((out / "box.json").read_text()))
        self.assertNotIn("## Reproduce", (out / "BOX.md").read_text())

    def test_cleared_block_running_a_held_back_file_is_refused(self):  # 3
        self.edit_box(lambda d: d["items"][2].update(sharing="private"))
        out = self.tmp / "drop"
        r = run(self.box, "--for", "shareable", "--out", out)
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs code/simulate.py", r.stderr)
        self.assertFalse(out.exists())

    def test_paths_leaving_the_box_are_refused(self):  # 4
        for bad in ("/Users/someone/run.py", "~/run.py", "../run.py", "--data=../x"):
            self.edit_box(lambda d: d["reproduce"].update(command=["python3", bad]))
            r = run(self.box, "--for", "shareable", "--out", self.tmp / "drop")
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("reproduce.command", r.stderr)
        self.edit_box(lambda d: d["reproduce"].update(
            command=["python3", "code/simulate.py"],
            expect=[{"source": "/etc/passwd", "match": "text", "value": "x"}]))
        r = run(self.box, "--for", "shareable", "--out", self.tmp / "drop")
        self.assertEqual(r.returncode, 1)
        self.assertIn("source", r.stderr)
        self.assertFalse((self.tmp / "drop").exists())

    def test_example_box_reproduces(self):  # 5
        r = run(self.drop(), "--reproduce", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Result: reproduced", r.stdout)

    def test_wrong_expected_value_does_not_reproduce(self):  # 6
        self.edit_box(lambda d: d["reproduce"]["expect"][0].update(value="[1, 2]"))
        r = run(self.drop(), "--reproduce", "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("expected [1, 2]; observed [487, 267]", r.stdout)

    def test_values_tolerances_and_bounds(self):  # 7
        cases = [
            ({"key": "mean_stretch", "equals": 2.84, "abs": 0.01}, 0),
            ({"key": "mean_stretch", "equals": 2.84, "abs": 0.001}, 1),
            ({"key": "mean_stretch", "equals": 2.84, "rel": 0.001}, 0),
            ({"key": "mean_stretch", "equals": 2.84, "rel": 0.0001}, 1),
            ({"key": "false_promotions", "at_most": 9}, 0),
            ({"key": "false_promotions", "at_most": 6}, 1),
            ({"key": "false_promotions", "at_least": 7}, 0),
            ({"key": "false_promotions", "at_least": 8}, 1),
            ({"key": "rows.0.n", "equals": 1024}, 0),
            ({"key": "no_such_key", "equals": 1}, 1),
        ]
        for i, (rule, code) in enumerate(cases):
            for source in ("stdout", "out.json"):
                self.tearDown()
                self.setUp()
                self.add_values_box([{"source": source, "match": "values", "values": [rule]}])
                r = run(self.drop(), "--reproduce", "--yes")
                self.assertEqual(r.returncode, code, f"{rule} from {source}: {r.stdout}")

    def test_edited_drop_is_not_run(self):  # 8
        out = self.drop()
        with open(out / "sheet" / "results.csv", "a") as f:
            f.write("edited\n")
        r = run(out, "--reproduce", "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Nothing was run", r.stdout)
        self.assertNotIn("Result:", r.stdout)
        self.assertEqual(list(self.tmp.glob("research-box-run-*")), [])

    def test_timeout_is_inconclusive(self):  # 9
        (self.box / "code" / "simulate.py").write_text("import time\ntime.sleep(30)\n")
        r = run(self.drop(), "--reproduce", "--yes", "--timeout", "1")
        self.assertEqual(r.returncode, 2)
        self.assertIn("did not finish within 1 seconds", r.stdout)

    def test_outputs_stay_out_of_the_drop(self):  # 10
        self.add_values_box([{"source": "out.txt", "match": "sha256",
                              "value": hashlib.sha256(b"table\n").hexdigest()}])
        out = self.drop()
        r = run(out, "--reproduce", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((out / "out.json").exists())
        self.assertFalse((out / "out.txt").exists())
        self.assertEqual(run(out, "--check").returncode, 0)

    def test_nothing_runs_without_a_yes(self):  # 11
        out = self.drop()
        for answer in ("", "n\n"):
            r = run_input(out, "--reproduce", stdin=answer)
            self.assertEqual(r.returncode, 2)
            self.assertIn("Not run.", r.stdout)
        r = run_input(out, "--reproduce", stdin="y\n")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_watch_word_in_the_command_is_caught(self):  # 12
        self.edit_box(lambda d: d["reproduce"].update(name="Project Lantern rerun"))
        r = run(self.box, "--for", "shareable", "--out", self.tmp / "loose")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Watch words in BOX.md", r.stdout)
        self.assertIn("Watch words in box.json", r.stdout)
        r = run(self.box, "--for", "shareable", "--out", self.tmp / "strict", "--strict")
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.tmp / "strict").exists())

    def test_report_is_written_outside_the_drop(self):  # 13
        out = self.drop()
        r = run(out, "--reproduce", "--yes", "--report", out / "report.json")
        self.assertEqual(r.returncode, 2)
        self.assertFalse((out / "report.json").exists())
        report = self.tmp / "report.json"
        r = run(out, "--reproduce", "--yes", "--report", report)
        self.assertEqual(r.returncode, 0, r.stdout)
        data = json.loads(report.read_text())
        drop_id = "sha256:" + hashlib.sha256(
            (out / "tagmanifest-sha256.txt").read_bytes()).hexdigest()
        self.assertEqual(data["drop_id"], drop_id)
        self.assertEqual(data["verdict"], "reproduced")
        self.assertEqual(data["run_folder"], "<tmp>")
        for key in ("box", "dropped", "command", "platform", "python", "started", "ended",
                    "exit_code", "expectations"):
            self.assertIn(key, data)
        self.assertNotIn(str(self.tmp), report.read_text())

    def test_ro_crate_records_the_run(self):  # 14
        self.add_values_box([{"source": "stdout", "match": "values",
                              "values": [{"key": "false_promotions", "at_most": 9}]}])
        out = self.drop()
        graph = json.loads((out / "ro-crate-metadata.json").read_text())["@graph"]
        by_id = {e["@id"]: e for e in graph}
        action = by_id["#reproduce"]
        self.assertEqual(action["@type"], "CreateAction")
        self.assertEqual(action["instrument"], {"@id": "code/values.py"})
        self.assertEqual(action["object"], [{"@id": "data/params.json"}])
        expected = by_id[action["result"][0]["@id"]]
        self.assertEqual(expected["@type"], "PropertyValue")
        self.assertEqual(expected["maxValue"], 9)
        self.assertEqual(by_id["./"]["mentions"], [{"@id": "#reproduce"}])

    def test_reproduce_without_a_block_or_manifest_is_not_run(self):
        self.edit_box(lambda d: d.pop("reproduce"))
        r = run(self.drop(), "--reproduce", "--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("declares no reproduce command", r.stdout)
        r = run(self.box, "--reproduce", "--yes")  # source box: no manifest yet
        self.assertEqual(r.returncode, 2)

    def test_reproduce_options_need_reproduce(self):
        r = run(self.box, "--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("go with --reproduce", r.stderr)


if __name__ == "__main__":
    unittest.main()
