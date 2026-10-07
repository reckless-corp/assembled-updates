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
5. Protect `main` with reviewed pull requests and required hosted `Test` checks.
   Restrict the environment to `main`; add required reviewers if desired. Keep
   this public repository's self-hosted runner isolated from other workloads.

Only a push to `main`, or manual dispatch selecting `main`, can use the publishing
runner. Pull requests validate every workflow with actionlint, run unit tests,
and smoke-test the pinned CLIs on GitHub-hosted Ubuntu. They receive no
publishing secret or NFS access. A merge produces a push and starts assembly.
Direct pushes to main also trigger it, so enforce branch protection if all
publishing changes must be merged PRs. Workflows currently use major-version
GitHub Action references; pin them to reviewed full SHAs for stricter supply-chain
control.

## Version and provenance

The server automatically assigns each update's numeric version; the workflow
does not pass `--version`. Update and target names use `<machine>-<full-sha>`,
where the SHA identifies the assembled repository commit. Both machines refer
to the same assembled commit, but their server-assigned versions may differ.

`out/provenance.json` records the assembled commit, exact submodule commits,
archive paths, app digests, architecture, and OSTree hash. The assembler prints this JSON
to the workflow log, and CI saves the file as an artifact. Full payloads remain in the runner's output during the job;
they are uploaded directly to the server rather than GitHub artifacts.

Uploads are sequential, not transactional. If the first succeeds and the second
fails, the first remains on the server. Re-running or manually dispatching the
same assembled commit reuses its update names and resumes safely: an existing
update is skipped only after checking its tag, TUF target identity (including
its server-assigned version), hardware ID, OSTree hash, and complete app digest
map. Read permission for `/v1/updates` and update TUF metadata is therefore also
required. Mismatches and unexpected API failures stop the run; there is no
automatic delete or blind overwrite. A new assembled commit produces new update
names. This workflow does not automatically roll back or deploy to devices.

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
git commit -m 'Pin input builds'
```

Confirm both producer workflows finished and the numbered NFS directories exist
before merging. Initial successful upstream runs do not prove that their NFS
archives are still present. Review machine/app mapping when producer apps change.

## Local checks

```
bash scripts/lint-workflows.sh
python3 -m unittest discover -s tests -v
python3 scripts/assemble.py --archive-root /path/to/archive --output out --composectl /path/to/composectl
```

`out` must not already exist. The assembler requires initialized submodules and
runs from the repository root. Upload configuration is read from the environment;
local upload additionally requires `GITHUB_SHA` matching provenance. Treat a
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
Unit tests run only in PR CI; the publishing workflow relies on reviewed,
verified changes in `main`. Update the release URLs and checksums together in
the shared script when upgrading.

Unit tests exercise archive selection, app identity and digest checks, trusted
tar extraction, OSTree selection, machine mapping, configuration validation,
provenance logging, exact upload arguments, server-assigned version resume
checks, and credential cleanup. Real NFS assembly and server upload require
the configured runner and are not covered by these fixture tests.
