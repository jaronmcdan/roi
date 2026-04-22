#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT/dist"
INCLUDE_OFFLINE_BUNDLE="0"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  cat <<EOF
Usage: $0 [--offline] [--python <python-exe>]

Options:
  --offline              Bundle Python wheels/sdists for offline Pi install
  --python <python-exe>  Python interpreter used to download/build artifacts

Examples:
  $0
  $0 --offline
  $0 --offline --python python3.11
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --offline)
      INCLUDE_OFFLINE_BUNDLE="1"; shift;;
    --python)
      PYTHON_BIN="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2;;
  esac
done

mkdir -p "$DIST_DIR"

if [[ "$INCLUDE_OFFLINE_BUNDLE" == "1" ]]; then
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    if command -v python >/dev/null 2>&1; then
      PYTHON_BIN="python"
    else
      echo "ERROR: cannot find Python interpreter ('$PYTHON_BIN')." >&2
      exit 1
    fi
  fi
fi

# Version string: git short SHA + dirty marker (if available), else timestamp
if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  SHA="$(git -C "$ROOT" rev-parse --short HEAD)"
  DIRTY=""
  if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
    DIRTY="-dirty"
  fi
  VER="${SHA}${DIRTY}"
else
  VER="$(date +%Y%m%d-%H%M%S)"
fi

OUT="$DIST_DIR/roi-$VER.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Copy a clean tree (avoid venv/cache/git)
rsync -a \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude "venv" \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  --exclude ".pytest_cache" \
  --exclude "dist" \
  --exclude "build" \
  --exclude ".mypy_cache" \
  --exclude ".ruff_cache" \
  --exclude "deploy/wheelhouse" \
  "$ROOT/" "$TMP/roi/"

# Ensure we ship install helpers even if user runs this script standalone
chmod +x "$TMP/roi/scripts/"*.sh 2>/dev/null || true

if [[ "$INCLUDE_OFFLINE_BUNDLE" == "1" ]]; then
  WHEELHOUSE="$TMP/roi/deploy/wheelhouse"
  mkdir -p "$WHEELHOUSE"

  echo "[ROI] Building offline wheelhouse with $PYTHON_BIN"
  # Keep installer bootstrap packages available without network.
  "$PYTHON_BIN" -m pip --disable-pip-version-check download --dest "$WHEELHOUSE" pip setuptools wheel
  # Build/download ROI + runtime dependencies into a transfer-friendly wheelhouse.
  "$PYTHON_BIN" -m pip --disable-pip-version-check wheel --wheel-dir "$WHEELHOUSE" --prefer-binary "$ROOT"
fi

tar -C "$TMP" -czf "$OUT" "roi"

echo "Built: $OUT"
if [[ "$INCLUDE_OFFLINE_BUNDLE" == "1" ]]; then
  echo "Includes offline wheelhouse at: deploy/wheelhouse/"
fi
