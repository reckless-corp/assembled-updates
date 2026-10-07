#!/usr/bin/env bash
# Validate workflow syntax, expressions, and context availability before merge.
set -euo pipefail

if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo 'Pinned actionlint requires Linux x86_64' >&2
  exit 1
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 120 \
  https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz \
  --output "$tmp_dir/actionlint.tar.gz"
# SHA256 digest published for this exact official release asset.
echo "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8  $tmp_dir/actionlint.tar.gz" | sha256sum --check
tar -xzf "$tmp_dir/actionlint.tar.gz" -C "$tmp_dir" actionlint

# Include every workflow, even ones that do not run for pull requests.
shopt -s nullglob
workflows=(.github/workflows/*.yml .github/workflows/*.yaml)
if (( ${#workflows[@]} == 0 )); then
  echo 'No GitHub Actions workflows found' >&2
  exit 1
fi
# Keep this check focused on GitHub Actions semantics and independent of optional
# ShellCheck/Pyflakes installations. Python behavior is covered by the test job.
"$tmp_dir/actionlint" -shellcheck= -pyflakes= "${workflows[@]}"
