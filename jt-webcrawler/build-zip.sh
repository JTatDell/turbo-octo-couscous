#!/usr/bin/env bash
# Build the distributable: dist/jt-webcrawler-<VERSION>.zip plus a .sha256 file.
# The zip holds one top-level folder, jt-webcrawler-<VERSION>/, so it never unpacks over an existing install.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
VER="$(tr -d '[:space:]' < "$HERE/VERSION")"
NAME="jt-webcrawler-$VER"
DIST="$HERE/../dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/$NAME" "$DIST"
for f in jt-webcrawler.py requirements.txt README.md install.sh VERSION; do
  install -m 0644 "$HERE/$f" "$STAGE/$NAME/$f"
done
chmod 0755 "$STAGE/$NAME/install.sh" "$STAGE/$NAME/jt-webcrawler.py"
rm -f "$DIST/$NAME.zip"
(cd "$STAGE" && zip -qrX "$DIST/$NAME.zip" "$NAME")
(cd "$DIST" && sha256sum "$NAME.zip" > "$NAME.zip.sha256")
echo "built $(cd "$DIST" && pwd -P)/$NAME.zip"
cat "$DIST/$NAME.zip.sha256"
