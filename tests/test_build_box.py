"""Tests for research-box/scripts/build_box.py. Standard library only.

Run from the repository root:  python3 -m unittest discover tests
"""
import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
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
open("out.txt", "w", newline="\\n").write("table\\n")
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


SSH_KEYGEN = shutil.which("ssh-keygen")


def run_env(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True, env=env)


class VerifyReportOptionTests(unittest.TestCase):
    """--verify-report command line; needs no ssh-keygen."""

    def test_verify_options_need_verify_report(self):
        r = run(EXAMPLE, "--signer", "alice")
        self.assertEqual(r.returncode, 2)
        self.assertIn("go with --verify-report", r.stderr)

    def test_verify_report_needs_allowed_signers(self):
        r = run(EXAMPLE, "--verify-report", "report.json")
        self.assertEqual(r.returncode, 2)
        self.assertIn("needs --allowed-signers", r.stderr)

    def test_verify_report_stands_alone(self):
        r = run(EXAMPLE, "--verify-report", "r.json", "--allowed-signers", "s", "--check")
        self.assertEqual(r.returncode, 2)
        self.assertIn("on its own", r.stderr)


@unittest.skipUnless(SSH_KEYGEN, "ssh-keygen (OpenSSH 8.2 or later) is not installed")
class VerifyReportTests(unittest.TestCase):
    """Signed reproduction reports: signature, drop, files and verdict must all match."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.box = self.tmp / "box"
        shutil.copytree(EXAMPLE, self.box)
        self.saved_tmpdir = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = str(self.tmp)
        self.out = self.tmp / "drop"
        r = run(self.box, "--for", "shareable", "--out", self.out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.report = self.tmp / "report.json"
        r = run(self.out, "--reproduce", "--yes", "--report", self.report)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.signers = self.tmp / "allowed_signers"
        self.alice = self.key("alice")
        self.add_signer("alice@example.org", self.alice)

    def tearDown(self):
        if self.saved_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = self.saved_tmpdir
        shutil.rmtree(self.tmp)

    def key(self, name):
        path = self.tmp / f"key_{name}"
        subprocess.run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", name,
                        "-f", str(path)], check=True)
        return path

    def add_signer(self, identity, key):
        public = (key.parent / (key.name + ".pub")).read_text().split()[:2]
        with open(self.signers, "a") as f:
            f.write(f'{identity} namespaces="research-box-report" {" ".join(public)}\n')

    def sign(self, key, report=None, namespace="research-box-report"):
        report = report or self.report
        sig = Path(str(report) + ".sig")
        if sig.exists():
            sig.unlink()
        subprocess.run([SSH_KEYGEN, "-q", "-Y", "sign", "-f", str(key), "-n", namespace,
                        str(report)], check=True, capture_output=True)
        return sig

    def verify(self, *extra, folder=None, env=None):
        return run_env(folder or self.out, "--verify-report", self.report,
                       "--allowed-signers", self.signers, *extra, env=env)

    def test_signed_report_verifies(self):
        self.sign(self.alice)
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("Signature: good, by alice@example.org", r.stdout)
        self.assertIn("Result: verified.", r.stdout)

    def test_named_signer_must_match(self):
        self.sign(self.alice)
        self.assertEqual(self.verify("--signer", "alice@example.org").returncode, 0)
        r = self.verify("--signer", "bob@example.org")
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not verify", r.stdout)

    def test_report_edited_after_signing_fails(self):
        self.sign(self.alice)
        data = json.loads(self.report.read_text())
        data["platform"] = "somewhere else"
        self.report.write_text(json.dumps(data, indent=2) + "\n")
        r = self.verify()
        self.assertEqual(r.returncode, 1)
        self.assertIn("Not verified", r.stdout)

    def test_unknown_key_fails(self):
        self.sign(self.key("mallory"))
        r = self.verify()
        self.assertEqual(r.returncode, 1)
        self.assertIn("not made by any key", r.stdout)

    def test_other_namespace_fails(self):
        self.sign(self.alice, namespace="file")
        r = self.verify("--signer", "alice@example.org")
        self.assertEqual(r.returncode, 1)

    def test_report_about_another_drop_fails(self):
        self.sign(self.alice)
        data = json.loads((self.box / "box.json").read_text())
        data["public"]["description"] = "A later version of the box."
        (self.box / "box.json").write_text(json.dumps(data))
        other = self.tmp / "drop2"
        r = run(self.box, "--for", "shareable", "--out", other)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.verify(folder=other)
        self.assertEqual(r.returncode, 1)
        self.assertIn("about another drop", r.stdout)

    def test_edited_drop_fails_before_the_signature(self):
        self.sign(self.alice)
        target = next(p for p in self.out.rglob("*.csv"))
        target.write_text(target.read_text() + "1,2\n")
        r = self.verify()
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match its manifests", r.stdout)
        self.assertNotIn("Signature:", r.stdout)

    def test_signed_failure_is_not_verified(self):
        data = json.loads(self.report.read_text())
        data["verdict"] = "did not reproduce"
        self.report.write_text(json.dumps(data, indent=2) + "\n")
        self.sign(self.alice)
        r = self.verify()
        self.assertEqual(r.returncode, 1)
        self.assertIn("did not reproduce", r.stdout)

    def test_missing_signature_or_report_cannot_be_checked(self):
        r = self.verify()  # nothing signed yet
        self.assertEqual(r.returncode, 2)
        self.assertIn("No signature", r.stdout)
        self.sign(self.alice)
        self.report.write_text("not json")
        self.assertEqual(self.verify().returncode, 2)

    def test_without_ssh_keygen_it_cannot_check(self):
        self.sign(self.alice)
        empty = self.tmp / "no-tools"
        empty.mkdir()
        env = dict(os.environ, PATH=str(empty))
        r = self.verify(env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("ssh-keygen", r.stdout)

    def test_verifying_runs_nothing(self):
        self.sign(self.alice)
        before = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(self.verify().returncode, 0)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), before)


def write_zip(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)


XLSX_STRINGS = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<si><t>{first}</t></si><si><t>{second}</t></si></sst>')
CORE_PROPS = ('<?xml version="1.0" encoding="UTF-8"?>'
              '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
              'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
              '<dc:creator>{creator}</dc:creator></cp:coreProperties>')
DOCX_BODY = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/'
             'main"><w:body><w:p><w:r><w:t>{a}</w:t></w:r><w:r><w:t>{b}</w:t></w:r></w:p>'
             '</w:body></w:document>')


class WatchWordScanTests(unittest.TestCase):
    """Watch words in identifiers, Office files and files that cannot be scanned."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.box = self.tmp / "box"
        shutil.copytree(EXAMPLE, self.box)
        # one unique watch word, and no reproduce block, so only the file under test hits
        data = json.loads((self.box / "box.json").read_text())
        data["private_terms"] = ["Bluefin"]
        data.pop("reproduce", None)
        (self.box / "box.json").write_text(json.dumps(data))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def add_shareable(self, rel, content):
        path = self.box / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        data = json.loads((self.box / "box.json").read_text())
        data["items"].append({"item": rel, "kind": "file", "live": None,
                              "snapshot": [rel], "sharing": "shareable"})
        (self.box / "box.json").write_text(json.dumps(data))

    def strict(self, *extra):
        return run(self.box, "--for", "shareable", "--out", self.tmp / "share", "--strict",
                   *extra)

    def test_identifier_with_underscores_is_caught(self):
        self.add_shareable("code/keys.py", "BLUEFIN_KEY = 1\nbluefin_mode = True\n")
        r = self.strict()
        self.assertEqual(r.returncode, 1)
        self.assertIn("code/keys.py line 1", r.stderr)

    def test_phrase_written_as_identifier_is_caught(self):
        data = json.loads((self.box / "box.json").read_text())
        data["private_terms"] = ["Project Bluefin"]
        (self.box / "box.json").write_text(json.dumps(data))
        self.add_shareable("code/run.py", "use_project_bluefin = False\n")
        r = self.strict()
        self.assertEqual(r.returncode, 1)
        self.assertIn("code/run.py", r.stderr)

    def test_longer_words_still_do_not_match(self):
        self.add_shareable("code/fish.py", "bluefins = 2  # bluefinish\n")
        r = self.strict()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_watch_word_in_spreadsheet_cells_is_caught(self):
        self.add_shareable("sheet/data.xlsx", b"")
        write_zip(self.box / "sheet" / "data.xlsx",
                  {"xl/sharedStrings.xml": XLSX_STRINGS.format(first="Bluefin", second="Tuna")})
        r = self.strict()
        self.assertEqual(r.returncode, 1)
        self.assertIn("sheet/data.xlsx", r.stderr)

    def test_watch_word_in_document_properties_is_caught(self):
        self.add_shareable("doc/paper.docx", b"")
        write_zip(self.box / "doc" / "paper.docx",
                  {"word/document.xml": DOCX_BODY.format(a="Clean ", b="text."),
                   "docProps/core.xml": CORE_PROPS.format(creator="Bluefin team")})
        r = self.strict()
        self.assertEqual(r.returncode, 1)
        self.assertIn("doc/paper.docx", r.stderr)

    def test_word_split_across_runs_is_caught(self):
        self.add_shareable("doc/notes.docx", b"")
        write_zip(self.box / "doc" / "notes.docx",
                  {"word/document.xml": DOCX_BODY.format(a="Blue", b="fin results")})
        self.assertEqual(self.strict().returncode, 1)

    def test_clean_office_file_passes(self):
        self.add_shareable("sheet/clean.xlsx", b"")
        write_zip(self.box / "sheet" / "clean.xlsx",
                  {"xl/sharedStrings.xml": XLSX_STRINGS.format(first="Blue", second="fin")})
        r = self.strict()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Not scanned", r.stdout)

    def test_unscannable_file_needs_allow_unscanned_under_strict(self):
        self.add_shareable("doc/figure.pdf", b"%PDF-1.7\n\xe2\xe3\xcf\xd3 stream\x00\xff")
        r = self.strict()
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not be scanned", r.stderr)
        self.assertIn("doc/figure.pdf", r.stderr)
        self.assertFalse((self.tmp / "share").exists())
        r = self.strict("--allow-unscanned")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Not scanned for watch words", r.stdout)

    def test_unscannable_file_is_only_reported_without_strict(self):
        self.add_shareable("doc/figure.png", b"\x89PNG\r\n\x1a\n\x00\xff")
        r = run(self.box, "--for", "shareable", "--out", self.tmp / "share")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--strict refuses the drop", r.stdout)

    def test_embedded_files_in_office_documents_count_as_unscanned(self):
        self.add_shareable("doc/slides.pptx", b"")
        write_zip(self.box / "doc" / "slides.pptx",
                  {"ppt/slides/slide1.xml": DOCX_BODY.format(a="Clean", b=""),
                   "ppt/media/image1.png": b"\x89PNG\r\n"})
        r = self.strict()
        self.assertEqual(r.returncode, 1)
        self.assertIn("embedded: ppt/media/image1.png", r.stderr)

    def test_broken_office_file_counts_as_unscanned(self):
        self.add_shareable("sheet/broken.xlsx", b"not a zip file")
        r = self.strict()
        self.assertEqual(r.returncode, 1)
        self.assertIn("sheet/broken.xlsx", r.stderr)

    def test_allow_unscanned_needs_strict(self):
        r = run(self.box, "--for", "shareable", "--out", self.tmp / "s", "--allow-unscanned")
        self.assertEqual(r.returncode, 2)
        self.assertIn("goes with --strict", r.stderr)


