#!/usr/bin/env bash
set -euo pipefail

PREFIX="/opt/roi"
INSTALL_OS_DEPS="0"
INSTALL_UDEV_RULES="0"
ADD_USER_GROUPS="0"
VENV_SYSTEM_SITE_PACKAGES="0"
OFFLINE_PIP="0"
WHEELHOUSE=""

EASY="0"

usage() {
  cat <<EOF
Usage: sudo $0 [--prefix /opt/roi]

Optional:
  --easy                       Do the "make it work" path (os deps + udev + user groups)
  --install-os-deps            Install recommended apt packages (python3-venv, can-utils, libusb, usbutils)
  --install-udev-rules         Install udev rules for USBTMC instruments (E-load)
  --add-user-groups            Add the invoking user to dialout/plugdev (for interactive runs)
  --venv-system-site-packages  Create the venv with --system-site-packages
  --offline                    Install Python packages from a local wheelhouse (no PyPI access)
  --wheelhouse <path>          Wheelhouse path (default: PREFIX/deploy/wheelhouse)

Installs ROI onto a Raspberry Pi:
- Copies this repo into PREFIX
- Creates venv at PREFIX/.venv
- Installs ROI into that venv
  - online default: pip install PREFIX/
  - offline mode: pip install --no-index --find-links <wheelhouse> PREFIX/
- Writes /etc/roi/roi.env if missing (per-host config overrides)
- Leaves systemd service install to scripts/service_install.sh
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --easy)
      EASY="1"; shift;;
    --prefix)
      PREFIX="$2"; shift 2;;
    --install-os-deps)
      INSTALL_OS_DEPS="1"; shift;;
    --install-udev-rules)
      INSTALL_UDEV_RULES="1"; shift;;
    --add-user-groups)
      ADD_USER_GROUPS="1"; shift;;
    --venv-system-site-packages)
      VENV_SYSTEM_SITE_PACKAGES="1"; shift;;
    --offline)
      OFFLINE_PIP="1"; shift;;
    --wheelhouse)
      WHEELHOUSE="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage; exit 2;;
  esac
done

if [[ "$EASY" == "1" ]]; then
  INSTALL_OS_DEPS="1"
  INSTALL_UDEV_RULES="1"
  ADD_USER_GROUPS="1"
  VENV_SYSTEM_SITE_PACKAGES="1"
fi

if [[ "$(id -u)" != "0" ]]; then
  echo "Please run as root (use sudo)." >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$INSTALL_OS_DEPS" == "1" ]]; then
  echo "[ROI] Installing OS dependencies via apt"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y       python3 python3-venv python3-pip python3-dev       can-utils       libusb-1.0-0       usbutils       rsync
  else
    echo "[ROI] WARNING: apt-get not found; skipping OS deps." >&2
  fi
fi

if [[ "$INSTALL_UDEV_RULES" == "1" ]]; then
  echo "[ROI] Installing udev rules for USBTMC instruments (E-load)"
  mkdir -p /etc/udev/rules.d

  # BK Precision 8600 series (VID:PID 2ec7:8800) - allow both libusb (pyvisa-py)
  # and /dev/usbtmc* kernel driver access.
  cat >/etc/udev/rules.d/99-roi-usbtmc.rules <<'EOF'
# ROI / instrument access

# BK Precision 8600-series Electronic Load (USBTMC)
# - "usb" rule covers libusb access (/dev/bus/usb/..)
# - "usbtmc" rule covers kernel driver node (/dev/usbtmc*)
SUBSYSTEM=="usb", ATTR{idVendor}=="2ec7", ATTR{idProduct}=="8800", MODE:="0666"
SUBSYSTEM=="usbtmc", ATTRS{idVendor}=="2ec7", ATTRS{idProduct}=="8800", MODE:="0666", GROUP:="plugdev"
EOF

  udevadm control --reload-rules || true
  udevadm trigger || true

  # Best-effort: ensure the kernel driver exists (enables /dev/usbtmc* fallback)
  modprobe usbtmc 2>/dev/null || true
  mkdir -p /etc/modules-load.d
  if [[ ! -f /etc/modules-load.d/usbtmc.conf ]]; then
    echo usbtmc >/etc/modules-load.d/usbtmc.conf
  fi

  echo "[ROI] NOTE: If the E-load was already plugged in, unplug/replug the USB cable now."
fi

