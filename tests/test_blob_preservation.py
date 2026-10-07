"""Offline registry-payload regression; the integration test uses pinned composectl."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import assemble

OCI = "application/vnd.oci.image."
COMPOSECTL = Path(os.environ.get("COMPOSECTL", Path(__file__).resolve().parents[1] /
                                 ".tools/bin/composectl"))


def archive_bytes(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
    return output.getvalue()


class BlobPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.blobs = self.source / "blobs/sha256"
        self.blobs.mkdir(parents=True)
        self.images = {}
        for arch in ("amd64", "arm64"):
            layer = self.blob(archive_bytes({"platform": arch.encode()}), OCI + "layer.v1.tar+gzip")
            config = self.blob({"architecture": arch, "os": "linux", "rootfs": {
                "type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(
                    gzip.decompress((self.blobs / layer["digest"][7:]).read_bytes())).hexdigest()]}}, OCI + "config.v1+json")
            manifest = self.blob({"schemaVersion": 2, "mediaType": OCI + "manifest.v1+json",
                                  "config": config, "layers": [layer]}, OCI + "manifest.v1+json")
            manifest["platform"] = {"architecture": arch, "os": "linux"}
            self.images[arch] = (manifest, config, layer)
        # A producer need not fetch platforms it never requested. Closure checks
        # must not require this third platform or unrelated attestation trees.
        unavailable = {"mediaType": OCI + "manifest.v1+json", "digest": "sha256:" + "f" * 64,
                       "size": 100, "platform": {"architecture": "riscv64", "os": "linux"}}
        self.image_index = self.blob({"schemaVersion": 2, "mediaType": OCI + "index.v1+json",
                                     "manifests": [v[0] for v in self.images.values()] + [unavailable]},
                                    OCI + "index.v1+json")
        self.image_uri = "ghcr.io/reckless-corp/fixture@" + self.image_index["digest"]
        compose = ("services:\n  fixture:\n    image: " + self.image_uri + "\n    labels:\n      io.compose-spec.config-hash: fixture\n").encode()
        self.bundle = self.blob(archive_bytes({"docker-compose.yml": compose}), "application/octet-stream")
        # These values hash bundle files, not separate downloadable blobs.
        self.bundle_index = self.blob({"docker-compose.yml": "sha256:" + hashlib.sha256(compose).hexdigest()},
                                      "application/json")
        self.bundle["annotations"] = {
            "org.foundries.app.bundle.index.digest": self.bundle_index["digest"],
            "org.foundries.app.bundle.index.size": str(self.bundle_index["size"])}
        # composectl permits missing optional layers metadata, including when
        # loading from a registry. Preserve that behavior in our extra check.
        optional_meta = {"mediaType": "application/octet-stream", "digest": "sha256:" + "e" * 64,
                         "size": 10, "annotations": {"layers-meta": "v1"}}
        self.app_manifest = self.blob({"schemaVersion": 2, "mediaType": OCI + "manifest.v1+json",
                                      "artifactType": "application/vnd.fio+compose-app",
                                      "config": self.blob(b"{}", "application/vnd.oci.empty.v1+json"),
                                      "layers": [self.bundle, optional_meta]}, OCI + "manifest.v1+json")
        self.app_digest = self.app_manifest["digest"][7:]
        self.uri = "ghcr.io/reckless-corp/matrix-app@" + self.app_manifest["digest"]
        self.app = self.source / "apps/matrix-app" / self.app_digest
        self.app.mkdir(parents=True)
        (self.app / "manifest.json").write_bytes((self.blobs / self.app_digest).read_bytes())
        (self.app / "uri").write_text(self.uri)
        # Model the producer's stale amd64-first convenience index.
        self.index_path = Path("apps/matrix-app") / self.app_digest / "images/ghcr.io/reckless-corp/fixture" / self.image_index["digest"][7:] / "index.json"
        stale = self.source / self.index_path
        stale.parent.mkdir(parents=True)
        stale.write_text(json.dumps({"schemaVersion": 2, "manifests": [self.images["amd64"][0]]}))

    def blob(self, data, media_type):
        if not isinstance(data, bytes):
            data = json.dumps(data, separators=(",", ":")).encode()
        digest = hashlib.sha256(data).hexdigest()
        (self.blobs / digest).write_bytes(data)
        return {"mediaType": media_type, "digest": "sha256:" + digest, "size": len(data)}

    def test_preserve_only_blobs_and_validate_registry_references(self):
        dest = self.root / "dest"
        fresh = dest / self.index_path
        fresh.parent.mkdir(parents=True)
        fresh.write_bytes(b"fresh per-machine index")
        before = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        assemble.preserve_archive_blobs(self.source, dest)
        apps = assemble.apps_for(self.source, ["matrix-app"])
        assemble.check_app_registry_blobs(dest, apps)
        self.assertEqual(fresh.read_bytes(), b"fresh per-machine index")
        for blob in self.blobs.iterdir():
            target = dest / "blobs/sha256" / blob.name
            self.assertEqual(target.read_bytes(), blob.read_bytes())
            self.assertNotEqual(target.stat().st_ino, blob.stat().st_ino)
        self.assertEqual(before, {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob("*") if p.is_file()})
        (dest / "blobs/sha256" / self.bundle_index["digest"][7:]).unlink()
        with self.assertRaisesRegex(ValueError, "app bundle index"):
            assemble.check_app_registry_blobs(dest, apps)

    def test_corrupt_blob_fails(self):
        (self.blobs / self.bundle_index["digest"][7:]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            assemble.preserve_archive_blobs(self.source, self.root / "dest")

    def test_missing_archived_index_fails_despite_valid_app_metadata(self):
        (self.blobs / self.bundle_index["digest"][7:]).unlink()
        dest = self.root / "dest"
        assemble.preserve_archive_blobs(self.source, dest)
        with self.assertRaisesRegex(ValueError, "app bundle index"):
            assemble.check_app_registry_blobs(dest, assemble.apps_for(self.source, ["matrix-app"]))

    @unittest.skipUnless(COMPOSECTL.is_file(), "download pinned tools to run offline composectl regression")
    def test_real_local_pull_omits_index_then_preservation_restores_registry_payload(self):
        before = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        env = {**os.environ, "HOME": str(self.root)}
        for arch in ("amd64", "arm64"):
            with self.subTest(architecture=arch):
                dest = self.root / arch
                subprocess.run([str(COMPOSECTL), "pull", "-l", str(self.source), "-s", str(dest),
                                "-i", str(dest), "-a", arch, self.uri], check=True, env=env,
                               stdout=subprocess.DEVNULL)
                index_blob = dest / "blobs/sha256" / self.bundle_index["digest"][7:]
                self.assertFalse(index_blob.exists(), "pinned local-provider omission must be reproduced")
                check_cmd = [str(COMPOSECTL), "check", "--local", "--format", "json", "-s", str(dest),
                             "-i", str(dest), "-a", arch, self.uri]
                # Existing check passes even while registry-required index is absent.
                self.assertFalse(json.loads(subprocess.check_output(check_cmd, env=env))["fetch_check"]["missing_blobs"])
                assemble.preserve_archive_blobs(self.source, dest)
                assemble.check_app_registry_blobs(dest, assemble.apps_for(self.source, ["matrix-app"]))
                self.assertFalse(json.loads(subprocess.check_output(check_cmd, env=env))["fetch_check"]["missing_blobs"])
                regenerated = json.loads((dest / self.index_path).read_bytes())
                self.assertEqual([d["platform"]["architecture"] for d in regenerated["manifests"]], [arch])
                # Match registry routing: every required URL resolves in blobs/sha256,
                # including the original image index, selected config and image layer.
                required = [self.app_manifest, self.bundle, self.bundle_index, self.image_index, *self.images[arch]]
                for desc in required:
                    blob = dest / "blobs/sha256" / desc["digest"][7:]
                    self.assertEqual(hashlib.sha256(blob.read_bytes()).hexdigest(), desc["digest"][7:])
                    self.assertEqual(blob.stat().st_size, desc["size"])
                selected_layer = self.images[arch][2]["digest"]
                (dest / "blobs/sha256" / selected_layer[7:]).unlink()
                missing = json.loads(subprocess.check_output(check_cmd, env=env))["fetch_check"]["missing_blobs"]
                self.assertTrue(missing, "selected-platform layer must be required")
        self.assertEqual(before, {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob("*") if p.is_file()})


if __name__ == "__main__":
    unittest.main()
