#!/usr/bin/env bash
# Regenerate netkeep/assets/netkeep.icns, the launcher app's icon (#754), from the
# favicon (frontend/public/favicon.svg). Run by hand on a Mac when the favicon changes,
# and commit the result: `netkeep setup` copies the committed file into the bundle, so
# building the launcher runs no image tools. Needs ImageMagick (magick) besides the
# Mac's own qlmanage, sips and iconutil.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
favicon="$root/frontend/public/favicon.svg"
out="$root/netkeep/assets/netkeep.icns"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# macOS icons leave a margin: the tile is 824 of 1024 points (Apple's app icon grid).
# Wrap the favicon's 32-unit tile in a viewBox 1024/824 times its size, centered, once
# on white and once on black.
box='-3.884 -3.884 39.767 39.767'
grep -q 'viewBox="0 0 32 32"' "$favicon" || { echo "favicon.svg's viewBox changed"; exit 1; }
for background in white black; do
  sed -e "s|viewBox=\"0 0 32 32\"|viewBox=\"$box\"|" \
    -e "s|<rect width=\"32\"|<rect x=\"-3.884\" y=\"-3.884\" width=\"39.767\" height=\"39.767\" fill=\"$background\"/><rect width=\"32\"|" \
    "$favicon" >"$work/$background.svg"
  # Quick Look renders SVG with WebKit, as a browser does, but only onto an opaque canvas.
  qlmanage -t -s 1024 -o "$work" "$work/$background.svg" >/dev/null
  test -f "$work/$background.svg.png" || { echo "qlmanage did not render $favicon"; exit 1; }
done

# Difference matting: what the two renders disagree on is transparency.
magick "$work/white.svg.png" "$work/black.svg.png" -compose difference -composite \
  -colorspace gray -negate "$work/alpha.png"
magick "$work/black.svg.png" "$work/alpha.png" -fx 'v.r<0.004?0:u/v.r' "$work/color.png"
magick "$work/color.png" "$work/alpha.png" -alpha off -compose copy_opacity -composite \
  "$work/master.png"

iconset="$work/netkeep.iconset"
mkdir "$iconset"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$work/master.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$work/master.png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns -o "$out" "$iconset"
echo "wrote $out ($(wc -c <"$out" | tr -d ' ') bytes)"
