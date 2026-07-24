#!/bin/bash
# Reference solution entrypoint for the firmware release publisher task.
# Installs the reference publisher into /app/publisher/ so the verifier can
# run `npm run report` against a known-good implementation.
#
# Usage: ./solution/publish.sh
#   Copies solution/release-publisher.mjs → environment/publisher/release-publisher.mjs
#
# This script is invoked by the harness before the grader runs. The grader
# runs `npm run report` (defined in environment/package.json) which executes
# publisher/release-publisher.mjs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${SCRIPT_DIR}/release-publisher.mjs"
TARGET="${SCRIPT_DIR}/../environment/publisher/release-publisher.mjs"

if [ ! -f "$SOURCE" ]; then
  echo "ERROR: Reference solution not found at $SOURCE" >&2
  exit 1
fi

cp "$SOURCE" "$TARGET"
echo "Installed reference publisher to $TARGET"