def load_script():
    spec = importlib.util.spec_from_file_location("build_box", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def can_drop_network():
    exe = shutil.which("unshare")
    if not sys.platform.startswith("linux") or exe is None:
        return False
    r = subprocess.run([exe, "--map-root-user", "--net", "--", sys.executable, "-c", ""],
                       capture_output=True)
    return r.returncode == 0


NETWORK_SCRIPT = """import json, socket
s = socket.socket()
s.settimeout(2)
try:
    s.connect(("127.0.0.1", 9))
    up = 1
except ConnectionRefusedError:
    up = 1
except OSError:
    up = 0
print(json.dumps({"loopback_up": up}))
"""


class ReportClaimTests(unittest.TestCase):
    """What a report records about the run, so a signature on it claims no more than the
    run shows."""

    setUp = ReproduceTests.setUp
    tearDown = ReproduceTests.tearDown
    edit_box = ReproduceTests.edit_box
    drop = ReproduceTests.drop
    add_values_box = ReproduceTests.add_values_box

    def values_report(self):
        self.add_values_box([{"source": "stdout", "match": "values", "values": [
            {"key": "mean_stretch", "equals": 2.84, "abs": 0.01},
            {"key": "false_promotions", "equals": 7}]}])
        out = self.drop()
        report = self.tmp / "report.json"
        r = run(out, "--reproduce", "--yes", "--report", report)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r, json.loads(report.read_text())

    def test_report_states_its_claim_and_conditions(self):
        r, data = self.values_report()
        self.assertIn("does not show that the outputs were computed", data["claim"])
        self.assertEqual(data["network"], "not restricted")
        self.assertTrue(data["tool"].startswith("research-box "))
        self.assertIn("PATH", data["environment"])
        self.assertEqual(data["cleared_outputs"], [])

    def test_values_written_in_the_code_are_flagged(self):
        r, data = self.values_report()
        flagged = {s["output"]: s for s in data["stored_values"]}
        self.assertIn("stdout mean_stretch", flagged)  # 2.8412 is a literal in values.py
        self.assertEqual(flagged["stdout mean_stretch"]["files"], ["code/values.py"])
        self.assertNotIn("stdout false_promotions", flagged)  # 7 is too short to mean much
        self.assertIn("already appear in the box's own files", r.stdout)

    def test_output_file_already_in_the_drop_must_be_written_again(self):
        (self.box / "code" / "quiet.py").write_text("print('done')\n")
        (self.box / "out.txt").write_text("table\n")

        def change(d):
            d["items"].append({"item": "Quiet", "kind": "code", "live": None,
                               "snapshot": ["code/quiet.py", "out.txt"],
                               "sharing": "shareable"})
            d["reproduce"] = {"command": ["python3", "code/quiet.py"], "sharing": "shareable",
                              "expect": [{"source": "out.txt", "match": "sha256",
                                          "value": hashlib.sha256(b"table\n").hexdigest()}]}
        self.edit_box(change)
        out = self.drop()
        report = self.tmp / "report.json"
        r = run(out, "--reproduce", "--yes", "--report", report)
        self.assertEqual(r.returncode, 1, r.stdout)
        data = json.loads(report.read_text())
        self.assertEqual(data["cleared_outputs"], ["out.txt"])
        self.assertTrue((out / "out.txt").is_file())  # the drop itself is untouched
        self.assertEqual(run(out, "--check").returncode, 0)

    def test_no_network_goes_with_reproduce(self):
        r = run(self.box, "--no-network")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--no-network", r.stderr)

    @unittest.skipUnless(can_drop_network(), "no unprivileged network namespaces here")
    def test_no_network_runs_without_a_network(self):
        (self.box / "code" / "net.py").write_text(NETWORK_SCRIPT)

        def change(d):
            d["items"].append({"item": "Net", "kind": "code", "live": None,
                               "snapshot": ["code/net.py"], "sharing": "shareable"})
            d["reproduce"] = {"command": ["python3", "code/net.py"], "sharing": "shareable",
                              "expect": [{"source": "stdout", "match": "values", "values": [
                                  {"key": "loopback_up", "equals": 0}]}]}
        self.edit_box(change)
        out = self.drop()
        report = self.tmp / "report.json"
        r = run(out, "--reproduce", "--yes", "--no-network", "--report", report)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(report.read_text())["network"],
                         "none (Linux network namespace)")
        r = run(out, "--reproduce", "--yes")
        self.assertEqual(r.returncode, 1, r.stdout)  # with a network, loopback is up

    @unittest.skipIf(can_drop_network(), "network namespaces are available here")
    def test_no_network_refuses_where_it_cannot_cut_the_network(self):
        out = self.drop()
        r = run(out, "--reproduce", "--yes", "--no-network")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Nothing was run.", r.stdout)


