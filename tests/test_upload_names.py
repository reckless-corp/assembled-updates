import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import upload


class UploadNameTests(unittest.TestCase):
    sha = "a" * 40
    machines = ["intel-corei7-64", "uno-q"]

    def names(self, message, run="73", short="aaaaaaaaaaaa"):
        with patch("upload.git_output", side_effect=[message, short]) as git:
            result = upload.update_names(self.sha, self.machines, {"GITHUB_RUN_NUMBER": run})
        self.assertEqual(git.call_args_list[0].args,
                         ("show", "-s", "--format=%B", self.sha, "--"))
        self.assertEqual(git.call_args_list[1].args, ("rev-parse", "--short", self.sha))
        return result

    def test_default_uses_run_number_and_git_short_hash(self):
        self.assertEqual(self.names("Merge pull request #8\n\nPin builds"),
                         {m: f"73_aaaaaaaaaaaa_{m}" for m in self.machines})
        self.assertEqual(self.names("Pin builds", run="74")["uno-q"],
                         "74_aaaaaaaaaaaa_uno-q")

    def test_custom_line_and_literal_prefix(self):
        message = "Merge pull request #8\n\nname-format=release-{{GITHASH}}-{{MACHINE}}-{{BUILD_NUM}}\n"
        self.assertEqual(self.names(message),
                         {m: f"release-aaaaaaaaaaaa-{m}-73" for m in self.machines})
        self.assertEqual(self.names("name-format=release-{{MACHINE}}"),
                         {m: f"release-{m}" for m in self.machines})

    def test_invalid_formats_fail_clearly(self):
        for template in ["", "../{{MACHINE}}", "x/{{MACHINE}}", "x%2f{{MACHINE}}",
                         "{{UNKNOWN}}-{{MACHINE}}", "{MACHINE}", "same-name",
                         " space-{{MACHINE}}", "{{MACHINE}} ", "$(cmd)-{{MACHINE}}"]:
            with self.subTest(template=template), self.assertRaises(ValueError):
                self.names("name-format=" + template)
        with self.assertRaisesRegex(ValueError, "at most one"):
            self.names("name-format=a\nname-format=b")
        for number in ["", "0", "-1", "3.2", "abc"]:
            with self.subTest(number=number), self.assertRaisesRegex(ValueError, "GITHUB_RUN_NUMBER"):
                self.names("", run=number)
        with self.assertRaisesRegex(ValueError, "invalid short hash"):
            self.names("", short="bbbbbbb")

    def test_git_message_is_read_without_shell(self):
        with patch("upload.subprocess.check_output", return_value="name-format=$(unsafe)\n") as run:
            self.assertEqual(upload.git_output("show", "-s", "--format=%B", self.sha, "--"),
                             "name-format=$(unsafe)\n")
        run.assert_called_once_with(["git", "show", "-s", "--format=%B", self.sha, "--"], text=True)

    def test_custom_resume_and_group_cleanup(self):
        for failing in [False, True]:
            with self.subTest(failing=failing), tempfile.TemporaryDirectory() as temp:
                record = {"assembled_commit": self.sha, "machines": {
                    m: {"ostree_sha256": "b" * 64, "apps": {}} for m in self.machines}}
                Path(temp, "provenance.json").write_text(json.dumps(record))
                name = "release-intel-corei7-64"
                target = {"hashes": {"sha256": "b" * 64}, "custom": {
                    "name": name, "version": "4291", "hardwareIds": [self.machines[0]],
                    "tags": ["main"], "targetFormat": "OSTREE", "docker_compose_apps": {}}}
                tuf = {"targets.json": {"signed": {"targets": {name + "-4291": target}}}}
                env = {"GITHUB_SHA": self.sha, "GITHUB_RUN_NUMBER": "73",
                       "UPDATE_SERVER_URL": "https://updates.example.test", "UPDATE_TAG": "main",
                       "UPDATE_SERVER_TOKEN": "test-token"}
                stdout = io.StringIO()
                with patch("upload.git_output", side_effect=["name-format=release-{{MACHINE}}", "aaaaaaa"]), \
                     patch("upload.get_json", side_effect=[[{"name": name, "tag": "main"}], tuf]) as get, \
                     patch("upload.subprocess.run", side_effect=RuntimeError("failed") if failing else None) as run, \
                     patch("sys.stdout", stdout):
                    if failing:
                        with self.assertRaises(RuntimeError):
                            upload.upload(temp, "fiocli", env)
                    else:
                        upload.upload(temp, "fiocli", env)
                get.assert_called_with("https://updates.example.test/v1/updates/" + name + "/tuf", "test-token")
                cmd = run.call_args.args[0]
                self.assertEqual(cmd[6], "release-uno-q")
                self.assertEqual(cmd[cmd.index("--name") + 1], "release-uno-q")
                self.assertNotIn("--version", cmd)
                self.assertFalse(Path(cmd[2]).exists())
                lines = stdout.getvalue().splitlines()
                self.assertEqual(lines.count("::group::Upload uno-q: release-uno-q"), 1)
                self.assertEqual(lines.count("::endgroup::"), 1)
                self.assertEqual(lines[-1], "::endgroup::")

    def test_all_names_validated_before_server_access(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "provenance.json").write_text(json.dumps({
                "assembled_commit": self.sha, "machines": {m: {} for m in self.machines}}))
            env = {"GITHUB_SHA": self.sha, "GITHUB_RUN_NUMBER": "73",
                   "UPDATE_SERVER_URL": "https://updates.example.test", "UPDATE_TAG": "main",
                   "UPDATE_SERVER_TOKEN": "test-token"}
            with patch("upload.git_output", side_effect=["name-format=duplicate", "aaaaaaa"]), \
                 patch("upload.get_json") as get, patch("upload.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "distinct"):
                    upload.upload(temp, "fiocli", env)
            get.assert_not_called()
            run.assert_not_called()
