#!/usr/bin/env bash
set -euo pipefail

# Build a single-file macOS executable of the customized icloudpd.
#
# Usage:
#   bash scripts/build_mac_bin.sh [OUTPUT_NAME] [EXPECTED_ARCH]
#
#   OUTPUT_NAME    file produced in dist/                    (default: icloudpd-mac)
#   EXPECTED_ARCH  arm64 or x86_64, verified with lipo       (default: uname -m)
#
# Env:
#   ICLOUDPD_SRC_ROOT      tree holding pyproject.toml        (default: cwd)
#   ICLOUDPD_BUILD_VENV    venv with the dev dependencies; when unset the
#                          ambient python3 / pyinstaller are used
#   ICLOUDPD_COMMIT_SHA    override the baked commit sha
#   ICLOUDPD_COMMIT_TS     override the baked commit timestamp
#
# Example (local, Apple Silicon):
#   ICLOUDPD_BUILD_VENV="$HOME/.workbuddy/binaries/python/envs/icloudpd-build" \
#     bash scripts/build_mac_bin.sh icloudpd-mac-arm64 arm64
#
# A native runner is required: PyInstaller cannot cross-compile between arm64
# and x86_64 on macOS, so the Intel build has to run on an Intel machine.

NAME="${1:-icloudpd-mac}"
EXPECT="${2:-$(uname -m)}"

case "$EXPECT" in
  arm64|x86_64) ;;
  *) echo "unsupported EXPECTED_ARCH: $EXPECT (expected arm64 or x86_64)" >&2; exit 2 ;;
esac

ROOT="${ICLOUDPD_SRC_ROOT:-$(pwd)}"
cd "$ROOT"
[ -f pyproject.toml ] || { echo "no pyproject.toml in $ROOT" >&2; exit 1; }

VENV="${ICLOUDPD_BUILD_VENV:-}"
if [ -n "$VENV" ]; then
  [ -x "$VENV/bin/python" ] || { echo "no python inside venv: $VENV" >&2; exit 1; }
  PY="$VENV/bin/python"
  PATH="$VENV/bin:$PATH"
  export PATH
else
  PY="$(command -v python3)"
fi

VERSION=$("$PY" -c "
import pathlib, re
m = re.search(r'^version\s*=\s*\"([^\"]+)\"', pathlib.Path('pyproject.toml').read_text(), re.M)
assert m, 'cannot read version from pyproject.toml'
print(m.group(1))
")

# A plain source copy has no .git, so allow the CI to inject the commit metadata.
SHA="${ICLOUDPD_COMMIT_SHA:-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)}"
TS="${ICLOUDPD_COMMIT_TS:-$(git log -1 --format=%ct HEAD 2>/dev/null || date +%s)}"

# CI bakes version/sha/timestamp into src/foundation/__init__.py before building
# (see scripts/patch_version). Restore it once we are done, whatever happens.
FOUNDATION=src/foundation/__init__.py
cp "$FOUNDATION" "$FOUNDATION.buildbak"
restore_foundation() { mv -f "$FOUNDATION.buildbak" "$FOUNDATION"; }
trap restore_foundation EXIT

"$PY" - "$FOUNDATION" "$VERSION" "$SHA" "$TS" <<'PY'
import pathlib, sys

path, version, sha, ts = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
text = pathlib.Path(path).read_text()
for old, new in (('"0.0.1"', f'"{version}"'), ('"abcdefgh"', f'"{sha}"'), ("1234567890", ts)):
    assert old in text, f"placeholder {old} not found in {path}"
    text = text.replace(old, new)
pathlib.Path(path).write_text(text)
PY

echo "building ${NAME} ${VERSION} (sha ${SHA}) for macos-${EXPECT}"

# Dedicated work dirs: PyInstaller then never has to wipe a previous tree, which
# also keeps the build friendly to sandboxed environments with guarded deletes.
WORK="${ICLOUDPD_WORK_DIR:-build/pyinstaller}"
OUT="${ICLOUDPD_OUT_DIR:-build/pyinstaller-dist}"

mkdir -p dist

# absolute --add-data paths: --specpath changes the effective working directory
PYTHONHASHSEED=42 "$PY" -m PyInstaller --noconfirm \
  --specpath "$WORK" --workpath "$WORK" --distpath "$OUT" \
  --collect-all keyrings.alt --copy-metadata keyrings.alt --hidden-import pkgutil \
  --add-data "$ROOT/src/icloudpd/server/static:static" \
  --add-data "$ROOT/src/icloudpd/server/templates:templates" \
  --onefile --name "$NAME" \
  "$ROOT/src/starters/icloudpd.py"

mv -f "$OUT/$NAME" "dist/$NAME"
chmod +x "dist/$NAME"

# Fail loudly instead of shipping a binary built for the wrong CPU.
ACTUAL="$(lipo -archs "dist/$NAME")"
echo "lipo -archs: ${ACTUAL} (expected ${EXPECT})"
[ "$ACTUAL" = "$EXPECT" ] || { echo "ARCHITECTURE MISMATCH for dist/$NAME" >&2; exit 1; }

echo
file "dist/$NAME"
"dist/$NAME" --version

( cd dist && shasum -a 256 "$NAME" > "$NAME.sha256" )
echo
ls -la dist