def tree_root(module, leaves):
    """RFC 6962 Merkle tree hash, written out the slow way to check the script's proofs."""
    if len(leaves) == 1:
        return module.leaf_hash(leaves[0])
    k = 1 << ((len(leaves) - 1).bit_length() - 1)
    return module.node_hash(tree_root(module, leaves[:k]), tree_root(module, leaves[k:]))


def tree_path(module, m, leaves):
    if len(leaves) == 1:
        return []
    k = 1 << ((len(leaves) - 1).bit_length() - 1)
    if m < k:
        return tree_path(module, m, leaves[:k]) + [tree_root(module, leaves[k:])]
    return tree_path(module, m - k, leaves[k:]) + [tree_root(module, leaves[:k])]


class MerkleTests(unittest.TestCase):
    def test_every_inclusion_proof_in_small_trees(self):
        m = load_script()
        for size in range(1, 34):
            leaves = [f"leaf {i}".encode() for i in range(size)]
            root = tree_root(m, leaves)
            for i in range(size):
                path = tree_path(m, i, leaves)
                self.assertEqual(m.root_from_inclusion(i, size, m.leaf_hash(leaves[i]), path),
                                 root, (size, i))
                if size > 1:
                    self.assertNotEqual(m.root_from_inclusion(
                        i, size, m.leaf_hash(b"other"), path), root)

    def test_bad_positions_are_refused(self):
        m = load_script()
        leaf = m.leaf_hash(b"x")
        self.assertIsNone(m.root_from_inclusion(3, 3, leaf, []))
        self.assertIsNone(m.root_from_inclusion(0, 4, leaf, [leaf]))  # path too short


