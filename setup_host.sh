#!/usr/bin/env bash
# setup_host.sh — One-time setup for a Raspberry Pi (Trixie, Desktop) acting
# as the production host for CM4 flashing on the Waveshare CM4-IO-Base-C.
#
# Run once as the desktop user (NOT as root). It will prompt for sudo when needed.
#
#   curl -sSL <your-url>/setup_host.sh -o ~/setup_host.sh
#   bash ~/setup_host.sh
#
# After this script finishes:
#   - rpiboot is built and installed under /opt/usbboot
#   - cm4_flasher.py and a desktop launcher are dropped on the Desktop
#   - The operator user is added to plugdev so flashing doesn't need sudo
#   - udev rules let the user open /dev/sdX devices written by rpiboot
#   - A Pi OS image is pre-downloaded to ~/cm4-images/
#
# Edit IMAGE_URL below if you want a different default image.

set -euo pipefail

# ---------- Configuration -----------------------------------------------------

# The OS image to bake into the production flow. Operators can switch to any
# .img / .img.xz file via the GUI, but this is the default.
IMAGE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-05-13/2025-05-13-raspios-bookworm-arm64-lite.img.xz"
IMAGE_BASENAME="$(basename "$IMAGE_URL")"

USBBOOT_REPO="https://github.com/raspberrypi/usbboot.git"
INSTALL_DIR="/opt/usbboot"
IMAGES_DIR="$HOME/cm4-images"
DESKTOP_DIR="$HOME/Desktop"
APP_DIR="$HOME/cm4-flasher"
ICON_PATH="$APP_DIR/icon.png"
LAUNCHER_PATH="$DESKTOP_DIR/cm4-flasher.desktop"
FLASHER_PATH="$APP_DIR/cm4_flasher.py"

# Path to the cm4_flasher.py source. The setup script looks for it next to
# itself first, falls back to ~/Downloads/cm4_flasher.py, then errors out.
SRC_CANDIDATES=(
    "$(dirname "$(readlink -f "$0")")/cm4_flasher.py"
    "$HOME/Downloads/cm4_flasher.py"
    "$HOME/cm4_flasher.py"
)

# ---------- Helpers -----------------------------------------------------------

