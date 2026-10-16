import base64
import hashlib
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import assemble


class DeltaMetadataTests(unittest.TestCase):
    def test_multiple_sources_and_duplicates(self):
        a, b = "24_" + "a" * 40, "25_" + "a" * 40
        message = f"Update\nplatform=uno-q\nmeta-foundries-from={a}\nmeta-foundries-from={b}\nmeta-foundries-from={a}"
        self.assertEqual(assemble.delta_sources(message), [a, b])
        self.assertEqual(assemble.delta_sources("Discuss meta-foundries-from=abc"), [])

    def test_invalid_sources(self):
        for value in ["", "abc", "a" * 64, "A" * 40, "a" * 40 + " ", "$(cmd)", "a" * 40, "0_" + "a" * 40,
                      "24_" + "A" * 40, "24_" + "a" * 40 + " ",
                      "../24_" + "a" * 40, "/24_" + "a" * 40]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                assemble.delta_sources("meta-foundries-from=" + value)

    def test_old_parameter_reports_migration(self):
        with self.assertRaisesRegex(ValueError, "Replace ostree-from"):
            assemble.delta_sources("ostree-from=" + "a" * 40)

    def test_exact_archive_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            names = ["24_" + "a" * 40, "25_" + "a" * 40]
            for name in names:
                (root / "meta-foundries" / name).mkdir(parents=True)
            for name in names:
                self.assertEqual(assemble.delta_archive(root, name),
                                 root / "meta-foundries" / name)
            with self.assertRaisesRegex(ValueError, "Missing completed"):
                assemble.delta_archive(root, "26_" + "a" * 40)
            link = root / "meta-foundries" / ("27_" + "a" * 40)
            link.symlink_to(root / "meta-foundries" / names[0])
            with self.assertRaisesRegex(ValueError, "Missing completed"):
                assemble.delta_archive(root, link.name)
            with self.assertRaisesRegex(ValueError, "Invalid"):
                assemble.delta_archive(root, "../" + names[0])


@unittest.skipUnless(shutil.which("ostree"), "ostree is not installed")
class DeltaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = {}
        self.bases = []
        for i in range(3):
            directory = self.root / str(i)
            repo = directory / "ostree_repo"
            tree = directory / "tree"
            tree.mkdir(parents=True)
            (tree / "unchanged").write_text("shared content\n" * 100)
            (tree / "changed").write_text(f"version {i}\n" * 100)
            assemble.run("ostree", f"--repo={repo}", "init", "--mode=archive")
            digest = assemble.run("ostree", f"--repo={repo}", "commit", "-b", "main",
                                  "-s", f"Version {i}", f"--tree=dir={tree}")
            if i < 2:
                archive = directory / "uno-q" / "ostree_repo.tgz"
                archive.parent.mkdir()
                with tarfile.open(archive, "w:gz") as tf:
                    tf.add(repo, arcname="ostree_repo")
                self.sources[f"{i + 1}_" + str(i + 1) * 40] = directory
                self.bases.append((repo, digest))
            else:
                self.repo, self.target = repo, digest

    def snapshot(self, directory):
        return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in directory.rglob("*") if p.is_file()}

    def test_two_bases_apply_without_target_objects(self):
        before = [self.snapshot(path) for path in self.sources.values()]
        objects = self.snapshot(self.repo / "objects")
        records = assemble.generate_deltas(self.repo, self.target, "uno-q", self.sources)
        self.assertEqual([r["from"] for r in records], [b for _, b in self.bases])
        self.assertEqual([r["meta_foundries_commit"] for r in records],
                         [name.split("_", 1)[1] for name in self.sources])
        self.assertTrue(all(r["status"] == "generated" for r in records))
        self.assertEqual(objects, self.snapshot(self.repo / "objects"))
        self.assertEqual(before, [self.snapshot(path) for path in self.sources.values()])
        self.assertEqual(assemble.run("ostree", f"--repo={self.repo}", "refs"), "main")
        self.assertTrue((self.repo / "summary").is_file())
        deltas = list((self.repo / "deltas").glob("*/*/superblock"))
        self.assertEqual(len(deltas), 2)
        for i, (base_repo, base) in enumerate(self.bases):
            client = self.root / f"client-{i}"
            assemble.run("ostree", f"--repo={client}", "init", "--mode=bare-user")
            assemble.run("ostree", f"--repo={client}", "pull-local", str(base_repo), base)
            # The superblock directory name encodes the source checksum.
            encoded = base64.b64encode(bytes.fromhex(base)).decode().rstrip("=").replace("/", "_")
            delta = next(p for p in deltas if p.parent.parent.name + p.parent.name.split("-")[0] == encoded)
            assemble.run("ostree", f"--repo={client}", "static-delta", "apply-offline", str(delta.parent))
            assemble.run("ostree", f"--repo={client}", "fsck")
            self.assertEqual(assemble.run("ostree", f"--repo={client}", "cat", self.target, "/changed"),
                             (self.root / "2/tree/changed").read_text().strip())

    def test_missing_base_fails(self):
        with self.assertRaises(FileNotFoundError):
            assemble.generate_deltas(self.repo, self.target, "intel-corei7-64", self.sources)
        self.assertFalse((self.repo / "deltas").exists())

    def test_same_commit_skips_delta(self):
        repo, target = self.bases[0]
        records = assemble.generate_deltas(repo, target, "uno-q", dict(list(self.sources.items())[:1]))
        self.assertEqual(records[0]["status"], "same-as-target")
        self.assertFalse((repo / "deltas").exists())


if __name__ == "__main__":
    unittest.main()
