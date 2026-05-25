#!/bin/sh
set -eu

PACKAGE="${1:-dist/LiveMotion.fpk}"
fail() { printf '[FAIL] %s\n' "$1" >&2; exit 1; }
pass() { printf '[OK] %s\n' "$1"; }

[ -f "$PACKAGE" ] || fail "Package not found: $PACKAGE"

TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/livemotion-verify.XXXXXX")
LIST_FILE="$TMP_DIR/list.txt"
mkdir -p "$TMP_DIR/root"

if tar -tzf "$PACKAGE" > "$LIST_FILE" 2>/dev/null; then
    FORMAT="tar.gz"
    tar -xzf "$PACKAGE" -C "$TMP_DIR/root"
elif command -v unzip >/dev/null 2>&1 && unzip -Z1 "$PACKAGE" > "$LIST_FILE" 2>/dev/null; then
    FORMAT="zip"
    unzip -q "$PACKAGE" -d "$TMP_DIR/root"
else
    fail "Package is neither readable tar.gz nor zip: $PACKAGE"
fi

ROOT="$TMP_DIR/root"
pass "Readable package format: $FORMAT"

if grep -Eq '(^|/)__MACOSX(/|$)|(^|/)\.DS_Store$|(^|/)\._' "$LIST_FILE"; then
    fail "Package contains macOS metadata (__MACOSX, .DS_Store, or AppleDouble files)"
fi
pass "No macOS metadata files"

if grep -Eq '(^|/)fpk/' "$LIST_FILE"; then
    fail "Package contains fpk/ directory; package root must directly contain manifest/cmd/config/ui/app.tgz"
fi
pass "No nested fpk/ directory"

if grep -Eq '^manifest\.json$|/manifest\.json$' "$LIST_FILE"; then
    fail "Package contains manifest.json, but downloaded installable fnOS samples do not"
fi
pass "No manifest.json in package root"

if grep -Eq '^docker/|/docker/' "$LIST_FILE"; then
    fail "Package root contains docker/. Installable Docker FPK sample places docker/ inside app.tgz"
fi
pass "No docker/ at package root"

require_file() {
    [ -f "$ROOT/$1" ] || fail "Missing required file: $1"
    pass "Found file: $1"
}

require_dir() {
    [ -d "$ROOT/$1" ] || fail "Missing required directory: $1"
    pass "Found directory: $1"
}

require_file manifest
require_file app.tgz
require_file ICON.PNG
require_file ICON_256.PNG
require_file LiveMotion.sc
require_dir cmd
require_dir config
require_dir ui
require_file cmd/main
require_file cmd/common
require_file cmd/installer
require_file cmd/install_init
require_file cmd/install_callback
require_file cmd/uninstall_init
require_file cmd/uninstall_callback
require_file cmd/upgrade_init
require_file cmd/upgrade_callback
require_file cmd/config_init
require_file cmd/config_callback
require_file cmd/service-setup
require_file config/privilege
require_file config/resource
require_file ui/config
require_file ui/images/64.png
require_file ui/images/256.png

for script in main common installer install_init install_callback uninstall_init uninstall_callback upgrade_init upgrade_callback config_init config_callback service-setup; do
    [ -x "$ROOT/cmd/$script" ] || fail "cmd/$script is not executable"
done
pass "Lifecycle scripts are executable"

python3 - "$ROOT/manifest" "$ROOT/ICON.PNG" "$ROOT/ICON_256.PNG" <<'PY'
from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
icon_64 = Path(sys.argv[2])
icon_256 = Path(sys.argv[3])

fields: dict[str, str] = {}
for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit(f"manifest line missing '=': {raw_line}")
    key, value = line.split("=", 1)
    fields[key.strip()] = value.strip()

required = {
    "appname": "livemotion",
    "display_name": "LiveMotion",
    "platform": "x86",
    "desktop_uidir": "ui",
    "desktop_applaunchname": "livemotion.Application",
    "service_port": "8011",
    "checkport": "false",
    "source": "thirdparty",
}
for key, expected in required.items():
    actual = fields.get(key)
    if actual != expected:
        raise SystemExit(f"manifest {key} expected {expected!r}, got {actual!r}")

