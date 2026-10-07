import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import assemble
import upload


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_archive_missing_unique_ambiguous_and_partial(self):
        base = self.root / "composeapps"
        base.mkdir()
        sha = "a" * 40
        (base / sha).mkdir()  # Producer's unfinished path must not be used.
        with self.assertRaises(ValueError):
            assemble.archive_for(self.root, "composeapps", sha)
        good = base / ("2_" + sha)
        good.mkdir()
        self.assertEqual(assemble.archive_for(self.root, "composeapps", sha), good)
        (base / ("3_" + sha)).mkdir()
        with self.assertRaises(ValueError):
            assemble.archive_for(self.root, "composeapps", sha)

    def make_app(self, name="matrix-app"):
        manifest = b'{"schemaVersion":2}'
        digest = hashlib.sha256(manifest).hexdigest()
        version = self.root / "apps" / name / digest
        version.mkdir(parents=True)
        (version / "manifest.json").write_bytes(manifest)
        (version / "uri").write_text(f"ghcr.io/reckless-corp/{name}@sha256:{digest}\n")
        return version

    def test_app_identity_digest_and_ambiguity(self):
        version = self.make_app()
        self.assertEqual(assemble.apps_for(self.root, ["matrix-app"])["matrix-app"]["sha256"], version.name)
        (version / "manifest.json").write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            assemble.apps_for(self.root, ["matrix-app"])
        (version.parent / ("f" * 64)).mkdir()
        with self.assertRaises(ValueError):
            assemble.apps_for(self.root, ["matrix-app"])

    def test_wrong_uri_and_missing_app_fail(self):
        version = self.make_app()
        (version / "uri").write_text("ghcr.io/another-owner/matrix-app@sha256:" + version.name)
        with self.assertRaises(ValueError):
            assemble.apps_for(self.root, ["matrix-app"])
        with self.assertRaises(ValueError):
            assemble.apps_for(self.root, ["led-matrix-anim-app"])

    def make_tar(self, entries):
        path = self.root / "ostree.tgz"
        with tarfile.open(path, "w:gz") as tf:
            for name, content in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
        return path

    def test_ostree_requires_single_ref_and_commit(self):
        digest = "a" * 64
        entries = {"ostree_repo/refs/heads/main": (digest + "\n").encode(),
                   "ostree_repo/objects/aa/" + digest[2:] + ".commit": b"commit"}
        self.assertEqual(assemble.extract_ostree(self.make_tar(entries), self.root / "out"), digest)
        entries["ostree_repo/refs/heads/other"] = digest.encode()
        with self.assertRaises(ValueError):
            assemble.extract_ostree(self.make_tar(entries), self.root / "ambiguous")

    def test_trusted_archive_needs_no_custom_member_validation(self):
        digest = "a" * 64
        entries = {"ostree_repo/refs/heads/main": digest.encode(),
                   "ostree_repo/objects/aa/" + digest[2:] + ".commit": b"commit",
                   "build-info.txt": b"trusted producer metadata"}
        self.assertEqual(assemble.extract_ostree(self.make_tar(entries), self.root / "out"), digest)

    def test_cli_prints_provenance(self):
        record = {"assembled_commit": "a" * 40, "machines": {}}
        stdout = io.StringIO()
        with patch.object(sys, "argv", ["assemble.py", "--archive-root", "/archive",
                                      "--output", "out", "--composectl", "composectl"]), \
             patch("assemble.assemble", return_value=record) as build, \
             patch("sys.stdout", stdout):
            assemble.main()
        build.assert_called_once_with("/archive", "out", "composectl")
        self.assertEqual(json.loads(stdout.getvalue()), record)

    def test_machine_selection(self):
        self.assertEqual(assemble.MACHINES["intel-corei7-64"], ("amd64", ["matrix-app"]))
        self.assertEqual(assemble.MACHINES["uno-q"], ("arm64", ["matrix-app", "led-matrix-anim-app"]))

    def test_assembly_uses_local_sources_and_publishes_complete_directory(self):
        output = self.root / "result"
        sha = "a" * 40
        def fake_run(*args):
            if args[:2] == ("git", "ls-tree"):
                return f"160000 commit {sha}\t{args[-1]}"
            if args[0] == "git":
                return sha
            return json.dumps({"fetch_check": {"missing_blobs": None}})
        def fake_apps(store, names):
            return {name: {"sha256": "c" * 64,
                           "uri": f"ghcr.io/reckless-corp/{name}@sha256:" + "c" * 64}
                    for name in names}
        calls = []
        def fake_pull(cmd, check):
            self.assertIn("-l", cmd)
            self.assertEqual(cmd[cmd.index("-l") + 1], str(self.root / "composeapps" / "apps"))
            calls.append(cmd)
        with patch("assemble.run", side_effect=fake_run), \
             patch("assemble.archive_for", side_effect=lambda root, repo, sha: self.root / repo), \
             patch("assemble.extract_ostree", return_value="b" * 64), \
             patch("assemble.apps_for", side_effect=fake_apps), \
             patch("assemble.preserve_archive_blobs") as preserve, \
             patch("assemble.check_app_registry_blobs") as registry_check, \
             patch("assemble.subprocess.run", side_effect=fake_pull):
            record = assemble.assemble(self.root, output, "composectl")
        self.assertEqual(preserve.call_count, 2)
        self.assertEqual(registry_check.call_count, 2)
        self.assertTrue((output / "provenance.json").is_file())
        self.assertEqual(set(record["machines"]), set(assemble.MACHINES))
        self.assertEqual([cmd[cmd.index("-a") + 1] for cmd in calls], ["amd64", "arm64"])

    def upload_env(self):
        return {"UPDATE_SERVER_URL": "https://updates.example.test",
                "UPDATE_SERVER_TOKEN": "test-token", "GITHUB_SHA": "a" * 40, "GITHUB_RUN_NUMBER": "73"}

    def mock_upload_git(self, message="Pin builds"):
        mocked = patch("upload.git_output", side_effect=[message, "aaaaaaa\n"])
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_settings(self):
        env = self.upload_env()
        self.assertNotIn("UPDATE_TAG", env)
        self.assertEqual(upload.settings(env), (env["UPDATE_SERVER_URL"], "test-token"))
        self.assertEqual(upload.settings({**env, "UPDATE_TAG": "../obsolete"}),
                         (env["UPDATE_SERVER_URL"], "test-token"))
        for key, value in [("UPDATE_SERVER_URL", "http://bad.test"),
                           ("UPDATE_SERVER_TOKEN", "")]:
            with self.assertRaises(ValueError):
                upload.settings({**env, key: value})

    def test_upload_config_permissions_cleanup_and_arguments(self):
        env = self.upload_env()
        record = {"assembled_commit": env["GITHUB_SHA"], "machines": {
            m: {"ostree_sha256": "b" * 64, "apps": {name: {"sha256": "c" * 64} for name in names}}
            for m, (_, names) in assemble.MACHINES.items()}}
        (self.root / "provenance.json").write_text(json.dumps(record))
        paths = []
        self.mock_upload_git()
        def fake(cmd, check, env):
            config = Path(cmd[2])
            paths.append(config)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(config.read_text())["contexts"]["ci"]["token"], "test-token")
            self.assertNotIn("UPDATE_SERVER_TOKEN", env)
            self.assertNotIn("test-token", " ".join(cmd))
            self.assertNotIn("--version", cmd)
            machine = cmd[cmd.index("--hardware-id") + 1]
            self.assertEqual(cmd[5], {"intel-corei7-64": "reckless-corp", "uno-q": "uno-q"}[machine])
            self.assertEqual(cmd[6], f"73_aaaaaaa_{machine}")
            self.assertEqual(cmd[cmd.index("--name") + 1], cmd[6])
            self.assertIn("matrix-app=" + "c" * 64, cmd)
        with patch("upload.get_json", return_value=[]), patch("upload.subprocess.run", side_effect=fake) as mocked:
            upload.upload(self.root, "fiocli", env)
            self.assertEqual(mocked.call_count, 2)
        self.assertTrue(all(not p.exists() for p in paths))

    def test_upload_failure_cleans_config(self):
        env = self.upload_env()
        record = {"assembled_commit": env["GITHUB_SHA"], "machines": {
            m: {"ostree_sha256": "b" * 64, "apps": {}} for m in assemble.MACHINES}}
        (self.root / "provenance.json").write_text(json.dumps(record))
        paths = []
        self.mock_upload_git()
        def fail(cmd, **kwargs):
            paths.append(Path(cmd[2]))
            raise RuntimeError("upload failed")
        with patch("upload.get_json", return_value=[]), patch("upload.subprocess.run", side_effect=fail), self.assertRaises(RuntimeError):
            upload.upload(self.root, "fiocli", env)
        self.assertFalse(paths[0].exists())

    def test_existing_update_requires_exact_metadata(self):
        machine, sha, digest = "uno-q", "a" * 40, "b" * 64
        data = {"ostree_sha256": digest, "apps": {"matrix-app": {"sha256": "c" * 64}}}
        target = {"hashes": {"sha256": digest}, "custom": {
            "name": f"{machine}-{sha}", "version": "1007", "hardwareIds": [machine],
            "tags": ["uno-q"], "targetFormat": "OSTREE", "docker_compose_apps": {
                "matrix-app": {"uri": "updates.example.test/composeapphack/matrix-app@sha256:" + "c" * 64}}}}
        tuf = {"targets.json": {"signed": {"targets": {f"{machine}-{sha}-1007": target}}}}
        upload.verify_existing(tuf, machine, data, f"{machine}-{sha}", "uno-q", "updates.example.test")
        for key, value in [("version", "1008"), ("hardwareIds", ["intel-corei7-64"]),
                           ("tags", ["other"]), ("docker_compose_apps", {})]:
            old = target["custom"][key]
            target["custom"][key] = value
            with self.assertRaises(ValueError):
                upload.verify_existing(tuf, machine, data, f"{machine}-{sha}", "uno-q", "updates.example.test")
            target["custom"][key] = old

    def test_resume_uses_existing_server_version_and_uploads_missing_machine(self):
        env = self.upload_env()
        sha = env["GITHUB_SHA"]
        record = {"assembled_commit": sha, "machines": {
            machine: {"ostree_sha256": "b" * 64, "apps": {}}
            for machine in assemble.MACHINES}}
        (self.root / "provenance.json").write_text(json.dumps(record))
        self.mock_upload_git()
        name = "73_aaaaaaa_intel-corei7-64"
        target = {"hashes": {"sha256": "b" * 64}, "custom": {
            "name": name, "version": "4291", "hardwareIds": ["intel-corei7-64"],
            "tags": ["reckless-corp"], "targetFormat": "OSTREE", "docker_compose_apps": {}}}
        tuf = {"targets.json": {"signed": {"targets": {f"{name}-4291": target}}}}
        with patch("upload.get_json", side_effect=[[{"name": name, "tag": "reckless-corp"}], tuf]), \
             patch("upload.subprocess.run") as run:
            upload.upload(self.root, "fiocli", env)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][6], "73_aaaaaaa_uno-q")
        self.assertNotIn("--version", run.call_args.args[0])

    def test_resume_requires_machine_tags_in_update_list_and_tuf(self):
        expected_tags = {"intel-corei7-64": "reckless-corp", "uno-q": "uno-q"}
        cases = [(None, None)] + [(machine, source) for machine in expected_tags
                                 for source in ["list", "tuf"]]
        for mismatch_machine, mismatch_source in cases:
            with self.subTest(machine=mismatch_machine, source=mismatch_source):
                env = self.upload_env()
                record = {"assembled_commit": env["GITHUB_SHA"], "machines": {
                    machine: {"ostree_sha256": "b" * 64, "apps": {}}
                    for machine in expected_tags}}
                (self.root / "provenance.json").write_text(json.dumps(record))
                updates, metadata = [], []
                for machine, tag in expected_tags.items():
                    name = f"73_aaaaaaa_{machine}"
                    # The other machine's valid tag must not be accepted here.
                    wrong_tag = "uno-q" if machine == "intel-corei7-64" else "reckless-corp"
                    mismatch = machine == mismatch_machine
                    updates.append({"name": name, "tag": wrong_tag
                                    if mismatch and mismatch_source == "list" else tag})
                    target = {"hashes": {"sha256": "b" * 64}, "custom": {
                        "name": name, "version": "4291", "hardwareIds": [machine],
                        "tags": [wrong_tag if mismatch and mismatch_source == "tuf" else tag],
                        "targetFormat": "OSTREE", "docker_compose_apps": {}}}
                    metadata.append({"targets.json": {"signed": {
                        "targets": {f"{name}-4291": target}}}})
                with patch("upload.git_output", side_effect=["Pin builds", "aaaaaaa"]), \
                     patch("upload.get_json", side_effect=[updates, *metadata]), \
                     patch("upload.subprocess.run") as run:
                    if mismatch_machine:
                        with self.assertRaisesRegex(ValueError, "different tag|metadata differs"):
                            upload.upload(self.root, "fiocli", env)
                    else:
                        upload.upload(self.root, "fiocli", env)
                run.assert_not_called()

    def test_existing_update_rejects_invalid_version_and_target_identity(self):
        sha, machine = "a" * 40, "uno-q"
        data = {"ostree_sha256": "b" * 64, "apps": {}}
        target = {"hashes": {"sha256": "b" * 64}, "custom": {
            "name": f"{machine}-{sha}", "version": "92", "hardwareIds": [machine],
            "tags": ["uno-q"], "targetFormat": "OSTREE", "docker_compose_apps": {}}}
        for version in (None, "", "0", "-1", "abc", 92):
            target["custom"]["version"] = version
            tuf = {"targets.json": {"signed": {"targets": {f"{machine}-{sha}-{version}": target}}}}
            with self.assertRaises(ValueError):
                upload.verify_existing(tuf, machine, data, f"{machine}-{sha}", "uno-q", "updates.example.test")
        target["custom"]["version"] = "92"
        tuf = {"targets.json": {"signed": {"targets": {"wrong-name-92": target}}}}
        with self.assertRaises(ValueError):
            upload.verify_existing(tuf, machine, data, f"{machine}-{sha}", "uno-q", "updates.example.test")


if __name__ == "__main__":
    unittest.main()
