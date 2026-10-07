# Assembled updates

This repository combines pinned `composeapps` and `meta-foundries` builds into
machine-specific offline updates, then uploads them with `fiocli`. It does not
rebuild Yocto or containers and does not assign updates or start device rollouts.

## Inputs

The Git submodule entries are the source of truth. Initial pins:

| Submodule | Commit | Successful producer run |
| --- | --- | --- |
| composeapps | `e4cc5317d47e172b610ced0a23c53665b9faf8a7` | 2 |
| meta-foundries | `86ceaf3bc8c9c038ac5e47575045fe9ec1d507b1` | 24 |

The runner must have read access to the NFS archive mounted at
`/var/code/reckless-corp/storage/archive`. Expected completed directories:

```
composeapps/<run-number>_<full-submodule-sha>/apps/
meta-foundries/<run-number>_<full-submodule-sha>/<machine>/ostree_repo.tgz
```

The unnumbered SHA directory is an unfinished producer build and is ignored.
Exactly one numbered archive must match each pin. Missing or ambiguous matches
fail the build; there is no fallback to newest, another commit, or a registry.
The initial expected prefixes are `2_` and `24_`; archive presence still must be
verified on the NFS-equipped runner. If an upstream rerun creates multiple
archives for the same SHA, resolve that ambiguity before assembling.

## Machine selection

| Hardware ID | Architecture | Compose apps |
| --- | --- | --- |
| intel-corei7-64 | amd64 | matrix-app |
| uno-q | arm64 | matrix-app, uno-q-hat-app |

Names and digests are verified from the archive's
`apps/apps/<app-name>/<sha256>/{uri,manifest.json}`. Each selected app must have
exactly one version, matching its URI and manifest digest. `--apps name=sha256`
is passed explicitly to fiocli so the server never picks an arbitrary version.

Each machine gets a **fresh** store populated by pinned composectl v96.3.0
using `pull -l <archive-store> -s <fresh-store> -i <fresh-store> -a <arch>`.
The `-l` path uses a local blob provider without registry fallback. This also
regenerates per-image indexes for the requested architecture. Copying the
producer's shared store is unsafe here: its amd64-first pull can leave existing
image indexes unchanged when arm64 is pulled afterward.

OSTree archives come from trusted producer builds and are extracted using
Python's standard data filter, without custom tar-member validation. Exactly
one ref under `ostree_repo/refs/heads` and its corresponding commit object are
required. The resolved OSTree hash is passed explicitly. Both machines are
assembled and locally checked before either upload starts. Sources are not modified.

## GitHub setup

1. Provide an isolated, trusted Linux x64 self-hosted runner with labels
   `self-hosted`, `Linux`, `X64`, Python 3.12+, Git, curl, sha256sum, sufficient local disk, and
   read-only access to the NFS archive. It needs network access for submodule
   checkouts, pinned GitHub release downloads, GitHub Actions, and the update server. Assembly
   itself obtains all application and OS payloads from NFS.
2. Create GitHub environment `update-server`. Set these as repository-level or
   `update-server` environment variables or secrets:
   - `UPDATE_SERVER_URL`: HTTPS base URL of the server
   - `UPDATE_TAG`: the desired update tag (for example `main`)
   A non-empty variable takes precedence over a secret with the same name.
3. Add repository-level or `update-server` environment secret `UPDATE_SERVER_TOKEN`
   with upload permission for the intended server. This token is read only from
   secrets. Do not put credentials in this public repository.
4. Enable server-side TUF signing on the destination. These input archives have
   no pre-signed `tuf/` directory, so the server must generate signed metadata.
5. Restrict the environment to `main`; add required reviewers if desired. Keep
   this public repository's self-hosted runner isolated from other workloads.
   If you prefer a PR workflow, protect `main` with required hosted `Test` checks.

Only a push to `main`, or manual dispatch selecting `main`, can use the publishing
runner. Pull requests validate every workflow with actionlint, run unit tests,
and smoke-test the pinned CLIs on GitHub-hosted Ubuntu. They receive no
publishing secret or NFS access. Both direct pushes to `main` and PR merges start assembly. For the usual
manual workflow, prepare one commit locally and push it directly to `main`. Run
the local checks first because PR CI does not run on a direct push. Workflows currently use major-version
GitHub Action references; pin them to reviewed full SHAs for stricter supply-chain
control.

## Version and provenance

The server automatically assigns each update's numeric version; the workflow
does not pass `--version`. Update and TUF target names default to
`{{BUILD_NUM}}_{{GITHASH}}_{{MACHINE}}`, for example `73_a1b2c3d_uno-q`.
`BUILD_NUM` is GitHub's workflow run number, `GITHASH` is Git's short hash of the
assembled repository commit, and `MACHINE` is `uno-q` or `intel-corei7-64`.
Both machines refer to the same assembled commit, but their server-assigned
versions may differ.