@unittest.skipUnless(SSH_KEYGEN, "ssh-keygen (OpenSSH 8.2 or later) is not installed")
class LogProofTests(unittest.TestCase):
    """--log-proof: the signed report must sit in a signed transparency-log checkpoint."""

    setUp = VerifyReportTests.setUp
    tearDown = VerifyReportTests.tearDown
    key = VerifyReportTests.key
    add_signer = VerifyReportTests.add_signer
    sign = VerifyReportTests.sign
    verify = VerifyReportTests.verify

    def make_proof(self, log_key, index=3, size=5, origin="log.example/research-box",
                   namespace="research-box-log", report_bytes=None):
        m = load_script()
        sig = self.sign(self.alice)
        mine = m.report_leaf(report_bytes or self.report.read_bytes(), sig.read_bytes())
        leaves = [f"other report {i}".encode() for i in range(size)]
        leaves[index] = mine
        root = tree_root(m, leaves)
        checkpoint = f"{origin}\n{size}\n{base64.b64encode(root).decode()}\n"
        cp = self.tmp / "checkpoint"
        cp.write_text(checkpoint, newline="\n")
        subprocess.run([SSH_KEYGEN, "-q", "-Y", "sign", "-f", str(log_key), "-n", namespace,
                        str(cp)], check=True, capture_output=True)
        proof = {"log": origin, "leaf_index": index, "tree_size": size,
                 "inclusion": [h.hex() for h in tree_path(m, index, leaves)],
                 "checkpoint": checkpoint,
                 "checkpoint_signature": Path(str(cp) + ".sig").read_text()}
        path = self.tmp / "proof.json"
        path.write_text(json.dumps(proof))
        return path

    def log_signers(self, key):
        public = (key.parent / (key.name + ".pub")).read_text().split()[:2]
        path = self.tmp / "log_signers"
        path.write_text(f'log@example.org namespaces="research-box-log" {" ".join(public)}\n')
        return path

    def test_logged_report_verifies(self):
        log = self.key("log")
        proof = self.make_proof(log)
        r = self.verify("--log-proof", proof, "--log-signers", self.log_signers(log))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("entry 3 of 5", r.stdout)
        self.assertIn("logged as", r.stdout)

    def test_without_a_proof_the_times_are_the_signers_word(self):
        self.sign(self.alice)
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("no log proof given", r.stdout)
        self.assertIn("What the report claims:", r.stdout)

    def test_proof_for_another_report_fails(self):
        log = self.key("log")
        proof = self.make_proof(log, report_bytes=b"{}")
        r = self.verify("--log-proof", proof, "--log-signers", self.log_signers(log))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("not in the log", r.stdout)

    def test_checkpoint_from_an_unknown_log_key_fails(self):
        proof = self.make_proof(self.key("rogue"))
        r = self.verify("--log-proof", proof, "--log-signers", self.log_signers(self.key("log")))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("checkpoint signature", r.stdout)

    def test_checkpoint_signed_for_another_purpose_fails(self):
        log = self.key("log")
        proof = self.make_proof(log, namespace="file")
        r = self.verify("--log-proof", proof, "--log-signers", self.log_signers(log))
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_changed_tree_size_fails(self):
        log = self.key("log")
        proof = self.make_proof(log)
        data = json.loads(proof.read_text())
        data["tree_size"] = 6
        proof.write_text(json.dumps(data))
        r = self.verify("--log-proof", proof, "--log-signers", self.log_signers(log))
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_log_proof_needs_log_signers(self):
        self.sign(self.alice)
        r = self.verify("--log-proof", self.tmp / "proof.json")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--log-signers", r.stderr)


