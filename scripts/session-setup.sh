#!/usr/bin/env bash
# Session bootstrap: the one command for a fresh Claude cloud session, a Remote Control session or a dev box.
#   bash scripts/session-setup.sh
# 1. installs mise if missing (version from the CI workflow, sha256 from the release checked against the download),
# 2. runs `mise run session:setup` (Python env from the hash-pinned requirements, app tools and packages).
# Idempotent; quiet unless something fails. Writes nothing outside the clone except ~/.local/bin/mise,
# mise's own cache, and uv's cache. Never reads or prints secrets.
set -euo pipefail

# The mise version is the one the CI workflow pins (the mise-action `version:` line, which Renovate bumps), so a
# bump needs no edit here. The sha256 comes from that release's SHASUMS256.txt and is checked against the tarball.
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MISE_VERSION=$(sed -n 's/^ *version: *\([0-9][0-9.]*\) *$/\1/p' "$root/.github/workflows/deploy-dashboard.yml" | head -n1)
[ -n "$MISE_VERSION" ] || { echo "session-setup: no mise version in .github/workflows/deploy-dashboard.yml" >&2; exit 1; }
release="https://github.com/jdx/mise/releases/download/v${MISE_VERSION}"

install_mise() {
  local arch tmp want
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) arch=x64 ;;
    Linux-aarch64|Linux-arm64) arch=arm64 ;;
    *) echo "session-setup: no pinned mise build for $(uname -s)-$(uname -m); install mise ${MISE_VERSION} yourself (https://mise.jdx.dev)" >&2; exit 1 ;;
  esac
  tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" EXIT
  curl -fsSL --retry 3 -o "$tmp/mise.tgz" "$release/mise-v${MISE_VERSION}-linux-${arch}.tar.gz"
  want=$(curl -fsSL --retry 3 "$release/SHASUMS256.txt" | awk -v f="./mise-v${MISE_VERSION}-linux-${arch}.tar.gz" '$2==f{print $1}')
  [ -n "$want" ] || { echo "session-setup: no checksum published for mise ${MISE_VERSION}" >&2; exit 1; }
  echo "$want  $tmp/mise.tgz" | sha256sum -c --quiet - || { echo "session-setup: mise checksum mismatch, not installing" >&2; exit 1; }
  tar -xzf "$tmp/mise.tgz" -C "$tmp"
  mkdir -p "$HOME/.local/bin"
  install -m 0755 "$tmp/mise/bin/mise" "$HOME/.local/bin/mise"
}

export PATH="$HOME/.local/bin:$PATH"
if ! command -v mise >/dev/null 2>&1 || [ "$(mise --version 2>/dev/null | awk '{print $1}')" != "$MISE_VERSION" ]; then
  install_mise
fi

cd "$root"
for f in "$root/mise.toml" "$root/app/mise.toml"; do mise trust --quiet "$f"; done
exec mise run session:setup
