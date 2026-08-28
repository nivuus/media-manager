#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

DOVI_VER="2.1.3"
# dovi_tool : binaire statique musl x86_64 (releases GitHub officielles)
curl -fsSL -o dovi_tool.tar.gz \
  "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_VER}/dovi_tool-${DOVI_VER}-x86_64-unknown-linux-musl.tar.gz"
tar xzf dovi_tool.tar.gz
rm -f dovi_tool.tar.gz
chmod +x dovi_tool

# mkvmerge : extrait de l'AppImage mkvtoolnix + libs à côté du binaire
MKV_APPIMAGE="https://mkvtoolnix.download/appimage/MKVToolNix_GUI-92.0-x86_64.AppImage"
curl -fsSL -o mkvtoolnix.AppImage "${MKV_APPIMAGE}"
chmod +x mkvtoolnix.AppImage
./mkvtoolnix.AppImage --appimage-extract >/dev/null
cp squashfs-root/usr/bin/mkvmerge ./mkvmerge
mkdir -p lib
cp -a squashfs-root/usr/lib/. lib/ 2>/dev/null || true
rm -rf squashfs-root mkvtoolnix.AppImage
chmod +x mkvmerge
# cp -a preserves the AppImage squashfs' restrictive perms (dirs 700, some files
# non-readable); the container runs as PUID/PGID (non-root) over a ro bind-mount,
# so make everything world-readable/traversable.
find lib -type d -exec chmod a+rx {} +
find lib -type f -exec chmod a+r {} +

echo "OK: $(./dovi_tool --version 2>&1 | head -1)"
