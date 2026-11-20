"""Tests for the GitHub Actions rerun: the safe unpacker, the report assembler that runs in
the signing job, and --verify-report --attested-by (with a stand-in for the gh CLI)."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "research-box" / "scripts" / "build_box.py"
UNPACK = ROOT / ".github" / "rerun" / "unpack.py"
ASSEMBLE = ROOT / ".github" / "rerun" / "assemble.py"
INDEX = ROOT / ".github" / "rerun" / "index.py"


def run(script, *args, env=None):
    return subprocess.run([sys.executable, str(script), *map(str, args)],
                          capture_output=True, text=True, env=env)


class RerunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.drop = cls.tmp / "drop" / "prime-numbers"
        out = run(SCRIPT, ROOT / "examples" / "prime-numbers", "--for", "shareable",
                  "--out", cls.drop)
        assert out.returncode == 0, out.stdout + out.stderr
        cls.runs = cls.tmp / "runs"
        cls.runs.mkdir()
        for i in (1, 2, 3):
            out = run(SCRIPT, cls.drop, "--reproduce", "--yes", "--report",
                      cls.runs / f"run-{i}.json")
            assert out.returncode == 0, out.stdout + out.stderr
            # These tests check the assembler, not the sandbox, so mark the runs as the
            # workflow's would be marked.
            path = cls.runs / f"run-{i}.json"
            data = json.loads(path.read_text())
            data["network"] = "none (Linux network namespace)"
            path.write_text(json.dumps(data))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def assemble(self, runs, out, *extra):
        return run(ASSEMBLE, "--drop", self.drop, "--runs", runs, "--out", out,
                   "--repository", "owner/repo", "--workflow-run", "https://example.org/run",
                   "--workflow-sha", "abc", "--source-url", "https://example.org/d.tar.gz",
                   "--source-sha256", "0" * 64, *extra)

    def test_signer_workflow_defaults_to_the_repositorys_rerun(self):
        out = self.tmp / "out-default-signer"
        self.assertEqual(self.assemble(self.runs, out).returncode, 0)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["runner"]["signer_workflow"],
                         "owner/repo/.github/workflows/rerun.yml")

    def test_rerun_in_your_own_repository_names_the_shared_workflow(self):
        out = self.tmp / "out-own-repo"
        signer = "PaoloTCS/research-box/.github/workflows/rerun-own.yml"
        result = self.assemble(self.runs, out, "--signer-workflow", signer)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["runner"]["repository"], "owner/repo")
        self.assertEqual(report["runner"]["signer_workflow"], signer)
        page = (out / "certificate.html").read_text()
        self.assertIn(f"--signer-workflow {signer}", page)
        self.assertIn("--attested-by owner/repo \\\n  --signer-workflow", page)

    def tampered(self, change):
        runs = Path(tempfile.mkdtemp(dir=self.tmp))
        for path in self.runs.iterdir():
            shutil.copy(path, runs / path.name)
        path = runs / "run-2.json"
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))
        return runs

    def test_assembles_three_good_runs(self):
        out = self.tmp / "out-good"
        result = self.assemble(self.runs, out)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["verdict"], "reproduced")
        self.assertEqual(report["runner"]["runs"], 3)
        self.assertTrue(report["runner"]["observed_identical"])
        page = (out / "certificate.html").read_text()
        self.assertIn("Declared outputs reproduced", page)
        self.assertIn("Anthropic does not run or endorse", page)

    def test_refuses_runs_that_disagree_with_the_drop(self):
        cases = {
            "ran with a network": lambda d: d.update(network="not restricted"),
            "names another drop": lambda d: d.update(drop_id="sha256:" + "1" * 64),
            "names another command": lambda d: d.update(command=["python3", "other.py"]),
            "other expectations": lambda d: d["expectations"][0].update(expected="[1, 2]"),
            "did not match": lambda d: d["expectations"][0].update(matched=False),
            "another claim": lambda d: d.update(claim="It is all true."),
        }
        for words, change in cases.items():
            with self.subTest(words):
                result = self.assemble(self.tampered(change), self.tmp / "out-bad")
                self.assertEqual(result.returncode, 1)
                self.assertIn(words, result.stdout)
                self.assertIn("Nothing to sign", result.stdout)

    def test_a_failed_run_is_reported_not_hidden(self):
        def failed(d):
            d["verdict"] = "did not reproduce"
            d["expectations"][0].update(matched=False, observed="[1, 1]")
        out = self.tmp / "out-failed"
        result = self.assemble(self.tampered(failed), out)
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["verdict"], "did not reproduce")
        self.assertFalse(report["runner"]["all_reproduced"])

    def test_index_lists_reruns(self):
        out = self.tmp / "out-index"
        self.assemble(self.runs, out)
        site = self.tmp / "site"
        (site / "r" / "7").mkdir(parents=True)
        for f in out.iterdir():
            shutil.copy(f, site / "r" / "7" / f.name)
        result = run(INDEX, site)
        self.assertEqual(result.returncode, 0, result.stderr)
        entries = json.loads((site / "index.json").read_text())
        self.assertEqual(entries[0]["path"], "r/7/")
        self.assertIn("r/7/certificate.html", (site / "index.html").read_text())
        self.assertIn("Content-Security-Policy", (site / "_headers").read_text())

    def test_unpack_takes_a_drop_and_refuses_links(self):
        good = self.tmp / "good.tar.gz"
        with tarfile.open(good, "w:gz") as tar:
            tar.add(self.drop, arcname="prime-numbers")
        target = self.tmp / "unpacked"
        result = run(UNPACK, good, target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((target / "box.json").is_file())

        bad = self.tmp / "bad.tar.gz"
        with tarfile.open(bad, "w:gz") as tar:
            tar.add(self.drop, arcname="prime-numbers")
            info = tarfile.TarInfo("prime-numbers/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        result = run(UNPACK, bad, self.tmp / "unpacked-bad")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("links or special files", result.stderr)


@unittest.skipIf(os.name == "nt", "the gh stand-in is a shell script")
class AttestedVerifyTests(unittest.TestCase):
    """--verify-report --attested-by, with a stand-in gh that passes or fails."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.drop = cls.tmp / "prime-numbers"
        run(SCRIPT, ROOT / "examples" / "prime-numbers", "--for", "shareable", "--out", cls.drop)
        cls.report = cls.tmp / "report.json"
        out = run(SCRIPT, cls.drop, "--reproduce", "--yes", "--report", cls.report)
        assert out.returncode == 0, out.stdout + out.stderr
        cls.bin = cls.tmp / "bin"
        cls.bin.mkdir()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def stand_in(self, code):
        gh = self.bin / "gh"
        gh.write_text(f"#!/bin/sh\necho \"$@\" > {self.tmp / 'gh-args'}\nexit {code}\n")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        return dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}")

    def test_good_attestation_verifies(self):
        env = self.stand_in(0)
        result = run(SCRIPT, self.drop, "--verify-report", self.report,
                     "--attested-by", "owner/repo", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Attestation: good", result.stdout)
        self.assertIn("Sigstore public transparency log", result.stdout)
        args = (self.tmp / "gh-args").read_text()
        self.assertIn("--signer-workflow owner/repo/.github/workflows/rerun.yml", args)

    def test_bad_attestation_does_not_verify(self):
        env = self.stand_in(1)
        result = run(SCRIPT, self.drop, "--verify-report", self.report,
                     "--attested-by", "owner/repo", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Not verified", result.stdout)

    def test_signer_workflow_is_passed_to_gh(self):
        env = self.stand_in(0)
        signer = "PaoloTCS/research-box/.github/workflows/rerun-own.yml"
        result = run(SCRIPT, self.drop, "--verify-report", self.report,
                     "--attested-by", "owner/repo", "--signer-workflow", signer, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = (self.tmp / "gh-args").read_text()
        self.assertIn("--repo owner/repo", args)
        self.assertIn(f"--signer-workflow {signer}", args)

    def test_signer_workflow_must_be_a_workflow_path(self):
        result = run(SCRIPT, self.drop, "--verify-report", self.report,
                     "--attested-by", "owner/repo", "--signer-workflow", "owner/repo")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--signer-workflow takes", result.stderr)

    def test_attested_by_does_not_mix_with_ssh_options(self):
        result = run(SCRIPT, self.drop, "--verify-report", self.report,
                     "--attested-by", "owner/repo", "--allowed-signers", self.report)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--attested-by replaces", result.stderr)


if __name__ == "__main__":
    unittest.main()


REQUEST = ROOT / ".github" / "rerun" / "request.py"
REPLY = ROOT / ".github" / "rerun" / "reply.py"


class NamedNumberRerunTests(unittest.TestCase):
    """The second example box declares named numbers in a JSON file; the assembler must
    accept its runs (it once read those checks as a single "not JSON" row)."""

    def test_assembles_named_numbers(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            drop = tmp / "harmonic-numbers"
            out = run(SCRIPT, ROOT / "examples" / "harmonic-numbers", "--for", "shareable",
                      "--out", drop)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            runs = tmp / "runs"
            runs.mkdir()
            for i in (1, 2, 3):
                path = runs / f"run-{i}.json"
                out = run(SCRIPT, drop, "--reproduce", "--yes", "--report", path)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                data = json.loads(path.read_text())
                data["network"] = "none (Linux network namespace)"
                path.write_text(json.dumps(data))
            out = run(ASSEMBLE, "--drop", drop, "--runs", runs, "--out", tmp / "out",
                      "--repository", "owner/repo", "--workflow-run", "https://example.org/run",
                      "--workflow-sha", "abc", "--source-url", "https://example.org/d.tar.gz",
                      "--source-sha256", "0" * 64)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            report = json.loads((tmp / "out" / "report.json").read_text())
            self.assertEqual(report["verdict"], "reproduced")
            self.assertEqual(len(report["expectations"]), 4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def form(url="https://example.org/drop.tar.gz", sha="a" * 64, name="_No response_"):
    return (f"### Drop address\n\n{url}\n\n### Archive sha256\n\n{sha}\n\n"
            f"### Name or ORCID to show on the report (optional)\n\n{name}\n\n"
            f"### Public\n\n- [X] I understand\n")


class RequestTests(unittest.TestCase):
    def decide(self, body=None, issues=(), **env):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "issues.json").write_text(json.dumps(list(issues)))
            base = {"BODY": form() if body is None else body, "ISSUE": "7", "AUTHOR": "alice",
                    "EVENT_ACTION": "opened", "AUTHOR_CREATED": "2020-01-01T00:00:00Z",
                    "RUNS_TODAY": "0", "PATH": os.environ.get("PATH", "")}
            # Windows Python 3.10 cannot start without SYSTEMROOT (it seeds its hash from it).
            base.update({k: os.environ[k] for k in ("SYSTEMROOT", "SystemRoot") if k in os.environ})
            base.update(env)
            out = run(REQUEST, "--author-issues", tmp / "issues.json",
                      "--comment", tmp / "comment.md", env=base)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            return json.loads(out.stdout), (tmp / "comment.md").read_text()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_good_request_runs(self):
        d, comment = self.decide()
        self.assertEqual(d["action"], "run")
        self.assertEqual(d["drop_sha256"], "a" * 64)
        self.assertEqual(d["submitter"], "@alice")
        self.assertIn("Rerun started", comment)

    def test_other_issues_are_ignored(self):
        self.assertEqual(self.decide(body="Just a bug report")[0]["action"], "ignore")

    def test_bad_address_and_sha_are_refused(self):
        for body in (form(url="http://example.org/x"), form(url="https://a.org/x\nhttps://b.org/y"),
                     form(url="file:///etc/passwd"), form(sha="xyz"),
                     form(url="https://example.org/$(id)")):
            self.assertEqual(self.decide(body=body)[0]["action"], "refuse", body)

    def test_new_accounts_and_limits_are_refused(self):
        d, comment = self.decide(AUTHOR_CREATED="2099-01-01T00:00:00Z")
        self.assertEqual(d["action"], "refuse")
        recent = [{"number": n, "body": form(), "created_at": "2099-01-01T00:00:00Z"}
                  for n in (1, 2, 3)]
        self.assertEqual(self.decide(issues=recent)[0]["action"], "refuse")
        self.assertEqual(self.decide(issues=recent[:2])[0]["action"], "run")
        self.assertEqual(self.decide(RUNS_TODAY="20")[0]["action"], "refuse")

    def test_maintainer_mode_waits_for_a_maintainers_label(self):
        self.assertEqual(self.decide(APPROVAL="maintainer")[0]["action"], "wait")
        labeled = dict(APPROVAL="maintainer", EVENT_ACTION="labeled", LABEL="rerun-approved")
        self.assertEqual(self.decide(SENDER_PERMISSION="admin", **labeled)[0]["action"], "run")
        self.assertEqual(self.decide(SENDER_PERMISSION="read", **labeled)[0]["action"], "ignore")
        self.assertEqual(self.decide(EVENT_ACTION="labeled", LABEL="bug",
                                     SENDER_PERMISSION="admin")[0]["action"], "ignore")

    def test_submitter_text_is_kept_plain(self):
        d, _ = self.decide(body=form(name="0000-0002-1825-0097"))
        self.assertEqual(d["submitter"], "ORCID 0000-0002-1825-0097 (@alice)")
        d, _ = self.decide(body=form(name="Ada Lovelace"))
        self.assertEqual(d["submitter"], "Ada Lovelace (@alice)")
        d, _ = self.decide(body=form(name="@everyone <script>"))
        self.assertEqual(d["submitter"], "@alice")


class ReplyTests(unittest.TestCase):
    def reply(self, report, dry="false"):
        tmp = Path(tempfile.mkdtemp())
        try:
            path = ""
            if report is not None:
                path = tmp / "report.json"
                path.write_text(json.dumps(report))
            out = run(REPLY, "--report", path, "--site", "https://site.example",
                      "--run-url", "https://example.org/run", "--run-id", "42",
                      "--dry-run", dry, "--out", tmp / "reply.md")
            self.assertEqual(out.returncode, 0, out.stderr)
            return (tmp / "reply.md").read_text()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reply_links_the_certificate_and_keeps_box_text_out(self):
        text = self.reply({"box": "@everyone", "verdict": "reproduced",
                           "runner": {"verdicts": ["reproduced"] * 3}})
        self.assertIn("https://site.example/r/42/certificate.html", text)
        self.assertNotIn("@everyone", text)
        self.assertIn("nothing was signed", self.reply({"verdict": "reproduced"}, dry="true"))
        self.assertIn("did not produce a report", self.reply(None))