To customize the names, add one standalone line to the message of the commit
you push to `main`:

```
name-format=text-from-user-{{GITHASH}}-{{MACHINE}}-{{BUILD_NUM}}
```

The line is read directly from that exact assembled commit, not a PR description
or submodule commits. If using a PR instead, put it in the final merge commit
message. Only those three placeholders are supported. Rendered names
must start with a letter or digit and contain only letters, digits, underscores,
dots, and hyphens. Both machine names must be distinct, so include `{{MACHINE}}`.
Duplicate format lines, unknown placeholders, and invalid names fail before any
server request or upload. Each fiocli upload has its own collapsible GitHub Actions
log group, labeled with the machine and rendered name, including on failure.

`out/provenance.json` records the assembled commit, exact submodule commits,
archive paths, app digests, architecture, and OSTree hash. The assembler prints this JSON
to the workflow log, and CI saves the file as an artifact. Full payloads remain in the runner's output during the job;
they are uploaded directly to the server rather than GitHub artifacts.

Uploads are sequential, not transactional. If the first succeeds and the second
fails, the first remains on the server. Re-running the same GitHub Actions run
keeps its run number and update names and resumes safely. A new manual dispatch
gets a new run number and therefore new default names. Custom formats that omit
`{{BUILD_NUM}}` can reuse names across runs. An existing
update is skipped only after checking its tag, TUF target identity (including
its server-assigned version), hardware ID, OSTree hash, and complete app digest
map. Read permission for `/v1/updates` and update TUF metadata is therefore also
required. Mismatches and unexpected API failures stop the run; there is no
automatic delete or blind overwrite. Names are determined by the format, commit,
and run number. This workflow does not automatically roll back or deploy to devices.

The fiocli token is provided only to the upload step, written to a temporary
mode-0600 JSON config, omitted from command arguments and child environment, and
removed on normal success or exceptions. An `always()` workflow step also removes
the run-specific credential directory on cancellation. A killed runner may not
execute cleanup; use ephemeral runners and secure runner cleanup.

## Updating inputs

```
git submodule update --init
git -C composeapps fetch origin
git -C composeapps checkout <reviewed-full-sha>
git -C meta-foundries fetch origin
git -C meta-foundries checkout <reviewed-full-sha>
git add composeapps meta-foundries
git commit -m 'Pin input builds' -m 'name-format=release-{{BUILD_NUM}}-{{GITHASH}}-{{MACHINE}}'
git push origin main
```

The second `-m` adds the optional name format to the commit body; omit it to use
the default. Confirm both producer workflows finished and the numbered NFS
directories exist before pushing. Initial successful upstream runs do not prove that their NFS
archives are still present. Review machine/app mapping when producer apps change.

## Local checks

```
bash scripts/lint-workflows.sh
python3 -m unittest discover -s tests -v
python3 scripts/assemble.py --archive-root /path/to/archive --output out --composectl /path/to/composectl
```

`out` must not already exist. The assembler requires initialized submodules and
runs from the repository root. Upload configuration is read from the environment;
local upload additionally requires `GITHUB_SHA` matching provenance and a positive
`GITHUB_RUN_NUMBER`. Run it from the assembled repository checkout so Git can read
the recorded commit message and resolve its short hash. Treat a
local upload as a real server mutation.

Official Linux amd64 release binaries and their published SHA256 checksums are
pinned once in `scripts/download-tools.sh`, shared by both workflows:
- fiocli: [foundriesio/update-server v1.0-rc1](https://github.com/foundriesio/update-server/releases/tag/v1.0-rc1)
- composectl: [foundriesio/composeapp v96.3.0](https://github.com/foundriesio/composeapp/releases/tag/v96.3.0)

PR CI also runs official [actionlint v1.7.12](https://github.com/rhysd/actionlint/releases/tag/v1.7.12),
downloaded and SHA256-verified by `scripts/lint-workflows.sh`, against every
workflow, including the main-only publishing workflow. This catches unsupported
expression contexts as well as workflow syntax errors. The lint script requires
Linux x86_64, curl, sha256sum, and tar; it does not require Go, ShellCheck, or Pyflakes.

PR CI downloads, verifies, and smoke-tests both CLIs without NFS or server
credentials. Neither workflow builds the tools or needs Go or Git LFS.
Unit tests run only in PR CI; run the local checks before pushing directly to
`main`. The publishing workflow does not run them. Update the release URLs and checksums together in
the shared script when upgrading.

Unit tests exercise archive selection, app identity and digest checks, trusted
tar extraction, OSTree selection, machine mapping, configuration validation,
provenance logging, exact upload arguments, server-assigned version resume
checks, and credential cleanup. Real NFS assembly and server upload require
the configured runner and are not covered by these fixture tests.