if [[ "$ADD_USER_GROUPS" == "1" ]]; then
  # When invoked via sudo, SUDO_USER is the original user.
  TARGET_USER="${SUDO_USER:-}"
  if [[ -n "$TARGET_USER" && "$TARGET_USER" != "root" ]]; then
    echo "[ROI] Adding $TARGET_USER to groups: dialout plugdev"
    usermod -aG dialout,plugdev "$TARGET_USER" || true
    echo "[ROI] NOTE: You may need to log out/in for group changes to take effect."
  else
    echo "[ROI] Skipping user group changes (no SUDO_USER)"
  fi
fi

echo "[ROI] Installing to: $PREFIX"
mkdir -p "$PREFIX"
rsync -a --delete   --exclude ".git"   --exclude ".venv"   --exclude "venv"   --exclude "__pycache__"   --exclude "*.pyc"   --exclude ".pytest_cache"   --exclude ".mypy_cache"   --exclude ".ruff_cache"   --exclude "dist"   --exclude "build"   "$SRC_DIR/" "$PREFIX/"

# Stamp the deployed tree with the current git commit (if available).
# This is important because we intentionally exclude .git from the rsync copy,
# but we still want ROI to be able to print a meaningful revision on startup.
REV=""
if command -v git >/dev/null 2>&1; then
  if git -C "$SRC_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    REV="$(git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || true)"
  fi
fi
if [[ -n "$REV" ]]; then
  echo "[ROI] Git revision: $REV"
  mkdir -p "$PREFIX/src/roi"
  cat >"$PREFIX/src/roi/_revision.py" <<EOF
# Auto-generated by scripts/pi_install.sh
# Do not edit this file; it will be overwritten on reinstall.
GIT_COMMIT = "$REV"
EOF
fi

echo "[ROI] Ensuring venv at $PREFIX/.venv"
if [[ "$VENV_SYSTEM_SITE_PACKAGES" == "1" ]]; then
  python3 -m venv --system-site-packages "$PREFIX/.venv"
else
  python3 -m venv "$PREFIX/.venv"
fi

if [[ -z "$WHEELHOUSE" ]]; then
  WHEELHOUSE="$PREFIX/deploy/wheelhouse"
elif [[ "$WHEELHOUSE" != /* ]]; then
  WHEELHOUSE="$PREFIX/$WHEELHOUSE"
fi

if [[ "$OFFLINE_PIP" == "1" ]]; then
  echo "[ROI] Installing Python packages in offline mode"
  echo "[ROI] Wheelhouse: $WHEELHOUSE"

  if [[ ! -d "$WHEELHOUSE" ]]; then
    echo "[ROI] ERROR: wheelhouse directory not found: $WHEELHOUSE" >&2
    echo "[ROI] Build/copy an offline bundle first (see scripts/make_pi_dist.sh --offline)." >&2
    exit 1
  fi

  shopt -s nullglob
  WHEELHOUSE_PKGS=("$WHEELHOUSE"/*.whl "$WHEELHOUSE"/*.tar.gz "$WHEELHOUSE"/*.zip)
  shopt -u nullglob
  if [[ "${#WHEELHOUSE_PKGS[@]}" -eq 0 ]]; then
    echo "[ROI] ERROR: wheelhouse is empty: $WHEELHOUSE" >&2
    exit 1
  fi

  "$PREFIX/.venv/bin/pip" install --no-index --find-links "$WHEELHOUSE" -U pip setuptools wheel
  # Install ROI (and dependencies) into the venv from local artifacts only.
  "$PREFIX/.venv/bin/pip" install --no-index --find-links "$WHEELHOUSE" "$PREFIX"
else
  "$PREFIX/.venv/bin/pip" install -U pip setuptools wheel

  # Install ROI (and dependencies) into the venv.
  "$PREFIX/.venv/bin/pip" install "$PREFIX"
fi

# Env dir
mkdir -p /etc/roi
if [[ ! -f /etc/roi/roi.env ]]; then
  echo "[ROI] Writing /etc/roi/roi.env (edit for per-host overrides)"
  cp -n "$PREFIX/deploy/env/roi.env.example" /etc/roi/roi.env || true
fi

echo
echo "[ROI] Done."
echo "Edit /etc/roi/roi.env for per-host overrides (roi.config provides defaults)."
echo "Run: sudo $PREFIX/.venv/bin/roi"
echo "(Optional service) sudo $PREFIX/scripts/service_install.sh --prefix $PREFIX --enable --start"
