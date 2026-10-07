#!/usr/bin/env bash
# Official Linux amd64 releases, shared by PR CI and the publishing workflow.
set -euo pipefail

if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo 'Pinned tools require Linux x86_64' >&2
  exit 1
fi

mkdir -p .tools/bin
curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 120 \
  https://github.com/foundriesio/update-server/releases/download/v1.0-rc1/fiocli-linux-amd64 \
  --output .tools/bin/fiocli
curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 120 \
  https://github.com/foundriesio/composeapp/releases/download/v96.3.0/composectl_96.3.0_linux_amd64 \
  --output .tools/bin/composectl

# SHA256 digests published by the GitHub Releases API for these exact assets.
sha256sum --check <<'CHECKSUMS'
78f04deb403c9df29bb001b15af49bb9964b9083478bb90da9bc3980064104eb  .tools/bin/fiocli
9c6ed265e5d3b6e318d9f89f1604310d1c6e32326e56340917cc225c47467085  .tools/bin/composectl
CHECKSUMS
chmod 755 .tools/bin/fiocli .tools/bin/composectl