version = fields.get("version", "")
if not re.fullmatch(r"\d+(?:\.\d+){1,2}(?:[-+][A-Za-z0-9_.-]+)?", version):
    raise SystemExit(f"manifest version is not valid semver-like value: {version!r}")

checksum = fields.get("checksum", "")
if not re.fullmatch(r"[0-9a-f]{32}", checksum):
    raise SystemExit("manifest checksum must be a 32-char lowercase md5")

if fields.get("fpk_version") != "0.1.2-r1":
    raise SystemExit("manifest fpk_version mismatch")

def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SystemExit(f"{path.name} is not a PNG file")
    return struct.unpack(">II", data[16:24])

if png_size(icon_64) != (64, 64):
    raise SystemExit("ICON.PNG must be 64x64 PNG")
if png_size(icon_256) != (256, 256):
    raise SystemExit("ICON_256.PNG must be 256x256 PNG")
PY
pass "manifest and icons validated"

APP_LIST="$TMP_DIR/app-list.txt"
mkdir -p "$TMP_DIR/app"
tar -tzf "$ROOT/app.tgz" > "$APP_LIST" 2>/dev/null || fail "app.tgz is not readable tar.gz"
tar -xzf "$ROOT/app.tgz" -C "$TMP_DIR/app"
if grep -Eq '(^|/)__MACOSX(/|$)|(^|/)\.DS_Store$|(^|/)\._' "$APP_LIST"; then
    fail "app.tgz contains macOS metadata"
fi
[ -f "$TMP_DIR/app/docker/docker-compose.yaml" ] || fail "app.tgz missing docker/docker-compose.yaml"
if [ -e "$TMP_DIR/app/docker/Dockerfile" ]; then
    fail "app.tgz must not contain docker/Dockerfile in prebuilt-image mode"
fi
if [ -e "$TMP_DIR/app/docker/requirements.txt" ]; then
    fail "app.tgz must not contain docker/requirements.txt in prebuilt-image mode"
fi
if [ -e "$TMP_DIR/app/docker/src" ]; then
    fail "app.tgz must not contain source code in prebuilt-image mode"
fi
[ -f "$TMP_DIR/app/ui/config" ] || fail "app.tgz missing ui/config"
[ -f "$TMP_DIR/app/ui/images/64.png" ] || fail "app.tgz missing ui/images/64.png"
[ -f "$TMP_DIR/app/ui/images/256.png" ] || fail "app.tgz missing ui/images/256.png"

if ! grep -q '/vol2/photos:/photos' "$TMP_DIR/app/docker/docker-compose.yaml"; then
    fail "docker-compose.yaml missing /vol2/photos:/photos"
fi
if ! grep -q '/vol1/docker/livephoto-worker/config:/config' "$TMP_DIR/app/docker/docker-compose.yaml"; then
    fail "docker-compose.yaml missing /vol1/docker/livephoto-worker/config:/config"
fi
if ! grep -q '8011:8011' "$TMP_DIR/app/docker/docker-compose.yaml"; then
    fail "docker-compose.yaml missing 8011:8011 port mapping"
fi
if grep -q 'build:' "$TMP_DIR/app/docker/docker-compose.yaml"; then
    fail "docker-compose.yaml must not contain build: in prebuilt-image mode"
fi
if grep -q 'ghcr.io/your-user' "$TMP_DIR/app/docker/docker-compose.yaml"; then
    fail "docker-compose.yaml must not contain placeholder ghcr.io/your-user"
fi
if ! grep -q 'image: ghcr.io/dttxorg/livemotion:0.1.2' "$TMP_DIR/app/docker/docker-compose.yaml"; then
    fail "docker-compose.yaml missing prebuilt image ghcr.io/dttxorg/livemotion:0.1.2"
fi
pass "app.tgz prebuilt-image docker and ui payload validated"

printf '[OK] Verification complete: %s\n' "$PACKAGE"
printf '[INFO] Extraction directory kept for inspection: %s\n' "$TMP_DIR"
