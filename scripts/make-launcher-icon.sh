#!/usr/bin/env bash
# Regenerate netkeep/assets/netkeep.icns, the launcher app's icon (#754), from its source
# netkeep/assets/netkeep-icon.svg (already on Apple's icon grid: an 824 px tile centered
# on 1024, with its drop shadow). Run by hand on a Mac when the source changes, and commit
# the result: `netkeep setup` copies the committed file into the bundle, so building the
# launcher runs no image tools. Needs ImageMagick (magick) besides the Mac's own
# qlmanage, sips and iconutil. Writes a 512 px preview to $PREVIEW when it is set.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
source_svg="$root/netkeep/assets/netkeep-icon.svg"
out="$root/netkeep/assets/netkeep.icns"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

grep -q '</defs>' "$source_svg" || { echo "netkeep-icon.svg has no </defs> to anchor on"; exit 1; }
for background in white black; do
  # The same drawing on an opaque background, once white and once black.
  sed -e "s|</defs>|</defs><rect width=\"1024\" height=\"1024\" fill=\"$background\"/>|" \
    "$source_svg" >"$work/$background.svg"
  # Quick Look renders SVG with WebKit (gradient, clip and drop shadow included), as a
  # browser does, but only onto an opaque canvas.
  qlmanage -t -s 1024 -o "$work" "$work/$background.svg" >/dev/null
  test -f "$work/$background.svg.png" || { echo "qlmanage did not render $source_svg"; exit 1; }
done

# Difference matting: what the two renders disagree on is transparency (the corners and
# the shadow's falloff).
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
if [ -n "${PREVIEW:-}" ]; then
  cp "$iconset/icon_512x512.png" "$PREVIEW"
fi
echo "wrote $out ($(wc -c <"$out" | tr -d ' ') bytes)"
