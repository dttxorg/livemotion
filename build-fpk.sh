#!/bin/sh
set -eu

APP_NAME="LiveMotion"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
FPK_DIR="${SCRIPT_DIR}/fpk"
DIST_DIR="${SCRIPT_DIR}/dist"
PACKAGE_NAME="${APP_NAME}.fpk"
OUTPUT_PATH="${DIST_DIR}/${PACKAGE_NAME}"

info() { printf '[INFO] %s\n' "$1"; }
fail() { printf '[ERROR] %s\n' "$1" >&2; exit 1; }

[ -d "$FPK_DIR" ] || fail "Missing fpk directory: $FPK_DIR"
[ -f "$FPK_DIR/manifest" ] || fail "Missing fpk/manifest"
[ ! -f "$FPK_DIR/manifest.json" ] || fail "fpk/manifest.json is not present in the installable fnOS samples; remove it from package source"
if [ -f "$FPK_DIR/ICON.PNG" ]; then
    ICON_64="$FPK_DIR/ICON.PNG"
elif [ -f "$FPK_DIR/icon.png" ]; then
    ICON_64="$FPK_DIR/icon.png"
else
    fail "Missing fpk/ICON.PNG or fpk/icon.png"
fi
[ -f "$FPK_DIR/ICON_256.PNG" ] || fail "Missing fpk/ICON_256.PNG"
[ -f "$FPK_DIR/docker/docker-compose.yaml" ] || fail "Missing fpk/docker/docker-compose.yaml"
[ ! -f "$FPK_DIR/docker/docker-compose.yml" ] || fail "Use docker-compose.yaml to match installable fnOS samples, not docker-compose.yml"
[ ! -f "$FPK_DIR/docker/Dockerfile" ] || fail "FPK must not include Dockerfile; use prebuilt image mode"
[ -f "$SCRIPT_DIR/Dockerfile" ] || fail "Missing project Dockerfile for local/CI image builds"

mkdir -p "$DIST_DIR"
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/livemotion-fpk.XXXXXX")
PKG_DIR="${WORK_DIR}/package"
APP_PAYLOAD_DIR="${WORK_DIR}/app"
mkdir -p "$PKG_DIR" "$APP_PAYLOAD_DIR/docker"

info "Staging package in $WORK_DIR"

# Prebuilt-image mode: FPK payload contains only Compose metadata, no source/build context.
cp "$FPK_DIR/docker/docker-compose.yaml" "$APP_PAYLOAD_DIR/docker/docker-compose.yaml"
cp -R "$FPK_DIR/ui" "$APP_PAYLOAD_DIR/ui"

(
    cd "$APP_PAYLOAD_DIR"
    COPYFILE_DISABLE=1 tar -czf "$PKG_DIR/app.tgz" docker ui
)

# FPK root layout follows downloaded installable samples.
cp "$FPK_DIR/manifest" "$PKG_DIR/manifest"
cp "$FPK_DIR/LiveMotion.sc" "$PKG_DIR/LiveMotion.sc"
cp "$ICON_64" "$PKG_DIR/ICON.PNG"
cp "$FPK_DIR/ICON_256.PNG" "$PKG_DIR/ICON_256.PNG"
cp -R "$FPK_DIR/cmd" "$PKG_DIR/cmd"
cp -R "$FPK_DIR/config" "$PKG_DIR/config"
cp -R "$FPK_DIR/ui" "$PKG_DIR/ui"
if [ -d "$FPK_DIR/wizard" ]; then
    cp -R "$FPK_DIR/wizard" "$PKG_DIR/wizard"
fi

# Patch checksum in manifest from app.tgz, as installable samples do.
if command -v md5sum >/dev/null 2>&1; then
    CHECKSUM=$(md5sum "$PKG_DIR/app.tgz" | awk '{print $1}')
else
    CHECKSUM=$(md5 -q "$PKG_DIR/app.tgz")
fi
python3 - "$PKG_DIR/manifest" "$CHECKSUM" <<'PY'
from pathlib import Path
import sys
manifest = Path(sys.argv[1])
checksum = sys.argv[2]
lines = manifest.read_text(encoding="utf-8").splitlines()
patched = []
seen = False
for line in lines:
    if line.startswith("checksum"):
        patched.append(f"checksum        = {checksum}")
        seen = True
    else:
        patched.append(line)
if not seen:
    patched.append(f"checksum        = {checksum}")
manifest.write_text("\n".join(patched) + "\n", encoding="utf-8")
PY

TAR_LIST="$WORK_DIR/package-files.txt"
: > "$TAR_LIST"
for item in LiveMotion.sc ICON.PNG ICON_256.PNG app.tgz cmd config manifest ui wizard; do
    if [ -e "$PKG_DIR/$item" ]; then
        printf '%s\n' "$item" >> "$TAR_LIST"
    fi
done

(
    cd "$PKG_DIR"
    COPYFILE_DISABLE=1 tar -czf "$OUTPUT_PATH" -T "$TAR_LIST"
)

info "Built $OUTPUT_PATH"
info "Temporary staging kept at $WORK_DIR for inspection. You may delete it manually after verification."
printf '%s\n' "$OUTPUT_PATH"