class RepositoryTests(unittest.TestCase):
    """Packaging and output conventions."""

    def test_versions_agree(self):
        skill = (ROOT / "research-box" / "SKILL.md").read_text()
        skill_version = skill.split('version: "', 1)[1].split('"', 1)[0]
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        readme = (ROOT / "README.md").read_text()
        latest = readme.split("## Versions", 1)[1].split("- **", 1)[1].split("**", 1)[0]
        self.assertEqual(plugin["version"], skill_version)
        self.assertEqual(latest, skill_version)
        for entry in market["plugins"]:  # plugin.json is the single source of the version
            self.assertNotIn("version", entry)

    def test_plugin_icon_sits_outside_the_manifest_folder(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        icon = plugin["icon"]
        self.assertFalse(icon.lstrip("./").startswith(".claude-plugin"))
        self.assertTrue((ROOT / icon).is_file())

    def test_license_and_notice_copies_match(self):
        for name in ("LICENSE", "NOTICE"):
            self.assertEqual((ROOT / name).read_bytes(),
                             (ROOT / "research-box" / name).read_bytes(), name)

    def test_generated_files_use_lf_and_posix_paths(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            out = tmp / "drop"
            r = run(EXAMPLE, "--for", "shareable", "--out", out)
            self.assertEqual(r.returncode, 0, r.stderr)
            for name in ("BOX.md", "box.json", "ro-crate-metadata.json",
                         "manifest-sha256.txt", "tagmanifest-sha256.txt"):
                self.assertNotIn(b"\r\n", (out / name).read_bytes(), name)
            self.assertNotIn("\\", (out / "manifest-sha256.txt").read_text())
            self.assertEqual(run(out, "--check").returncode, 0)
        finally:
            shutil.rmtree(tmp)

    def test_non_empty_out_folder_is_refused(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            out = tmp / "drop"
            out.mkdir()
            (out / "leftover.txt").write_text("from an earlier, wider drop")
            r = run(EXAMPLE, "--for", "shareable", "--out", out)
            self.assertEqual(r.returncode, 1)
            self.assertIn("is not empty", r.stderr)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
