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
from assemble import selected_machines


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Refusing update-server redirect")


def get_json(url, token):
    request = Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    with build_opener(NoRedirect).open(request, timeout=60) as response:
        return json.load(response)


MACHINE_TAGS = {"intel-corei7-64": "reckless-corp", "uno-q": "uno-q"}
DEFAULT_NAME_FORMAT = "{{BUILD_NUM}}_{{GITHASH}}_{{MACHINE}}"


def git_output(*args):
    # The message is data, never shell input. Read the recorded assembled commit,
    # not HEAD, a PR body, or one of the submodule commits.
    return subprocess.check_output(["git", *args], text=True)


def update_names(sha, machines, env, message=None):
    build_num = env.get("GITHUB_RUN_NUMBER", "")
    if not re.fullmatch(r"[1-9][0-9]*", build_num):
        raise ValueError("GITHUB_RUN_NUMBER must be a positive integer")
    if message is None:
        message = git_output("show", "-s", "--format=%B", sha, "--")
    formats = [line[len("name-format="):] for line in message.splitlines()
               if line.startswith("name-format=")]
    if len(formats) > 1:
        raise ValueError("Commit message must contain at most one name-format= line")
    template = formats[0] if formats else DEFAULT_NAME_FORMAT
    short_sha = git_output("rev-parse", "--short", sha).strip()
    if not re.fullmatch(r"[0-9a-f]{4,40}", short_sha) or not sha.startswith(short_sha):
        raise ValueError("Git returned an invalid short hash for the assembled commit")
    names = {}
    for machine in machines:
        name = template
        for key, value in {"GITHASH": short_sha, "MACHINE": machine,
                           "BUILD_NUM": build_num}.items():
            name = name.replace("{{" + key + "}}", value)
        if "{" in name or "}" in name:
            raise ValueError("name-format contains an unknown or malformed placeholder; "
                             "use {{GITHASH}}, {{MACHINE}}, or {{BUILD_NUM}}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError("Rendered update names must be nonempty safe path components "
                             "using letters, digits, underscores, dots, and hyphens")
        names[machine] = name
    if len(set(names.values())) != len(names):
        raise ValueError("name-format must produce a distinct name for each machine; "
                         "include {{MACHINE}}")
    return names


def verify_existing(tuf, machine, data, target_name, tag, host):
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
    token = env.get("UPDATE_SERVER_TOKEN", "")
    if not token:
        raise ValueError("UPDATE_SERVER_TOKEN is required")
    return url.rstrip("/"), token


def upload(output, fiocli, env=None):
    env = dict(os.environ if env is None else env)
    url, token = settings(env)
    output = Path(output).absolute()
    record = json.loads((output / "provenance.json").read_text())
    sha = record["assembled_commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or sha != env.get("GITHUB_SHA"):
        raise ValueError("Provenance must match GITHUB_SHA")
    message = git_output("show", "-s", "--format=%B", sha, "--")
    if set(record["machines"]) != set(selected_machines(message)):
        raise ValueError("Provenance machines must match the commit's platform selection")
    names = update_names(sha, record["machines"], env, message)
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
            name = names[machine]
            tag = MACHINE_TAGS[machine]
            matches = [item for item in existing if item["name"] == name]
            if matches:
                if len(matches) != 1 or matches[0]["tag"] != tag:
                    raise ValueError("Existing update name has a different tag or is ambiguous")
                metadata = get_json(url.rstrip("/") + f"/v1/updates/{name}/tuf", token)
                verify_existing(metadata, machine, data, name, tag, urlsplit(url).netloc)
                print(f"Verified existing {name}; skipping upload", flush=True)
                continue
            cmd = [fiocli, "--config", str(config), "updates", "upload", tag, name,
                   str(output / machine), "--hardware-id", machine,
                   "--name", name, "--ostree-hash", data["ostree_sha256"]]
            for app, info in sorted(data["apps"].items()):
                cmd.extend(["--apps", f"{app}={info['sha256']}"])
            print("Running:", cmd)
            print(f"::group::Upload {machine}: {name}", flush=True)
            try:
                subprocess.run(cmd, check=True, env=child_env)
                print(f"Uploaded {name}", flush=True)
            finally:
                print("::endgroup::", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--fiocli", required=True)
    args = parser.parse_args()
    upload(args.output, args.fiocli)
