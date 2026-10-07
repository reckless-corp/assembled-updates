#!/usr/bin/env python3
"""Build machine-specific offline stores exclusively from completed NFS archives."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

MACHINES = {
    "intel-corei7-64": ("amd64", ["matrix-app"]),
    "uno-q": ("arm64", ["matrix-app", "led-matrix-anim-app"]),
}
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def archive_for(root, repo, sha):
    if not SHA.fullmatch(sha):
        raise ValueError("Expected a full Git SHA")
    base = Path(root) / repo
    matches = [p for p in base.iterdir()
               if re.fullmatch(r"[1-9][0-9]*_" + sha, p.name)]
    if len(matches) != 1 or not matches[0].is_dir() or matches[0].is_symlink():
        raise ValueError(f"Expected exactly one completed archive for {repo}@{sha}; found {len(matches)}")
    return matches[0]


def apps_for(store, names):
    result = {}
    for name in names:
        app = Path(store) / "apps" / name
        if app.is_symlink() or not app.is_dir():
            raise ValueError(f"Missing app directory: {app}")
        versions = list(app.iterdir())
        if len(versions) != 1:
            raise ValueError(f"Ambiguous or missing app version: {app}")
        version = versions[0]
        if version.is_symlink() or not version.is_dir() or not DIGEST.fullmatch(version.name):
            raise ValueError(f"Invalid app version: {version}")
        uri_file = version / "uri"
        if uri_file.is_symlink():
            raise ValueError(f"Refusing URI symlink: {uri_file}")
        uri = uri_file.read_text().strip()
        expected = f"ghcr.io/reckless-corp/{name}@sha256:{version.name}"
        if uri != expected:
            raise ValueError(f"App URI does not match archive path: {uri_file}")
        manifest = version / "manifest.json"
        if manifest.is_symlink() or hashlib.sha256(manifest.read_bytes()).hexdigest() != version.name:
            raise ValueError(f"App manifest digest mismatch: {manifest}")
        result[name] = {"sha256": version.name, "uri": uri}
    return result


def extract_ostree(archive, destination):
    with tarfile.open(archive, "r:gz") as tf:
        # Archives are produced by our trusted, pinned build workflows.
        tf.extractall(destination, filter="data")
    repo = Path(destination) / "ostree_repo"
    heads = [p for p in (repo / "refs" / "heads").rglob("*") if p.is_file()]
    if len(heads) != 1:
        raise ValueError("Expected exactly one OSTree ref; refusing server's first-ref guess")
    digest = heads[0].read_text().strip()
    if not DIGEST.fullmatch(digest):
        raise ValueError("Invalid OSTree ref digest")
    if not (repo / "objects" / digest[:2] / (digest[2:] + ".commit")).is_file():
        raise ValueError("Missing OSTree commit object")
    return digest


def assemble(root, output, composectl):
    output = Path(output).absolute()
    if output.exists():
        raise ValueError(f"Output already exists; choose a fresh directory: {output}")
    pins = {}
    for repo in ("composeapps", "meta-foundries"):
        entry = run("git", "ls-tree", "HEAD", repo).split()
        if len(entry) != 4 or entry[:2] != ["160000", "commit"]:
            raise ValueError(f"{repo} must be a pinned Git submodule")
        pins[repo] = entry[2]
        if run("git", "-C", repo, "rev-parse", "HEAD") != pins[repo]:
            raise ValueError(f"Submodule checkout mismatch: {repo}")
    archives = {repo: archive_for(root, repo, sha) for repo, sha in pins.items()}
    assembled_sha = run("git", "rev-parse", "HEAD")
    if not SHA.fullmatch(assembled_sha):
        raise ValueError("Invalid assembled commit")
    record = {"assembled_commit": assembled_sha, "pins": pins,
              "archives": {k: str(v) for k, v in archives.items()}, "machines": {}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".assemble-", dir=output.parent) as temp:
        stage = Path(temp)
        store = archives["composeapps"] / "apps"
        for machine, (arch, names) in MACHINES.items():
            dest = stage / machine
            dest.mkdir()
            digest = extract_ostree(archives["meta-foundries"] / machine / "ostree_repo.tgz", dest)
            apps = apps_for(store, names)
            # -l uses a local blob provider with no remote fallback. A fresh store
            # regenerates correct per-architecture indexes rather than copying
            # the producer's shared amd64-first indexes.
            subprocess.run([composectl, "pull", "-l", str(store), "-s", str(dest / "apps"),
                            "-i", str(dest / "apps"), "-a", arch,
                            *[v["uri"] for v in apps.values()]], check=True)
            if apps_for(dest / "apps", names) != apps:
                raise ValueError("Assembled app metadata differs from source")
            check = json.loads(run(composectl, "check", "--local", "--format", "json",
                                   "-s", str(dest / "apps"), "-i", str(dest / "apps"),
                                   "-a", arch, *[v["uri"] for v in apps.values()]))
            if check["fetch_check"]["missing_blobs"]:
                raise ValueError(f"Assembled store has missing blobs: {machine}")
            # The pinned server accepts regular files/directories only. Composectl
            # creates hard links, which fiocli archives as regular file copies.
            for path in dest.rglob("*"):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise ValueError(f"Unsupported assembled filesystem entry: {path}")
            record["machines"][machine] = {"architecture": arch, "ostree_sha256": digest, "apps": apps}
        (stage / "provenance.json").write_text(json.dumps(record, indent=2) + "\n")
        # Rename the completed directory; do not mutate either source archive.
        stage.rename(output)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--composectl", required=True)
    args = parser.parse_args()
    print(json.dumps(assemble(args.archive_root, args.output, args.composectl), indent=2))


if __name__ == "__main__":
    main()
