#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v node >/dev/null; then
  for candidate in /opt/homebrew/opt/node@22/bin /usr/local/opt/node@22/bin; do
    if [ -x "$candidate/node" ]; then
      export PATH="$candidate:$PATH"
      break
    fi
  done
fi
if ! command -v node >/dev/null; then
  echo 'Node.js 22.12+ is required. See README_frontend.md.' >&2
  exit 1
fi
exec node scripts/demo.mjs "$@"
