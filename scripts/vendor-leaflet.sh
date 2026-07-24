#!/usr/bin/env sh
# Download Leaflet into web/vendor/ so the dashboard runs without reaching a CDN.
# index.html loads the CDN copy first and falls back to these files automatically.
#
#   sh scripts/vendor-leaflet.sh
#
# Note that the dashboard still needs network access for the Dott feed and map
# tiles; this only removes the CDN dependency.
set -eu

VERSION="1.9.4"
DEST="$(dirname "$0")/../web/vendor"
BASE="https://unpkg.com/leaflet@${VERSION}/dist"

mkdir -p "$DEST"
for f in leaflet.js leaflet.css; do
  echo "fetching $f"
  curl -fsSL "$BASE/$f" -o "$DEST/$f"
done
echo "Leaflet $VERSION vendored into $DEST"
