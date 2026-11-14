# shellcheck shell=bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/config/run.env"
log() { echo "[jbp $(date +%H:%M:%S)] $*" >&2; }
need() { for c in "$@"; do command -v "$c" >/dev/null || { echo "missing tool: $c" >&2; exit 1; }; done; }
# render <file>: replace __VAR__ with the value of shell var VAR
render() {
  python3 - "$1" <<'PY'
import os, re, sys
src = open(sys.argv[1]).read()
print(re.sub(r"__([A-Z][A-Z0-9_]*)__", lambda m: os.environ.get(m.group(1), m.group(0)), src), end="")
PY
}
stamp() { # record phase timestamps for the cost estimate: stamp <run_dir> <key>
  mkdir -p "$1"; echo "$2=$(date +%s)" >> "$1/phases.env"
}
