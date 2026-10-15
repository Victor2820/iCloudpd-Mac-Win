#!/bin/sh
# Apply the customizations to an unpacked upstream tree, then prove they landed.
#
# Used by custom/Dockerfile. The GitHub workflows do the same thing inline,
# because each of them also pins and verifies the sha256 of every patched file.
# This is the lighter version for the image build: it asserts the customizations
# are present *by content*, not by hash, so that bumping the upstream pin cannot
# break the image build on a hash nobody remembered to update here.
#
# Expects:  /src     an unpacked upstream tarball
#           /custom  this repository's custom/ directory
set -eu

SRC_DIR="${SRC_DIR:-/src}"
CUSTOM_DIR="${CUSTOM_DIR:-/custom}"

cd "$SRC_DIR"

git init -q .
git apply -p1 --verbose "$CUSTOM_DIR/icloudpd-custom.patch"
cp "$CUSTOM_DIR/guided_setup.py" src/icloudpd/guided_setup.py
cp "$CUSTOM_DIR/remember.py" src/icloudpd/remember.py
rm -rf .git

python3 - "$ICLOUDPD_VERSION" "$ICLOUDPD_COMMIT_SHA" "$ICLOUDPD_COMMIT_TS" <<'PY'
import pathlib
import sys

version, sha, ts = sys.argv[1], sys.argv[2], sys.argv[3]
path = pathlib.Path("src/foundation/__init__.py")
text = path.read_text()
text = text.replace('version="0.0.1"', f'version="{version}"')
text = text.replace('commit_sha="abcdefgh"', f'commit_sha="{sha}"')
text = text.replace("commit_timestamp=1234567890", f"commit_timestamp={ts}")
path.write_text(text)
PY

# If any of these is missing the patch did not apply, and the image would look
# fine while behaving exactly like stock upstream.
grep -q 'default="original"' src/icloudpd/cli.py
grep -q -- '--no-pause' src/icloudpd/cli.py
grep -q -- '--ignore-indexing-state' src/icloudpd/cli.py
grep -q 'ICLOUDPD_INDEXING_TIMEOUT' src/pyicloud_ipd/services/photos.py
grep -q 'sys.exit(cli())' src/starters/icloudpd.py
grep -q "version=\"$ICLOUDPD_VERSION\"" src/foundation/__init__.py
test -f src/icloudpd/guided_setup.py
test -f src/icloudpd/remember.py

echo "customizations applied and verified"
