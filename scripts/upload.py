#!/usr/bin/env python3
"""Upload assembled stores using an ephemeral fiocli config."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Refusing update-server redirect")


def get_json(url, token):
    request = Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    with build_opener(NoRedirect).open(request, timeout=60) as response:
        return json.load(response)


def verify_existing(tuf, machine, data, sha, tag, host):
    target_name = f"{machine}-{sha}"
    targets = tuf["targets.json"]["signed"]["targets"]
    if len(targets) != 1:
        raise ValueError("Existing update has different TUF target identity")
    target = next(iter(targets.values()))
    custom = target["custom"]
    # The server assigns the version. Verify the returned identity is internally
    # consistent rather than predicting a version from the workflow run number.
    version = custom.get("version")
    if (not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]*", version)
            or set(targets) != {f"{target_name}-{version}"}):
        raise ValueError("Existing update has different TUF target identity")
    expected = {"name": target_name, "version": str(version), "hardwareIds": [machine],
                "tags": [tag], "targetFormat": "OSTREE", "docker_compose_apps": {
                    name: {"uri": f"{host}/composeapphack/{name}@sha256:{info['sha256']}"}
                    for name, info in data["apps"].items()}}
    if target["hashes"] != {"sha256": data["ostree_sha256"]} or any(custom.get(k) != v for k, v in expected.items()):
        raise ValueError("Existing update metadata differs; refusing to skip or overwrite")


def settings(env):
    url = env.get("UPDATE_SERVER_URL", "")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("UPDATE_SERVER_URL must be an HTTPS URL without credentials, query or fragment")
    tag = env.get("UPDATE_TAG", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", tag):
        raise ValueError("UPDATE_TAG must be a nonempty safe path component")
    token = env.get("UPDATE_SERVER_TOKEN", "")
    if not token:
        raise ValueError("UPDATE_SERVER_TOKEN is required")
    return url.rstrip("/"), tag, token


def upload(output, fiocli, env=None):
    env = dict(os.environ if env is None else env)
    url, tag, token = settings(env)
    output = Path(output).absolute()
    record = json.loads((output / "provenance.json").read_text())
    sha = record["assembled_commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or sha != env.get("GITHUB_SHA"):
        raise ValueError("Provenance must match GITHUB_SHA")
    if set(record["machines"]) != {"intel-corei7-64", "uno-q"}:
        raise ValueError("Both expected machine builds are required")
    existing = get_json(url.rstrip("/") + "/v1/updates", token)
    if not isinstance(existing, list):
        raise ValueError("Unexpected updates API response")
    config_root = env.get("FIOCLI_CONFIG_ROOT")
    if config_root:
        Path(config_root).mkdir(mode=0o700, parents=True, exist_ok=True)
    # No token on command line, in logs, or in repository artifacts.
    with tempfile.TemporaryDirectory(prefix="assembled-fiocli-", dir=config_root) as temp:
        config = Path(temp) / "config.json"
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"active_context": "ci", "contexts": {"ci": {"url": url, "token": token}}}, stream)
        child_env = dict(env)
        child_env.pop("UPDATE_SERVER_TOKEN", None)
        for machine, data in record["machines"].items():
            name = f"{machine}-{sha}"
            matches = [item for item in existing if item["name"] == name]
            if matches:
                if len(matches) != 1 or matches[0]["tag"] != tag:
                    raise ValueError("Existing update name has a different tag or is ambiguous")
                metadata = get_json(url.rstrip("/") + f"/v1/updates/{name}/tuf", token)
                verify_existing(metadata, machine, data, sha, tag, urlsplit(url).netloc)
                print(f"Verified existing {name}; skipping upload", flush=True)
                continue
            cmd = [fiocli, "--config", str(config), "updates", "upload", tag, name,
                   str(output / machine), "--hardware-id", machine,
                   "--name", f"{machine}-{sha}", "--ostree-hash", data["ostree_sha256"]]
            for app, info in sorted(data["apps"].items()):
                cmd.extend(["--apps", f"{app}={info['sha256']}"])
            subprocess.run(cmd, check=True, env=child_env)
            print(f"Uploaded {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--fiocli", required=True)
    args = parser.parse_args()
    upload(args.output, args.fiocli)