say()   { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok()    { printf "  \033[1;32m✓ %s\033[0m\n" "$*"; }
warn()  { printf "  \033[1;33m⚠ %s\033[0m\n" "$*"; }
die()   { printf "\n\033[1;31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

require_not_root() {
    if [ "$(id -u)" -eq 0 ]; then
        die "Run this as your normal desktop user, not as root. (It will sudo when needed.)"
    fi
}

require_pi() {
    if ! grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
        warn "This doesn't look like a Raspberry Pi. Continuing anyway."
    fi
}

# ---------- Sanity checks -----------------------------------------------------

require_not_root
require_pi

# Locate cm4_flasher.py
FLASHER_SRC=""
for c in "${SRC_CANDIDATES[@]}"; do
    if [ -f "$c" ]; then FLASHER_SRC="$c"; break; fi
done
if [ -z "$FLASHER_SRC" ]; then
    die "Could not find cm4_flasher.py.
Place it next to this setup script, or in ~/Downloads/, then re-run."
fi
ok "Found flasher script: $FLASHER_SRC"

# ---------- 1. APT packages ---------------------------------------------------

say "Installing system packages (apt)…"
sudo apt-get update -y
sudo apt-get install -y \
    git build-essential pkg-config \
    libusb-1.0-0-dev libusb-1.0-0 \
    xz-utils unzip gzip \
    python3 python3-tk python3-pip python3-paramiko python3-zeroconf \
    openssl avahi-utils iputils-ping iputils-arping \
    udisks2 policykit-1 \
    yad zenity
ok "apt packages installed"

# ---------- 2. Build rpiboot --------------------------------------------------

say "Building rpiboot from source…"
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$USER":"$USER" "$INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone --depth=1 "$USBBOOT_REPO" "$INSTALL_DIR"
fi
make -C "$INSTALL_DIR" -j"$(nproc)"
ok "rpiboot built at $INSTALL_DIR/rpiboot"

# ---------- 3. udev rule so non-root users can run rpiboot --------------------

say "Installing udev rule for Broadcom BCM2711/2712 (no sudo needed for rpiboot)…"
sudo tee /etc/udev/rules.d/99-rpiboot.rules > /dev/null <<'EOF'
# Raspberry Pi CM4 / CM5 in mass-storage / rpiboot mode
SUBSYSTEM=="usb", ATTRS{idVendor}=="0a5c", ATTRS{idProduct}=="2711", MODE="0660", GROUP="plugdev", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0a5c", ATTRS{idProduct}=="2764", MODE="0660", GROUP="plugdev", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0a5c", ATTRS{idProduct}=="2712", MODE="0660", GROUP="plugdev", TAG+="uaccess"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
ok "udev rule installed"

# ---------- 4. Polkit rule so the user can write to /dev/sdX without sudo -----

say "Installing polkit rule for udisks2…"
sudo tee /etc/polkit-1/rules.d/50-cm4-flasher.rules > /dev/null <<EOF
// Let plugdev members operate on removable USB block devices without auth.
polkit.addRule(function(action, subject) {
    if (subject.isInGroup("plugdev")) {
        if (action.id.indexOf("org.freedesktop.udisks2.") === 0) {
            return polkit.Result.YES;
        }
    }
});
EOF
ok "polkit rule installed"

# ---------- 5. Add operator user to plugdev -----------------------------------

say "Adding $USER to plugdev group…"
sudo usermod -aG plugdev "$USER"
ok "User added to plugdev (takes effect on next login)"

# ---------- 6. Allow flashing without a sudo password ------------------------
# We narrow this to ONLY the binaries we need, not blanket NOPASSWD.

say "Configuring sudoers for the specific commands the flasher uses…"
SUDOERS_FILE="/etc/sudoers.d/010-cm4-flasher"
sudo tee "$SUDOERS_FILE" > /dev/null <<EOF
# Allow flashing CM4 boards without password prompts. Narrow to specific commands.
$USER ALL=(root) NOPASSWD: $INSTALL_DIR/rpiboot, /usr/bin/dd, /usr/sbin/eject, /usr/bin/sync, /bin/sync, /usr/bin/mount, /bin/mount, /usr/bin/umount, /bin/umount, /usr/bin/tee, /bin/tee, /usr/bin/chmod, /bin/chmod
EOF
sudo chmod 0440 "$SUDOERS_FILE"
sudo visudo -cf "$SUDOERS_FILE" || die "sudoers syntax error"
ok "sudoers rule installed"

# ---------- 7. Pre-download the OS image --------------------------------------

mkdir -p "$IMAGES_DIR"
if [ ! -f "$IMAGES_DIR/$IMAGE_BASENAME" ]; then
    say "Downloading OS image to $IMAGES_DIR/$IMAGE_BASENAME…"
    if command -v curl >/dev/null; then
        curl -L --progress-bar -o "$IMAGES_DIR/$IMAGE_BASENAME" "$IMAGE_URL"
    else
        wget -O "$IMAGES_DIR/$IMAGE_BASENAME" "$IMAGE_URL"
    fi
    ok "Image downloaded"
else
    ok "Image already present at $IMAGES_DIR/$IMAGE_BASENAME"
fi

# ---------- 8. Install the flasher script ------------------------------------

say "Installing cm4_flasher.py…"
mkdir -p "$APP_DIR"
cp "$FLASHER_SRC" "$FLASHER_PATH"
chmod +x "$FLASHER_PATH"
ok "Installed to $FLASHER_PATH"

# ---------- 9. Create a launcher icon -----------------------------------------

say "Creating desktop launcher…"
# Simple bundled icon. If you have a branded one, drop it at $ICON_PATH and
# this overwrite step won't run.
if [ ! -f "$ICON_PATH" ]; then
    # 256-px Raspberry Pi-ish SVG embedded then rasterised, or just use the
    # system raspberry icon if present. Fall back to a built-in one.
    if [ -f "/usr/share/icons/hicolor/scalable/apps/raspberry-pi-logo.svg" ]; then
        cp "/usr/share/icons/hicolor/scalable/apps/raspberry-pi-logo.svg" \
           "$APP_DIR/icon.svg"
        ICON_PATH="$APP_DIR/icon.svg"
    else
        # Fall back to a generic system icon
        ICON_PATH="utilities-system-monitor"
    fi
fi

mkdir -p "$DESKTOP_DIR"
cat > "$LAUNCHER_PATH" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=CM4 Flasher
Comment=Flash and verify Raspberry Pi CM4 modules
Exec=python3 $FLASHER_PATH
Icon=$ICON_PATH
Terminal=false
Categories=Utility;
StartupNotify=true
EOF
chmod +x "$LAUNCHER_PATH"

# On Trixie/Wayland the file manager may need this xattr to trust it
if command -v gio >/dev/null; then
    gio set "$LAUNCHER_PATH" metadata::trusted true 2>/dev/null || true
fi
ok "Desktop launcher created: $LAUNCHER_PATH"

# Also drop the same .desktop file into the user's applications dir so it
# shows up in the application menu.
mkdir -p "$HOME/.local/share/applications"
cp "$LAUNCHER_PATH" "$HOME/.local/share/applications/cm4-flasher.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

# ---------- 10. Write a small default config so the flasher auto-fills paths --

CONFIG_FILE="$HOME/.cm4_flasher.json"
if [ ! -f "$CONFIG_FILE" ]; then
cat > "$CONFIG_FILE" <<EOF
{
  "rpiboot_path": "$INSTALL_DIR/rpiboot",
  "bootfiles_dir": "$INSTALL_DIR/mass-storage-gadget64",
  "image_path": "$IMAGES_DIR/$IMAGE_BASENAME",
  "username": "pi",
  "hostname": "Carebloom{MAC}",
  "wifi_country": "US"
}
EOF
ok "Default config written to $CONFIG_FILE"
fi

# ---------- Done --------------------------------------------------------------

cat <<EOF

═══════════════════════════════════════════════════════════════════════════════
  Setup complete.

  Next steps:
    1. LOG OUT and back in (or reboot) so the plugdev group membership and
       the sudoers / udev rules take effect for your session.
    2. Double-click the "CM4 Flasher" icon on the Desktop to run the tool.

  Locations:
    Flasher script  : $FLASHER_PATH
    Desktop icon    : $LAUNCHER_PATH
    OS images       : $IMAGES_DIR
    rpiboot binary  : $INSTALL_DIR/rpiboot
    Operator config : $CONFIG_FILE
    Flash log       : ~/cm4_flash_log.csv  (appended on each flash)

  To run from a terminal:
    python3 $FLASHER_PATH
═══════════════════════════════════════════════════════════════════════════════
EOF
