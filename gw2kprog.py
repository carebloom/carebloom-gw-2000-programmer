#!/usr/bin/env python3
"""
Care Bloom GW2000 Programmer — production programming & verification tool
for the CM4 on the Waveshare CM4-IO-Base-C. Runs on a Raspberry Pi host
(Trixie/Bookworm with Desktop), launched by operators via a desktop icon.

Five tabs:
  1. Configure        — set image / username / password / hostname / Wi-Fi
  2. Program          — single button, runs the full programming workflow
  3. Verify           — finds the programmed board on the LAN and SSHes in
  4. App Installation — installs the Carebloom application onto the board
  5. Label Generation — prints GW-2000 QR-code labels on the Zebra ZD410

All sudo calls are pre-authorized via /etc/sudoers.d/010-gw2kprog so
operators never see a password prompt during normal use.
"""

import os
import re
import csv
import time
import json
import shlex
import queue
import socket
import shutil
import secrets
import threading
import subprocess
import ipaddress
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

try:
    import paramiko
except ImportError:
    paramiko = None

try:
    import qrcode
except ImportError:
    qrcode = None


# =============================================================================
# Defaults — adjust if your production layout differs.
# =============================================================================

DEFAULT_RPIBOOT_CANDIDATES = [
    "/opt/usbboot/rpiboot",
    str(Path.home() / "usbboot/rpiboot"),
    "/usr/local/bin/rpiboot",
]
DEFAULT_BOOTFILES_SUBDIR = "mass-storage-gadget64"
DEFAULT_IMAGES_DIR = str(Path.home() / "gw2k-images")
DEFAULT_USERNAME = "pi"
DEFAULT_PASSWORD = "raspberry"
DEFAULT_HOSTNAME = "CareBloom{MAC}"   # {MAC} replaced with eth0 MAC at first boot

# The gateway brings up its own Wi-Fi access point on wlan0 as the backhaul
# network that CareBloom room anchors connect to. The SSID is derived on the
# board from the eth0 MAC (same {MAC} substitution as the hostname), so the
# AP name matches the hostname. The password below is the AP's WPA2 key.
DEFAULT_AP_SSID = "CareBloom{MAC}"    # {MAC} = eth0 MAC, substituted at first boot
DEFAULT_AP_PASSWORD = "CareBloomDemo2021"

# Minimum time (seconds) that must elapse between programming finishing and
# Verify starting discovery. First boot runs firstrun.sh and then reboots, so
# the board isn't discoverable until ~90-120 s after power-on. If the operator
# clicks Find and Verify sooner, Verify waits out the remainder automatically.
POST_PROGRAM_SETTLE_SECS = 120

# Carebloom application installation
DEFAULT_APP_NAME = "CARE001"          # top-level folder name inside the app zip
DEFAULT_APPS_DIR = str(Path.home() / "gw2k-apps")  # where app zips live on host

# The programmer looks here by default for the gateway application firmware
# tarball (a .tar.gz). This is a "gateway-firmware" subfolder next to the
# gw2kprog.py script itself, so it travels with the install / repo.
DEFAULT_FIRMWARE_DIR = str(Path(__file__).resolve().parent / "gateway-firmware")

# Raspberry Pi MAC OUI prefixes, used to narrow LAN discovery. Raspberry Pi
# registers new blocks periodically, so this list will go stale over time -
# discovery also falls back to matching the CareBloom* hostname (see below),
# which catches boards whose OUI isn't listed here yet.
PI_MAC_PREFIXES = ("b8:27:eb", "dc:a6:32", "e4:5f:01",
                   "2c:cf:67", "d8:3a:dd", "28:cd:c1",
                   "88:a2:9e")

LOG_FILE = str(Path.home() / "gw2k_program_log.csv")
CONFIG_FILE = str(Path.home() / ".gw2kprog.json")

# Full transcripts of each operation, for sharing when something goes wrong.
# Each run overwrites the file so it always reflects the most recent attempt.
TRANSCRIPT_DIR = str(Path.home() / "gw2k-programmer-logs")
PROGRAM_LOG = os.path.join(TRANSCRIPT_DIR, "program_transcript.log")
VERIFY_LOG = os.path.join(TRANSCRIPT_DIR, "verify_transcript.log")
INSTALL_LOG = os.path.join(TRANSCRIPT_DIR, "install_transcript.log")

# -----------------------------------------------------------------------------
# Label generation (Zebra ZD410 thermal printer, ZPL over raw USB)
#
# This matches the label format produced by the CareBloom Anchor Programmer
# (an2kprog.py) so GW-2000 gateway labels are visually identical to AN-2000
# anchor labels. The only differences:
#   - Part number is GW-2000 (not AN-2000)
#   - The QR code encodes the CM4's eth0 MAC address
# -----------------------------------------------------------------------------
PRINTER_DEVICE  = "/dev/usb/lp0"   # raw USB device node on Linux/RPi
PRINTER_DPI     = 300              # ZD410 300 dpi model
LABEL_WIDTH_IN  = 1.0              # physical label width in inches
LABEL_HEIGHT_IN = 0.5              # physical label height in inches

LABEL_PRODUCT_PN = "GW-2000"       # part number printed on every GW2000 label
LABELS_PER_PRINT = 2               # copies of each label per print job (one for
                                   # the board, one for paperwork / backup)

# On-screen preview size (2:1 aspect ratio, matching a 1" x 0.5" sticker).
LABEL_PREVIEW_W = 320
LABEL_PREVIEW_H = 160


# =============================================================================
# Helpers
# =============================================================================

# Matches ANSI escape sequences (colors, cursor moves) and terminal control
# bytes that apt/dpkg emit when they think they're on a real terminal.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][AB0]|\x1b[78]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def strip_ansi(s):
    """Remove ANSI escape sequences and stray control characters so the
    text renders cleanly in a plain Tk text widget."""
    s = _ANSI_RE.sub("", s)
    s = _CTRL_RE.sub("", s)
    return s


def which(cmd):
    return shutil.which(cmd)


def run_stream(cmd, log_cb, shell=False, timeout=None):
    """Run a command, stream stdout+stderr to log_cb, return (rc, output)."""
    log_cb(f"$ {cmd if isinstance(cmd, str) else ' '.join(shlex.quote(c) for c in cmd)}")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, shell=shell,
            bufsize=1, universal_newlines=True,
        )
    except FileNotFoundError as e:
        log_cb(f"ERROR: {e}")
        return 127, ""

    lines = []
    start = time.time()
    for line in proc.stdout:
        line = line.rstrip()
        lines.append(line)
        log_cb(line)
        if timeout and (time.time() - start) > timeout:
            proc.kill()
            log_cb(f"[killed after {timeout}s]")
            break
    proc.wait()
    log_cb(f"[exit {proc.returncode}]")
    return proc.returncode, "\n".join(lines)


def lsblk_json():
    """Return list of dicts for each block device via lsblk -J."""
    try:
        out = subprocess.check_output(
            ["lsblk", "-J", "-b", "-o",
             "NAME,SIZE,TYPE,RM,RO,TRAN,MODEL,VENDOR,MOUNTPOINT,HOTPLUG,LABEL"],
            text=True,
        )
        return json.loads(out).get("blockdevices", [])
    except Exception:
        return []


def find_cm4_disk():
    """Return (dev_path, size_bytes, name) of a CM4 eMMC, or None.

    Strict rules so we never write to the wrong disk:
      - Type 'disk' (not a partition, not a loop, not the host's root)
      - Transport USB (rpiboot exposes the eMMC over USB)
      - Marked removable (rm == true)
      - Size between 1 GB and 64 GB
      - Not the device backing the running root filesystem

    NOTE: we deliberately do NOT require the 'hotplug' flag. The rpiboot
    mass-storage gadget reports hotplug=false even though the eMMC is a
    removable USB device, so requiring hotplug would (and did) miss it.
    """
    # Find the root device so we exclude it.
    root_dev = ""
    try:
        out = subprocess.check_output(["findmnt", "-n", "-o", "SOURCE", "/"],
                                       text=True).strip()
        # Strip partition suffix (e.g. /dev/mmcblk0p2 -> /dev/mmcblk0, /dev/sda1 -> /dev/sda)
        root_dev = re.sub(r"p?\d+$", "", out)
    except Exception:
        pass

    candidates = []
    for d in lsblk_json():
        if d.get("type") != "disk":
            continue
        name = d.get("name", "")
        path = f"/dev/{name}"
        if root_dev and path == root_dev:
            continue
        if d.get("tran") != "usb":
            continue
        # Must be flagged removable. The rpiboot eMMC gadget reports rm=true.
        if not d.get("rm"):
            continue
        try:
            size = int(d.get("size") or 0)
        except Exception:
            size = 0
        if size < 1 * 1024**3 or size > 64 * 1024**3:
            continue
        candidates.append((path, size, (d.get("model") or d.get("vendor") or "").strip()))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[1])
    return candidates[0]


def _human(n):
    if n is None:
        return "?"
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n/div:.2f} {unit}"
    return f"{n} B"


def guess_rpiboot():
    for c in DEFAULT_RPIBOOT_CANDIDATES:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return which("rpiboot") or ""


def guess_bootfiles(rpiboot_path):
    if not rpiboot_path:
        return ""
    cand = os.path.join(os.path.dirname(rpiboot_path), DEFAULT_BOOTFILES_SUBDIR)
    return cand if os.path.isdir(cand) else ""


def local_subnets():
    """Return list of IPv4 subnets the host is on (CIDR strings)."""
    nets = []
    try:
        out = subprocess.check_output(["ip", "-4", "-o", "addr"], text=True)
    except Exception:
        return nets
    for line in out.splitlines():
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", line)
        if not m:
            continue
        cidr = m.group(1)
        if cidr.startswith("127."):
            continue
        try:
            net = ipaddress.ip_network(cidr, strict=False)
            # Accept any plausible LAN prefix. The ping-sweep caps host count
            # itself; mDNS discovery doesn't care about subnet size at all.
            if 8 <= net.prefixlen <= 30:
                nets.append(str(net))
        except Exception:
            pass
    return nets


def unmount_all_partitions(disk_path, log_cb):
    """Unmount every partition of disk_path. Returns True if all umounted OK."""
    name = os.path.basename(disk_path)
    ok = True
    for d in lsblk_json():
        if d.get("name") != name:
            continue
        for child in d.get("children", []) or []:
            mp = child.get("mountpoint") or child.get("mountpoints")
            cp = f"/dev/{child.get('name')}"
            if mp:
                rc, _ = run_stream(["sudo", "umount", cp], log_cb)
                if rc != 0:
                    # Try lazy
                    rc, _ = run_stream(["sudo", "umount", "-l", cp], log_cb)
                    if rc != 0:
                        ok = False
    return ok


def find_bootfs_mount(disk_path, deadline):
    """Wait for the bootfs partition of disk_path to be auto-mounted; mount
    manually via udisksctl if it isn't. Returns mount path or None."""
    name = os.path.basename(disk_path)
    while time.time() < deadline:
        for d in lsblk_json():
            if d.get("name") != name:
                continue
            for child in d.get("children", []) or []:
                cp = f"/dev/{child.get('name')}"
                mp = child.get("mountpoint")
                # bootfs is the FAT32 boot partition. Prefer the label that
                # lsblk now reports directly (LABEL is in the -o list); only
                # fall back to 'sudo blkid' if lsblk hasn't picked the label
                # up yet (can happen right after dd rewrites the partition
                # table, before udev re-probes).
                label = (child.get("label") or "")
                if not label:
                    try:
                        blkid = subprocess.check_output(
                            ["sudo", "blkid", "-o", "export", cp],
                            text=True, stderr=subprocess.DEVNULL)
                        info = {k: v for k, v in (
                            line.split("=", 1) for line in blkid.splitlines()
                            if "=" in line)}
                    except Exception:
                        info = {}
                    label = info.get("LABEL", "")
                if label.lower() != "bootfs":
                    continue
                if mp:
                    return mp
                # Auto-mount via udisks2
                try:
                    out = subprocess.check_output(
                        ["udisksctl", "mount", "-b", cp],
                        text=True, stderr=subprocess.STDOUT)
                    m = re.search(r"at (.+?)\.$", out.strip())
                    if m:
                        return m.group(1)
                except subprocess.CalledProcessError as e:
                    # Could already be mounted by polkit-driven auto-mount
                    msg = e.output or ""
                    m = re.search(r"already mounted at (.+?)\.", msg)
                    if m:
                        return m.group(1)
        time.sleep(1)
    return None


# =============================================================================
# Label generation — ZPL builder + Zebra ZD410 printer
#
# Ported from the CareBloom Anchor Programmer (an2kprog.py) so GW-2000 labels
# are byte-for-byte the same format as AN-2000 anchor labels, except for the
# part number (GW-2000) and the MAC encoded in the QR code.
# =============================================================================

class LabelPrinterError(Exception):
    """Raised when label printing fails."""


def normalize_mac(mac):
    """Return a MAC as 12 uppercase hex characters with no separators.

    Accepts the common forms: 'd8:3a:dd:c4:55:70', 'd8-3a-dd-c4-55-70',
    'd83add c45570', 'd83addc45570'. Raises ValueError if the result is not
    exactly 12 hex digits."""
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", mac or "")
    if len(cleaned) != 12:
        raise ValueError(
            f"MAC must be 12 hex digits; got {len(cleaned)} "
            f"from '{mac}'.")
    return cleaned.upper()


def format_mac_colons(mac):
    """Return a normalized MAC formatted with colons (AA:BB:CC:DD:EE:FF)."""
    m = normalize_mac(mac)
    return ":".join(m[i:i + 2] for i in range(0, 12, 2))


def build_label_zpl(mac, pn=LABEL_PRODUCT_PN, lot="",
                     copies=LABELS_PER_PRINT):
    """Build a ZPL byte string for a 1" x 0.5" label: QR code on the left,
    three lines of text on the right:

        +-----------------------------+
        |  QR     PN:GW-2000          |
        |  QR     LOT:202605          |
        |  QR     D83ADDC45570        |
        +-----------------------------+

    The QR encodes the bare 12-char MAC (no colons). PN and LOT are text only.
    This mirrors an2kprog.py's build_label_zpl() exactly so the two product
    families produce visually identical labels.
    """
    mac_clean = normalize_mac(mac)
    pn_clean  = (pn  or "").strip()
    lot_clean = (lot or "").strip()

    line_pn  = f"PN:{pn_clean}"
    line_lot = f"LOT:{lot_clean}"
    line_mac = mac_clean   # no prefix — the QR makes it obvious this is the MAC

    # Label dimensions in dots (300 dpi): 300 x 150.
    label_w = int(LABEL_WIDTH_IN  * PRINTER_DPI)
    label_h = int(LABEL_HEIGHT_IN * PRINTER_DPI)

    # ---- Manual offset tuning ----
    # Positive X shifts RIGHT; positive Y shifts DOWN. 1 dot = 1/300".
    # These values are carried over from an2kprog.py's tuned layout.
    qr_offset_x   = 20
    qr_offset_y   = 7
    text_offset_x = 25
    text_offset_y = 15

    # ZPL Font A renders characters at roughly 70% of the specified width.
    font_w_render_ratio = 0.70

    # Print darkness 0-30 (direct-thermal labels want ~15-25).
    darkness = 20

    # ---- QR geometry ----
    # 12-char alphanumeric data with H error correction -> Version 2 = 25
    # modules per side. At magnification N the QR is 25*N dots wide.
    qr_mag = 4
    qr_size_est = 25 * qr_mag
    qr_x = 6
    qr_y = max(0, (label_h - qr_size_est) // 2)

    # ---- Text geometry ----
    text_area_left  = qr_x + qr_size_est + 6
    text_area_right = label_w - 4
    text_area_w     = text_area_right - text_area_left

    longest_len = max(len(line_pn), len(line_lot), len(line_mac))
    n_lines = 3

    font_w = max(7, int(text_area_w / (longest_len * font_w_render_ratio)))

    label_h_usable = label_h - 8
    font_h_max = int(label_h_usable / (n_lines + (n_lines - 1) / 6))
    font_h = max(10, min(font_h_max, int(font_w * 2.0)))
    font_h = max(font_h, font_w)

    line_gap = max(2, font_h // 6)
    text_block_h = font_h * n_lines + line_gap * (n_lines - 1)

    text_x = text_area_left
    text_block_top = max(0, (label_h - text_block_h) // 2)
    text_y_pn  = text_block_top
    text_y_lot = text_y_pn  + font_h + line_gap
    text_y_mac = text_y_lot + font_h + line_gap

    # "HA," = high (~30%) error correction, automatic data mode.
    zpl = (
        "^XA"
        "^MMT"
        "^MNY"
        f"^PW{label_w}"
        f"^LL{label_h}"
        "^LS0"
        "^LH0,0"
        "^PON"
        f"^MD{darkness}"
        # --- QR code (encodes MAC only) ---
        f"^FO{qr_x + qr_offset_x},{qr_y + qr_offset_y}"
        f"^BQN,2,{qr_mag}"
        f"^FDHA,{mac_clean}^FS"
        # --- Line 1: PN ---
        f"^FO{text_x + text_offset_x},{text_y_pn + text_offset_y}"
        f"^A0N,{font_h},{font_w}"
        f"^FD{line_pn}^FS"
        # --- Line 2: LOT ---
        f"^FO{text_x + text_offset_x},{text_y_lot + text_offset_y}"
        f"^A0N,{font_h},{font_w}"
        f"^FD{line_lot}^FS"
        # --- Line 3: MAC ---
        f"^FO{text_x + text_offset_x},{text_y_mac + text_offset_y}"
        f"^A0N,{font_h},{font_w}"
        f"^FD{line_mac}^FS"
        f"^PQ{int(copies)},0,0,N"
        "^XZ"
    )
    return zpl.encode("utf-8")


def print_label(mac, pn=LABEL_PRODUCT_PN, lot="",
                copies=LABELS_PER_PRINT, device=PRINTER_DEVICE):
    """Send a label print job to the ZD410. Raises LabelPrinterError on
    failure. The printer must be powered on, loaded with stock, and present
    at `device` (typically /dev/usb/lp0 on a Pi)."""
    zpl = build_label_zpl(mac, pn=pn, lot=lot, copies=copies)
    try:
        with open(device, "wb") as f:
            f.write(zpl)
    except FileNotFoundError:
        raise LabelPrinterError(
            f"Printer device '{device}' not found. Is the ZD410 plugged in "
            "and powered on?")
    except PermissionError:
        raise LabelPrinterError(
            f"Permission denied accessing '{device}'. Add your user to the "
            "'lp' group: sudo usermod -a -G lp $USER (then log out / in).")
    except OSError as e:
        raise LabelPrinterError(f"Failed to write to printer: {e}")


def calibrate_printer(device=PRINTER_DEVICE):
    """Run the ZD410's automatic media calibration. Needed when a new roll of
    labels is loaded or when prints come out misaligned."""
    label_w = int(LABEL_WIDTH_IN  * PRINTER_DPI)
    label_h = int(LABEL_HEIGHT_IN * PRINTER_DPI)
    zpl = (
        "^XA"
        "^MMT"
        "^MNY"
        f"^PW{label_w}"
        f"^LL{label_h}"
        "^LH0,0"
        "^LS0"
        "^PON"
        "^MD0"
        "^PR4,4"
        "^JUS"
        "^XZ"
        "~JC"
    ).encode("utf-8")
    try:
        with open(device, "wb") as f:
            f.write(zpl)
    except FileNotFoundError:
        raise LabelPrinterError(
            f"Printer device '{device}' not found. Is the ZD410 plugged in?")
    except PermissionError:
        raise LabelPrinterError(
            f"Permission denied accessing '{device}'.")
    except OSError as e:
        raise LabelPrinterError(f"Failed to write to printer: {e}")


def qr_matrix(data):
    """Return the QR code module matrix (list of list of bool) for `data`,
    using the same parameters as the printed label (H error correction).
    Requires the `qrcode` package; returns None if it isn't installed."""
    if qrcode is None:
        return None
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=1,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


# =============================================================================
# Main application
# =============================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Care Bloom Gateway Programmer")
        try:
            self.tk.call("tk", "scaling", 1.3)
        except Exception:
            pass
        self.geometry("1100x900")
        self.minsize(960, 700)

        self._log_q = queue.Queue()
        self.after(80, self._drain_log)

        self.rpiboot_path = tk.StringVar(value=guess_rpiboot())
        self.bootfiles_dir = tk.StringVar(value=guess_bootfiles(self.rpiboot_path.get()))
        self.image_path = tk.StringVar(value="")
        self.username = tk.StringVar(value=DEFAULT_USERNAME)
        self.password = tk.StringVar(value=DEFAULT_PASSWORD)
        self.hostname = tk.StringVar(value=DEFAULT_HOSTNAME)
        self.wifi_ssid = tk.StringVar(value=DEFAULT_AP_SSID)
        self.wifi_psk = tk.StringVar(value=DEFAULT_AP_PASSWORD)
        self.wifi_country = tk.StringVar(value="US")

        self.found_ip = tk.StringVar(value="")
        self.found_host = tk.StringVar(value="")

        # Wall-clock time (time.time()) when programming last finished
        # successfully. Verify uses this to enforce a minimum settle delay so
        # the board has time to finish first boot (firstrun.sh + its reboot)
        # before discovery starts. None = no program run this session.
        self.program_finished_at = None

        # Unique token written onto the board during the last Program run and
        # copied by firstrun.sh to /etc/gw2k_program_id on the booted system.
        # Verify reads it back over SSH to positively identify the board just
        # programmed. None = no program run this session (Verify then falls
        # back to the LAN-snapshot diff and, ultimately, an operator pick).
        self.program_id = None

        # Set of CareBloom gateway hostnames already on the LAN at the moment
        # programming started. Verify diffs the current LAN against this to
        # identify the board that was JUST programmed (the new arrival),
        # instead of guessing. None = no snapshot taken this session.
        self.lan_snapshot = None

        # App installation
        self.app_zip_path = tk.StringVar(value="")
        self.app_name = tk.StringVar(value=DEFAULT_APP_NAME)
        self.install_host = tk.StringVar(value="")

        # Label generation
        self.label_mac = tk.StringVar(value="")
        self.label_lot = tk.StringVar(value="")
        self.label_copies = tk.StringVar(value=str(LABELS_PER_PRINT))

        self.steps = []
        self.expected_hostname = None
        self.expected_user = None
        self.expected_pw = None

        self._build_ui()
        self._load_defaults(silent=True)

        # If the default config left image_path empty, pick the newest .img.xz
        # in DEFAULT_IMAGES_DIR.
        if not self.image_path.get():
            self._auto_pick_image()

        # Likewise, if no app archive is set, pick the newest firmware tarball
        # from the gateway-firmware folder next to the script.
        if not self.app_zip_path.get():
            self._auto_pick_firmware()

    def _auto_pick_image(self):
        d = DEFAULT_IMAGES_DIR
        if not os.path.isdir(d):
            return
        imgs = [os.path.join(d, f) for f in os.listdir(d)
                if f.lower().endswith((".img", ".img.xz", ".xz", ".zip", ".gz"))]
        if not imgs:
            return
        imgs.sort(key=os.path.getmtime, reverse=True)
        self.image_path.set(imgs[0])
        self.log(f"Auto-picked image: {imgs[0]}")

    def _auto_pick_firmware(self):
        """Pick the newest application firmware tarball from the
        gateway-firmware folder, so the App archive field is pre-filled on a
        fresh start."""
        d = DEFAULT_FIRMWARE_DIR
        if not os.path.isdir(d):
            return
        tarballs = [os.path.join(d, f) for f in os.listdir(d)
                    if f.lower().endswith((".tar.gz", ".tgz", ".tar", ".zip"))]
        if not tarballs:
            return
        tarballs.sort(key=os.path.getmtime, reverse=True)
        self.app_zip_path.set(tarballs[0])
        self.log(f"Auto-picked firmware: {tarballs[0]}")

    # ---- UI ----------------------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(side="top", fill="x")
        ttk.Label(top, text="Care Bloom GW2000 Programmer",
                  font=("DejaVu Sans", 18, "bold")).pack(side="left")

        nb = ttk.Notebook(self)
        nb.pack(side="top", fill="both", expand=True, padx=8, pady=8)
        self.cfg_tab = ttk.Frame(nb)
        self.program_tab = ttk.Frame(nb)
        self.verify_tab = ttk.Frame(nb)
        self.install_tab = ttk.Frame(nb)
        self.label_tab = ttk.Frame(nb)
        nb.add(self.cfg_tab, text="1. Configure")
        nb.add(self.program_tab, text="2. Program")
        nb.add(self.verify_tab, text="3. Verify")
        nb.add(self.install_tab, text="4. App Installation")
        nb.add(self.label_tab, text="5. Label Generation")
        self.notebook = nb

        self._build_cfg_tab(self.cfg_tab)
        self._build_program_tab(self.program_tab)
        self._build_verify_tab(self.verify_tab)
        self._build_install_tab(self.install_tab)
        self._build_label_tab(self.label_tab)

    def _row(self, parent, r, label, var, browse=None, show=None, hint=""):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=6, pady=4)
        entry = ttk.Entry(parent, textvariable=var, width=70, show=show)
        entry.grid(row=r, column=1, sticky="we", padx=4, pady=4)
        if hint:
            ttk.Label(parent, text=hint, foreground="#888").grid(
                row=r, column=3, sticky="w")
        if browse:
            kind, title = browse
            def pick():
                if kind == "file":
                    tl = title.lower()
                    if "image" in tl:
                        start = DEFAULT_IMAGES_DIR
                    elif "archive" in tl:
                        # App archive browser opens at the gateway-firmware
                        # folder if it exists, else the home directory.
                        start = (DEFAULT_FIRMWARE_DIR
                                 if os.path.isdir(DEFAULT_FIRMWARE_DIR)
                                 else os.path.expanduser("~"))
                    else:
                        start = os.path.expanduser("~")
                    p = filedialog.askopenfilename(
                        title=title, initialdir=start)
                else:
                    p = filedialog.askdirectory(title=title)
                if p:
                    var.set(p)
            ttk.Button(parent, text="Browse…", command=pick).grid(
                row=r, column=2, padx=4)
        parent.columnconfigure(1, weight=1)

    def _build_cfg_tab(self, parent):
        wrap = ttk.Frame(parent, padding=12)
        wrap.pack(fill="both", expand=True)

        f1 = ttk.LabelFrame(wrap, text="Tooling (auto-detected — change only if needed)")
        f1.pack(fill="x", pady=(0, 8))
        self._row(f1, 0, "rpiboot binary:", self.rpiboot_path,
                  browse=("file", "Select rpiboot"))
        self._row(f1, 1, "Boot-files dir:", self.bootfiles_dir,
                  browse=("dir", "Select mass-storage-gadget64"))
        self._row(f1, 2, "OS image:", self.image_path,
                  browse=("file", "Select OS image (.img / .img.xz)"))

        f2 = ttk.LabelFrame(wrap, text="First-boot configuration")
        f2.pack(fill="x", pady=(0, 8))
        self._row(f2, 0, "Username:", self.username)
        self._row(f2, 1, "Password:", self.password, show="•")
        self._row(f2, 2, "Hostname:", self.hostname,
                  hint="  ({MAC} = full MAC, {MAC6} = last 6, {MACUPPER} = uppercase)")
        self._row(f2, 3, "AP SSID:", self.wifi_ssid,
                  hint="  (Wi-Fi AP for room anchors; {MAC} = eth0 MAC)")
        self._row(f2, 4, "AP password:", self.wifi_psk, show="•")
        self._row(f2, 5, "Wi-Fi country:", self.wifi_country)

        f4 = ttk.LabelFrame(wrap, text="Carebloom application")
        f4.pack(fill="x", pady=(0, 8))
        self._row(f4, 0, "App archive:", self.app_zip_path,
                  browse=("file", "Select Carebloom app archive (.tar.gz / .zip)"))
        self._row(f4, 1, "App folder name:", self.app_name,
                  hint="  (top-level folder inside the archive, e.g. CARE001)")

        f3 = ttk.Frame(wrap)
        f3.pack(fill="x", pady=(8, 0))
        ttk.Button(f3, text="Save these as defaults",
                   command=self._save_defaults).pack(side="left", padx=4)
        ttk.Button(f3, text="Load defaults",
                   command=self._load_defaults).pack(side="left", padx=4)

        ttk.Label(wrap, justify="left", foreground="#000",
                  font=("DejaVu Sans", 13, "bold"),
                  text="Follow these steps before opening the "
                       "'2. Program' tab.").pack(anchor="w", pady=(12, 2))
        ttk.Label(wrap, justify="left", foreground="#000", text=(
            "1. Set the BOOT switch on the gateway to the ON position.\n"
            "2. Connect one of the blue USB 3.0 ports on the programmer "
            "to the gateway USB-C port.\n"
            "3. Plug the LAN cable in to the ethernet port on the gateway."
        )).pack(anchor="w", pady=(0, 0))

    def _build_program_tab(self, parent):
        wrap = ttk.Frame(parent, padding=12)
        wrap.pack(fill="both", expand=True)

        steps_frame = ttk.LabelFrame(wrap, text="Steps")
        steps_frame.pack(fill="both", expand=False)

        step_defs = [
            ("Detect gateway via rpiboot",
             "Plug USB-C into this Pi (BOOT switch ON; the cable powers the board)."),
            ("Identify eMMC",
             "Confirm a small (~8/16/32 GB) USB disk appears."),
            ("Unmount any partitions",
             "Detach any auto-mounted partitions."),
            ("Program image",
             "Stream the image straight to the block device."),
            ("Re-attach for config",
             "rpiboot to expose bootfs partition."),
            ("Write first-boot config",
             "Creates user, sets password, enables SSH, sets hostname/Wi-Fi."),
            ("Sync and eject",
             "Flush + power-off the disk."),
        ]
        self.steps = []
        for label, sub in step_defs:
            row = ttk.Frame(steps_frame)
            row.pack(fill="x", padx=8, pady=2)
            icon = ttk.Label(row, text="○", width=2, font=("DejaVu Sans Mono", 14))
            icon.pack(side="left")
            ttk.Label(row, text=label,
                      font=("DejaVu Sans", 12, "bold")).pack(side="left", padx=4)
            ttk.Label(row, text="— " + sub,
                      foreground="#777").pack(side="left", padx=4)
            self.steps.append({"icon": icon, "status": "pending"})

        ctrl = ttk.Frame(wrap)
        ctrl.pack(fill="x", pady=12)
        self.start_btn = ttk.Button(ctrl, text="Program GW2000", width=15,
                                    command=self._start_program_thread)
        self.start_btn.pack(side="left", padx=4, ipadx=20, ipady=6)
        ttk.Button(ctrl, text="Reset", width=15,
                   command=self._reset_steps).pack(
            side="right", padx=4, ipadx=20, ipady=6)

        statusf = ttk.LabelFrame(wrap, text="Status")
        statusf.pack(fill="both", expand=True, pady=(8, 0))
        self.program_status = ttk.Label(statusf, text="Ready.",
                                       font=("DejaVu Sans", 13))
        self.program_status.pack(anchor="w", padx=6, pady=6)

        self.program_results = scrolledtext.ScrolledText(
            statusf, height=14, wrap="word", font=("DejaVu Sans Mono", 10))
        self.program_results.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.program_results.configure(state="disabled")

        ttk.Label(statusf,
                  text=f"Full transcript saved to: {PROGRAM_LOG}",
                  foreground="#888").pack(anchor="w", padx=6, pady=(0, 6))

    def _build_verify_tab(self, parent):
        wrap = ttk.Frame(parent, padding=12)
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, justify="left", text=(
            "After programming has finished and before clicking "
            "'Find and Verify':\n"
            "\n"
            "  1. Unplug the USB-C cable from the gateway.\n"
            "  2. Move the BOOT switch on the gateway to the OFF position.\n"
            "  3. Connect the 5V/3A USB-C power supply to the gateway "
            "USB-C receptacle.\n"
            "  4. Click the 'Find and Verify' button below."
        )).pack(anchor="w", pady=(0, 4))

        # NOTE on one logical line that re-wraps as the window scales.
        note = ttk.Label(wrap, justify="left", text=(
            "NOTE: Because the first boot after programming the EMMC "
            "includes filesystem expansion and a reboot, ~ 2 minutes is "
            "required before the gateway will be ready. A built in delay "
            "timer handles this automatically."))
        note.pack(anchor="w", fill="x", pady=(0, 8))
        note.bind("<Configure>",
                  lambda e: note.configure(wraplength=e.width - 4))

        ctrl = ttk.Frame(wrap)
        ctrl.pack(fill="x", pady=4)
        self.verify_btn = ttk.Button(ctrl, text="Find and Verify",
                                     command=self._start_verify_thread)
        self.verify_btn.pack(side="left", padx=4, ipadx=20, ipady=6)
        ttk.Button(ctrl, text="Enter MAC Manually",
                   command=self._manual_verify_entry).pack(
            side="right", padx=4, ipadx=10, ipady=6)
        ttk.Label(ctrl, text="Found at:").pack(side="left", padx=(20, 4))
        ttk.Entry(ctrl, textvariable=self.found_ip, width=18,
                  state="readonly").pack(side="left")
        ttk.Label(ctrl, textvariable=self.found_host,
                  foreground="#666").pack(side="left", padx=8)

        statusf = ttk.LabelFrame(wrap, text="Status")
        statusf.pack(fill="both", expand=True, pady=(6, 0))

        self.verify_status = ttk.Label(statusf, text="",
                                        font=("DejaVu Sans", 14, "bold"))
        self.verify_status.pack(anchor="w", padx=6, pady=6)

        self.verify_results = scrolledtext.ScrolledText(
            statusf, height=18, wrap="word", font=("DejaVu Sans Mono", 10))
        self.verify_results.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        ttk.Label(statusf,
                  text=f"Full transcript saved to: {VERIFY_LOG}",
                  foreground="#888").pack(anchor="w", padx=6, pady=(0, 6))

    def _build_install_tab(self, parent):
        wrap = ttk.Frame(parent, padding=12)
        wrap.pack(fill="both", expand=True)

        # Intro sentence on one logical line that re-wraps as the window
        # scales. Tk Labels only wrap when wraplength is set, so bind it to
        # the frame width on resize.
        intro = ttk.Label(wrap, justify="left", text=(
            "Installs the Care Bloom application onto the gateway that has "
            "already been programmed and verified. The following steps are "
            "automated:"))
        intro.pack(anchor="w", fill="x")
        intro.bind("<Configure>",
                   lambda e: intro.configure(wraplength=e.width - 4))

        ttk.Label(wrap, justify="left", text=(
            "\n"
            "  1. The application is loaded into the /tmp folder on the "
            "gateway.\n"
            "  2. The application is extracted.\n"
            "  3. The dos2unix utility is installed on the gateway.\n"
            "  4. dos2unix and chmod +x are applied to the application bin "
            "and etc folders\n"
            "      prior to installation.\n"
            "  5. The application installer: setupSystemLocal.sh is ran."
        )).pack(anchor="w", pady=(0, 8))

        # Target host row
        hostf = ttk.Frame(wrap)
        hostf.pack(fill="x", pady=4)
        ttk.Label(hostf, text="Target host:").pack(side="left")
        ttk.Entry(hostf, textvariable=self.install_host, width=30).pack(
            side="left", padx=6)
        ttk.Button(hostf, text="Use verified board",
                   command=self._install_use_verified).pack(side="left", padx=4)
        ttk.Label(hostf, text="(hostname or IP; SSH user/password come "
                              "from the Configure tab)",
                  foreground="#888").pack(side="left", padx=6)

        # Steps
        steps_frame = ttk.LabelFrame(wrap, text="Steps")
        steps_frame.pack(fill="x", pady=8)
        step_defs = [
            ("Connect to gateway over SSH",   "Uses the configured user / password."),
            ("Transfer app archive",      "SCP the archive to /tmp on the gateway."),
            ("Extract the app",           "Unpack into the home directory."),
            ("Install dos2unix",          "apt install dos2unix."),
            ("Fix line endings + perms",  "dos2unix + chmod +x on bin/ and etc/."),
            ("Run setupSystemLocal.sh",   "The Carebloom system setup script."),
        ]
        self.install_steps = []
        for label, sub in step_defs:
            row = ttk.Frame(steps_frame)
            row.pack(fill="x", padx=8, pady=2)
            icon = ttk.Label(row, text="○", width=2, font=("DejaVu Sans Mono", 14))
            icon.pack(side="left")
            ttk.Label(row, text=label,
                      font=("DejaVu Sans", 12, "bold")).pack(side="left", padx=4)
            ttk.Label(row, text="— " + sub,
                      foreground="#777").pack(side="left", padx=4)
            self.install_steps.append({"icon": icon, "status": "pending"})

        ctrl = ttk.Frame(wrap)
        ctrl.pack(fill="x", pady=8)
        self.install_btn = ttk.Button(ctrl, text="Install Application",
                                      command=self._start_install_thread)
        self.install_btn.pack(side="left", padx=4, ipadx=20, ipady=6)

        statusf = ttk.LabelFrame(wrap, text="Status")
        statusf.pack(fill="both", expand=True, pady=(8, 0))

        self.install_status = ttk.Label(statusf, text="Ready.",
                                         font=("DejaVu Sans", 13))
        self.install_status.pack(anchor="w", padx=6, pady=6)

        self.install_results = scrolledtext.ScrolledText(
            statusf, height=14, wrap="word", font=("DejaVu Sans Mono", 10))
        self.install_results.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        # Where the full install transcript is written, so it can be shared.
        ttk.Label(statusf,
                  text=f"Full transcript saved to: {INSTALL_LOG}",
                  foreground="#888").pack(anchor="w", padx=6, pady=(0, 6))

    # ---- Label generation tab ---------------------------------------------
    def _build_label_tab(self, parent):
        wrap = ttk.Frame(parent, padding=12)
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, justify="left", text=(
            "Prints QR-code labels for the GW-2000 gateway on the Zebra "
            "ZD410 thermal printer.\n"
            "Labels match the AN-2000 anchor format: a QR code on the left "
            "and three text lines\n"
            "on the right. The QR code encodes the gateway's eth0 MAC address."
        )).pack(anchor="w", pady=(0, 8))

        # MAC entry row
        macf = ttk.Frame(wrap)
        macf.pack(fill="x", pady=4)
        ttk.Label(macf, text="Gateway Ethernet MAC:").pack(side="left")
        ttk.Entry(macf, textvariable=self.label_mac, width=24).pack(
            side="left", padx=6)
        ttk.Button(macf, text="Use verified board's MAC",
                   command=self._label_use_verified_mac).pack(
            side="left", padx=4)
        ttk.Button(macf, text="Read MAC over SSH…",
                   command=self._label_read_mac_ssh).pack(side="left", padx=4)
        ttk.Label(macf, text="(any format: aa:bb:cc:dd:ee:ff or aabbccddeeff)",
                  foreground="#888").pack(side="left", padx=6)

        # LOT + copies row
        lotf = ttk.Frame(wrap)
        lotf.pack(fill="x", pady=4)
        ttk.Label(lotf, text="LOT:").pack(side="left")
        ttk.Entry(lotf, textvariable=self.label_lot, width=16).pack(
            side="left", padx=6)
        ttk.Label(lotf, text="   Copies:").pack(side="left")
        ttk.Spinbox(lotf, from_=1, to=20, width=4,
                    textvariable=self.label_copies).pack(side="left", padx=6)
        ttk.Label(lotf, text=f"   Part number: {LABEL_PRODUCT_PN}",
                  foreground="#555",
                  font=("DejaVu Sans", 11, "bold")).pack(side="left", padx=12)

        # Live preview
        prevf = ttk.LabelFrame(wrap, text="Label preview (1.0\" x 0.5\")")
        prevf.pack(fill="x", pady=8)
        inner = ttk.Frame(prevf, padding=10)
        inner.pack()
        self.label_canvas = tk.Canvas(
            inner, width=LABEL_PREVIEW_W, height=LABEL_PREVIEW_H,
            background="white", highlightthickness=1,
            highlightbackground="#000")
        self.label_canvas.pack()
        ttk.Label(prevf,
                  text="Preview updates as you type. The QR encodes the "
                       "bare MAC (no colons).",
                  foreground="#888").pack(anchor="w", padx=8, pady=(0, 6))

        # Controls
        ctrl = ttk.Frame(wrap)
        ctrl.pack(fill="x", pady=8)
        self.print_btn = ttk.Button(
            ctrl, text="Print Labels", width=17,
            command=self._start_print_thread)
        self.print_btn.pack(side="left", padx=4, ipadx=20, ipady=6)
        ttk.Button(ctrl, text="Calibrate Printer", width=17,
                   command=self._start_calibrate_thread).pack(
            side="right", padx=4, ipadx=20, ipady=6)
        ttk.Label(ctrl,
                  text=f"Printer: {PRINTER_DEVICE}",
                  foreground="#888", anchor="center").pack(
            side="left", fill="x", expand=True, padx=6)

        # Status
        statusf = ttk.LabelFrame(wrap, text="Status")
        statusf.pack(fill="both", expand=True, pady=(8, 0))
        self.label_status = ttk.Label(statusf, text="Ready.",
                                       font=("DejaVu Sans", 13))
        self.label_status.pack(anchor="w", padx=6, pady=6)
        self.label_results = scrolledtext.ScrolledText(
            statusf, height=8, wrap="word", font=("DejaVu Sans Mono", 10))
        self.label_results.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        # Redraw the preview whenever any field changes.
        for var in (self.label_mac, self.label_lot):
            var.trace_add("write", lambda *_: self._refresh_label_preview())

        if qrcode is None:
            self._lresult("WARNING: the 'qrcode' package is not installed, so "
                          "the on-screen preview cannot draw the QR code.")
            self._lresult("Printing still works (the ZD410 generates the QR "
                          "itself from the ZPL ^BQ command).")
            self._lresult("To enable the preview: sudo apt install "
                          "python3-qrcode")

        self._refresh_label_preview()

    def _lresult(self, text):
        """Append a line to the Label tab's status pane."""
        def append():
            self.label_results.insert("end", text + "\n")
            self.label_results.see("end")
        self.after(0, append)

    def _label_use_verified_mac(self):
        """Populate the MAC field from the board found on the Verify tab."""
        mac = getattr(self, "found_mac", "") or ""
        if not mac:
            messagebox.showinfo(
                "No verified board",
                "No board MAC is known yet. Run the Verify tab first, or "
                "type / read the MAC manually.")
            return
        self.label_mac.set(mac)
        self._lresult(f"MAC set from verified board: {mac}")

    def _label_read_mac_ssh(self):
        """Read eth0's MAC live from a board over SSH."""
        if paramiko is None:
            messagebox.showerror(
                "paramiko missing",
                "paramiko is not installed. Run:\n"
                "  sudo apt install python3-paramiko")
            return
        # Default the host to whatever Verify / App Installation last used.
        default_host = (self.found_host.get().strip()
                        or self.found_ip.get().strip()
                        or self.install_host.get().strip())
        host = simpledialog.askstring(
            "Read MAC over SSH",
            "Hostname or IP of the GW2000 to read eth0's MAC from:",
            initialvalue=default_host, parent=self)
        if not host:
            return
        self._lresult(f"Connecting to {host} to read eth0 MAC…")
        self.print_btn.configure(state="disabled")

        def worker():
            user = self.expected_user or self.username.get()
            pw = self.expected_pw or self.password.get()
            mac = None
            err = None
            client = None
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(hostname=host, username=user, password=pw,
                               timeout=12, allow_agent=False,
                               look_for_keys=False)
                stdin, stdout, stderr = client.exec_command(
                    "cat /sys/class/net/eth0/address", timeout=10)
                out = stdout.read().decode(errors="replace").strip()
                if out:
                    mac = out
                else:
                    err = "eth0 has no address (interface down?)."
            except Exception as e:
                err = str(e)
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

            def finish():
                self.print_btn.configure(state="normal")
                if mac:
                    try:
                        self.label_mac.set(format_mac_colons(mac))
                        self._lresult(f"Read eth0 MAC: {mac}")
                    except ValueError as e:
                        self._lresult(f"Got an unexpected value: {e}")
                else:
                    self._lresult(f"Could not read MAC: {err}")
            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_label_preview(self):
        """Redraw the on-screen label preview from the current field values.
        This mirrors what build_label_zpl() sends to the printer: QR on the
        left, three text lines on the right."""
        c = self.label_canvas
        c.delete("all")
        W, H = LABEL_PREVIEW_W, LABEL_PREVIEW_H

        raw_mac = self.label_mac.get()
        try:
            mac_clean = normalize_mac(raw_mac)
        except ValueError:
            mac_clean = None

        margin = 8
        qr_box = H - 2 * margin   # square QR area on the left

        if mac_clean is None:
            # Placeholder QR area + guidance text.
            c.create_rectangle(margin, margin, margin + qr_box,
                               margin + qr_box, outline="#bbb", dash=(3, 3))
            c.create_text(margin + qr_box / 2, margin + qr_box / 2,
                          text="QR", fill="#bbb",
                          font=("DejaVu Sans", 14, "bold"))
            c.create_text(margin + qr_box + 12, H / 2, anchor="w",
                          text="Enter a 12-digit MAC\nto preview the label.",
                          fill="#999", font=("DejaVu Sans", 10))
            self.label_status.configure(
                text="Enter the gateway Ethernet MAC to enable printing.",
                foreground="#555")
            self.print_btn.configure(state="disabled")
            return

        # ---- Draw the QR code preview ----
        matrix = qr_matrix(mac_clean)
        if matrix:
            n = len(matrix)
            module = qr_box / n
            for row in range(n):
                for col in range(n):
                    if matrix[row][col]:
                        x0 = margin + col * module
                        y0 = margin + row * module
                        c.create_rectangle(
                            x0, y0, x0 + module, y0 + module,
                            fill="black", outline="black")
        else:
            # qrcode not installed — show a stand-in box.
            c.create_rectangle(margin, margin, margin + qr_box,
                               margin + qr_box, outline="#888")
            c.create_text(margin + qr_box / 2, margin + qr_box / 2,
                          text="QR\n(preview\nunavailable)", fill="#888",
                          justify="center", font=("DejaVu Sans", 9))

        # ---- Draw the three text lines on the right ----
        text_x = margin + qr_box + 12
        lines = [
            f"PN:{LABEL_PRODUCT_PN}",
            f"LOT:{self.label_lot.get().strip()}",
            mac_clean,
        ]
        n_lines = len(lines)
        line_h = (H - 2 * margin) / n_lines
        font = ("DejaVu Sans Mono", 11, "bold")
        for i, ln in enumerate(lines):
            cy = margin + line_h * (i + 0.5)
            c.create_text(text_x, cy, anchor="w", text=ln,
                          fill="black", font=font)

        self.label_status.configure(
            text=f"Ready to print {self.label_copies.get()} label(s) "
                 f"for {format_mac_colons(mac_clean)}.",
            foreground="#080")
        self.print_btn.configure(state="normal")

    def _label_copies_value(self):
        """Parse the copies spinbox; default to LABELS_PER_PRINT on bad input."""
        try:
            n = int(str(self.label_copies.get()).strip())
            return max(1, min(20, n))
        except (ValueError, TypeError):
            return LABELS_PER_PRINT

    def _start_print_thread(self):
        raw_mac = self.label_mac.get()
        try:
            mac_clean = normalize_mac(raw_mac)
        except ValueError as e:
            messagebox.showerror("Invalid MAC", str(e))
            return

        copies = self._label_copies_value()
        lot = self.label_lot.get().strip()

        self.print_btn.configure(state="disabled")
        self.label_status.configure(text="Printing…", foreground="#0a7")
        self._lresult(f"Sending {copies} label(s) to the printer "
                      f"(PN={LABEL_PRODUCT_PN}, LOT={lot or '<blank>'}, "
                      f"MAC={mac_clean})…")

        def worker():
            ok = True
            try:
                print_label(mac_clean, pn=LABEL_PRODUCT_PN, lot=lot,
                            copies=copies)
                self._lresult("Label(s) sent to printer.")
            except LabelPrinterError as e:
                ok = False
                self._lresult(f"PRINT ERROR: {e}")
            except Exception as e:
                ok = False
                self._lresult(f"PRINT ERROR (unexpected): {e}")

            def finish():
                self.print_btn.configure(state="normal")
                if ok:
                    self.label_status.configure(
                        text="✓ Label(s) sent to printer.",
                        foreground="#080")
                else:
                    self.label_status.configure(
                        text="✗ Print failed — see status above.",
                        foreground="#c00")
            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _start_calibrate_thread(self):
        self.print_btn.configure(state="disabled")
        self._lresult("Running printer media calibration…")

        def worker():
            ok = True
            try:
                calibrate_printer()
                self._lresult("Calibration command sent. The printer will "
                              "feed a few labels.")
            except LabelPrinterError as e:
                ok = False
                self._lresult(f"CALIBRATION ERROR: {e}")
            except Exception as e:
                ok = False
                self._lresult(f"CALIBRATION ERROR (unexpected): {e}")

            def finish():
                self.print_btn.configure(state="normal")
                self.label_status.configure(
                    text=("✓ Calibration sent." if ok
                          else "✗ Calibration failed — see status above."),
                    foreground=("#080" if ok else "#c00"))
            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _install_use_verified(self):
        """Populate the target host from the most recent verified board."""
        ip = self.found_ip.get().strip()
        host = self.found_host.get().strip()
        target = host or ip
        if target:
            self.install_host.set(target)
            self._iresult(f"Target set to: {target}")
        else:
            messagebox.showinfo(
                "No verified board",
                "Run the Verify tab first, or type a hostname/IP manually.")

    def _set_install_step(self, idx, status):
        glyph = {"pending": ("○", "#888"),
                 "running": ("●", "#0a7"),
                 "ok":      ("✓", "#080"),
                 "fail":    ("✗", "#c00")}[status]
        self.install_steps[idx]["status"] = status
        self.install_steps[idx]["icon"].configure(
            text=glyph[0], foreground=glyph[1])

    def _reset_install_steps(self):
        for i in range(len(self.install_steps)):
            self._set_install_step(i, "pending")
        self.install_status.configure(text="Ready.", foreground="#000")

    def _write_transcript(self, path, s):
        """Append one line to a transcript file. Failures here are silent -
        transcript logging must never break the actual operation."""
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(s + "\n")
        except Exception:
            pass

    def _start_transcript(self, path, title):
        """Truncate a transcript file and write a header. Called at the start
        of each program / verify / install run."""
        try:
            os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"=== {title} ===\n")
                f.write(f"Started: {datetime.now().isoformat(timespec='seconds')}\n\n")
        except Exception:
            pass

    def _iresult(self, s):
        self._write_transcript(INSTALL_LOG, s)
        def upd():
            self.install_results.insert("end", s + "\n")
            self.install_results.see("end")
        self.after(0, upd)


    def log(self, s):
        self._write_transcript(PROGRAM_LOG, s)
        self._log_q.put(s)

    def _drain_log(self):
        try:
            while True:
                s = self._log_q.get_nowait()
                self.program_results.configure(state="normal")
                self.program_results.insert("end", s + "\n")
                self.program_results.see("end")
                self.program_results.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(80, self._drain_log)

    def _set_step(self, idx, status):
        glyph = {"pending": ("○", "#888"),
                 "running": ("●", "#0a7"),
                 "ok":      ("✓", "#080"),
                 "fail":    ("✗", "#c00")}[status]
        self.steps[idx]["status"] = status
        self.steps[idx]["icon"].configure(text=glyph[0], foreground=glyph[1])

    def _reset_steps(self):
        """Reset the Program tab AND clear all per-board state on the Verify
        and App Installation tabs. The Reset button starts a fresh gateway,
        so stale PASS/FAIL results from the previous board must not linger."""
        for i in range(len(self.steps)):
            self._set_step(i, "pending")
        self.program_status.configure(text="Ready.", foreground="#000")

        # --- Clear Verify tab ---
        try:
            self.verify_results.delete("1.0", "end")
        except Exception:
            pass
        self.verify_status.configure(text="", foreground="#000")
        self.found_ip.set("")
        self.found_host.set("")
        self.found_mac = ""

        # --- Clear App Installation tab ---
        for i in range(len(self.install_steps)):
            self._set_install_step(i, "pending")
        try:
            self.install_results.delete("1.0", "end")
        except Exception:
            pass
        self.install_status.configure(text="Ready.", foreground="#000")

    # ---- Config persistence -----------------------------------------------
    def _save_defaults(self):
        data = {
            "rpiboot_path": self.rpiboot_path.get(),
            "bootfiles_dir": self.bootfiles_dir.get(),
            "image_path": self.image_path.get(),
            "username": self.username.get(),
            "hostname": self.hostname.get(),
            "wifi_ssid": self.wifi_ssid.get(),
            "wifi_country": self.wifi_country.get(),
            "app_zip_path": self.app_zip_path.get(),
            "app_name": self.app_name.get(),
            # passwords intentionally NOT saved
        }
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(data, f, indent=2)
            self.log(f"Saved defaults to {CONFIG_FILE}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _load_defaults(self, silent=False):
        try:
            with open(CONFIG_FILE) as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            if not silent:
                messagebox.showerror("Load failed", str(e))
            return
        for k, v in d.items():
            if hasattr(self, k) and isinstance(getattr(self, k), tk.StringVar):
                getattr(self, k).set(v)
        if not silent:
            self.log(f"Loaded defaults from {CONFIG_FILE}")

    # ---- Program workflow ---------------------------------------------------
    def _start_program_thread(self):
        problems = []
        if not (self.rpiboot_path.get() and os.path.isfile(self.rpiboot_path.get())):
            problems.append("rpiboot binary not found")
        if not (self.bootfiles_dir.get() and os.path.isdir(self.bootfiles_dir.get())):
            problems.append("Boot-files directory not found")
        if not (self.image_path.get() and os.path.isfile(self.image_path.get())):
            problems.append("OS image not found")
        if not self.username.get().strip():
            problems.append("Username is empty")
        if len(self.password.get()) < 4:
            problems.append("Password must be at least 4 characters")
        for tool in ("openssl", "xz", "dd", "sudo"):
            if not which(tool):
                problems.append(f"Missing tool: {tool}")
        if problems:
            messagebox.showerror("Fix configuration", "\n".join(problems))
            return

        self._reset_steps()
        self._start_transcript(PROGRAM_LOG, "GW2000 Program")
        self.program_results.configure(state="normal")
        self.program_results.delete("1.0", "end")
        self.program_results.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.program_status.configure(text="Running…", foreground="#0a7")

        # Unique token for THIS program run. It is written onto the board's
        # boot partition and copied by firstrun.sh to /etc/gw2k_program_id on
        # the booted system. Verify reads it back over SSH to identify the
        # board just programmed - this works even when re-programming a board
        # that was already on the LAN (the hostname/MAC are unchanged on a
        # re-program, so the old "new arrival" snapshot diff could not).
        self.program_id = secrets.token_hex(8)

        threading.Thread(target=self._program_workflow, daemon=True).start()

    def _program_workflow(self):
        ok = False
        try:
            ok = self._do_program()
        except Exception as e:
            self.log(f"EXCEPTION: {e}")
        finally:
            def finish():
                self.start_btn.configure(state="normal")
                if ok:
                    # Snapshot when programming finished. Verify uses this to
                    # hold off discovery until the board has had time to boot.
                    self.program_finished_at = time.time()
                    self.program_status.configure(
                        text="✓ Program complete. Set BOOT switch to OFF, "
                             "connect Ethernet, power-cycle, then go to Verify.",
                        foreground="#080")
                    self.notebook.select(self.verify_tab)
                else:
                    self.program_status.configure(
                        text="✗ Program failed — see log.",
                        foreground="#c00")
            self.after(0, finish)

    def _do_program(self):
        # Snapshot which CareBloom gateways are already on the LAN before we
        # program this board. Verify will diff against this to identify the
        # board just programmed (the new arrival on the network).
        #
        # The snapshot is a synchronous avahi-browse pass that takes a couple
        # of seconds. rpiboot (Step 1) does not depend on it, so we run the
        # snapshot in a background thread and let it overlap rpiboot instead
        # of adding its delay before programming visibly begins. The thread
        # is joined right after rpiboot, long before Verify ever reads
        # self.lan_snapshot.
        self.lan_snapshot = None
        snapshot_done = threading.Event()

        def snapshot_worker():
            try:
                template = self.hostname.get() or DEFAULT_HOSTNAME
                self.lan_snapshot = self._snapshot_lan_boards(template)
            except Exception as e:
                self.lan_snapshot = None
                self._snapshot_error = e
            finally:
                snapshot_done.set()

        self._snapshot_error = None
        snapshot_thread = threading.Thread(target=snapshot_worker, daemon=True)
        snapshot_thread.start()

        # 1) rpiboot  (runs concurrently with the LAN snapshot above)
        self._set_step(0, "running")
        self.log("=== Step 1: rpiboot ===")
        rc, _ = run_stream(
            ["sudo", self.rpiboot_path.get(), "-d", self.bootfiles_dir.get()],
            self.log, timeout=180)
        if rc != 0:
            self.log("rpiboot failed. Check: BOOT switch ON, and the "
                     "USB-C cable plugged into this Pi AFTER rpiboot started "
                     "(the cable powers the board).")
            self._set_step(0, "fail")
            return False
        self._set_step(0, "ok")

        # The snapshot has almost certainly finished during rpiboot; wait for
        # it (briefly) so self.lan_snapshot is settled before the workflow
        # continues. Cap the wait so a hung avahi-browse can't stall the run.
        if not snapshot_done.wait(timeout=20):
            self.log("(LAN snapshot still running after rpiboot — "
                     "continuing without it.)")
            self.lan_snapshot = None
        elif self._snapshot_error is not None:
            self.log(f"(LAN snapshot skipped: {self._snapshot_error})")
        else:
            self.log(f"LAN snapshot: {len(self.lan_snapshot)} CareBloom "
                     f"gateway(s) already on the network before programming.")

        # 2) find disk
        self._set_step(1, "running")
        self.log("=== Step 2: identify eMMC ===")
        node, size, name = None, 0, ""
        for _ in range(15):
            found = find_cm4_disk()
            if found:
                node, size, name = found
                self.log(f"Found: {node} — {_human(size)} — {name}")
                break
            time.sleep(1)
        if not node:
            self.log("No USB disk in the 1–64 GB range appeared. Re-seat USB-C.")
            self._set_step(1, "fail")
            return False
        self._set_step(1, "ok")
        self.node = node

        # 3) unmount partitions
        self._set_step(2, "running")
        unmount_all_partitions(node, self.log)
        self._set_step(2, "ok")

        # 4) program
        self._set_step(3, "running")
        img = self.image_path.get()
        low = img.lower()
        # Use bs=4M + status=progress (GNU dd). Operator sees throughput in the log.
        if low.endswith((".img.xz", ".xz")):
            pipeline = (f"xz -dc {shlex.quote(img)} | "
                        f"sudo dd of={shlex.quote(node)} bs=4M conv=fsync status=progress")
        elif low.endswith(".gz"):
            pipeline = (f"gunzip -c {shlex.quote(img)} | "
                        f"sudo dd of={shlex.quote(node)} bs=4M conv=fsync status=progress")
        elif low.endswith(".zip"):
            pipeline = (f"unzip -p {shlex.quote(img)} | "
                        f"sudo dd of={shlex.quote(node)} bs=4M conv=fsync status=progress")
        else:
            pipeline = (f"sudo dd if={shlex.quote(img)} of={shlex.quote(node)} "
                        f"bs=4M conv=fsync status=progress")
        self.log(f"=== Step 4: program {os.path.basename(img)} → {node} ===")
        rc, _ = run_stream(pipeline, self.log, shell=True)
        if rc != 0:
            self.log("dd failed.")
            self._set_step(3, "fail")
            return False
        run_stream(["sync"], self.log)
        self._set_step(3, "ok")

        # 5) re-attach so bootfs is mounted
        self._set_step(4, "running")
        self.log("=== Step 5: re-attach for config write ===")
        # After dd the kernel re-reads the partition table; bootfs may auto-mount.
        # If not, run rpiboot again to re-expose the eMMC.
        time.sleep(3)
        bootfs = find_bootfs_mount(node, deadline=time.time() + 15)
        if not bootfs:
            rc, _ = run_stream(
                ["sudo", self.rpiboot_path.get(),
                 "-d", self.bootfiles_dir.get()],
                self.log, timeout=120)
            # rpiboot may give us a NEW node — find it again.
            new_node = None
            for _ in range(15):
                f = find_cm4_disk()
                if f:
                    new_node = f[0]
                    break
                time.sleep(1)
            if new_node:
                node = new_node
                self.node = new_node
            bootfs = find_bootfs_mount(node, deadline=time.time() + 20)
        if not bootfs:
            self.log("bootfs never mounted.")
            self._set_step(4, "fail")
            return False
        self.log(f"bootfs mounted at: {bootfs}")
        self._set_step(4, "ok")

        # 6) write firstrun.sh + cmdline.txt
        self._set_step(5, "running")
        if not self._write_firstboot(bootfs):
            self._set_step(5, "fail")
            return False
        self._set_step(5, "ok")

        # 7) sync + eject
        self._set_step(6, "running")
        run_stream(["sync"], self.log)
        # Unmount cleanly via udisks if mounted
        try:
            subprocess.run(["udisksctl", "unmount", "-b",
                            self._bootfs_block(node, bootfs)],
                           check=False, timeout=15)
        except Exception:
            pass
        # Power off the USB device so operator can yank it
        run_stream(["sudo", "eject", node], self.log)
        self._set_step(6, "ok")
        return True

    def _bootfs_block(self, node, mountpoint):
        """Given a disk path /dev/sdX, find the partition device for bootfs."""
        name = os.path.basename(node)
        for d in lsblk_json():
            if d.get("name") != name:
                continue
            for child in d.get("children", []) or []:
                if (child.get("mountpoint") == mountpoint
                        or (child.get("label") or "").lower() == "bootfs"):
                    return f"/dev/{child.get('name')}"
        return node + "1"

    def _write_firstboot(self, bootfs):
        user = self.username.get().strip()
        pw = self.password.get()
        host_template = self.hostname.get().strip()
        ssid = self.wifi_ssid.get().strip()
        psk = self.wifi_psk.get()
        country = self.wifi_country.get().strip() or "US"

        try:
            proc = subprocess.run(
                ["openssl", "passwd", "-6", pw],
                capture_output=True, text=True, check=True, timeout=10)
            pw_hash = proc.stdout.strip()
        except Exception as e:
            self.log(f"openssl passwd failed: {e}")
            return False

        # Hostname is derived on the target itself at first boot, so it
        # reflects the actual Ethernet MAC of the CM4 we're programming.
        # Template tokens supported in the hostname field:
        #   {MAC}     — eth0 MAC, lowercase, no colons (e.g. b827ebabc123)
        #   {MAC6}    — last 6 hex chars of eth0 MAC (e.g. abc123)
        #   {MACUPPER}— full MAC, uppercase, no colons
        # Default template if user left it blank or set 'auto':
        if not host_template or host_template.endswith("auto"):
            host_template = "CareBloom{MAC}"
        # If the operator typed plain text, leave it alone (no MAC injection).

        firstrun = [
            "#!/bin/bash",
            "set +e",
            "exec > /var/log/firstrun.log 2>&1",
            'echo "firstrun.sh starting at $(date)"',
            "",
            "# Derive hostname from eth0 MAC address.",
            "# Wait briefly for the kernel to bring eth0 up (interface may not be ready yet).",
            "MAC=''",
            "for i in 1 2 3 4 5 6 7 8 9 10; do",
            "    if [ -r /sys/class/net/eth0/address ]; then",
            "        MAC=$(cat /sys/class/net/eth0/address | tr -d ':' | tr '[:upper:]' '[:lower:]')",
            "        break",
            "    fi",
            "    sleep 1",
            "done",
            "# Fall back: scan for any non-loopback interface with a MAC.",
            'if [ -z "$MAC" ]; then',
            "    for iface in /sys/class/net/*/address; do",
            "        ifname=$(basename $(dirname \"$iface\"))",
            '        [ "$ifname" = "lo" ] && continue',
            "        val=$(cat \"$iface\" 2>/dev/null | tr -d ':' | tr '[:upper:]' '[:lower:]')",
            '        if [ -n "$val" ] && [ "$val" != "000000000000" ]; then',
            '            MAC="$val"; break',
            "        fi",
            "    done",
            "fi",
            'if [ -z "$MAC" ]; then',
            '    MAC=$(printf "%012x" $RANDOM$RANDOM)',
            "    echo 'WARNING: no MAC found, using random fallback' >&2",
            "fi",
            "MAC6=${MAC: -6}",
            "MACUPPER=$(echo $MAC | tr '[:lower:]' '[:upper:]')",
            "",
            f"HOSTNAME_TEMPLATE={shlex.quote(host_template)}",
            'NEW_HOSTNAME=$(echo "$HOSTNAME_TEMPLATE" | sed -e "s/{MAC}/$MAC/g" -e "s/{MAC6}/$MAC6/g" -e "s/{MACUPPER}/$MACUPPER/g")',
            '# Sanitize: only a-z A-Z 0-9 - allowed; max 63 chars; cannot start/end with -',
            'NEW_HOSTNAME=$(echo "$NEW_HOSTNAME" | tr -cd "[:alnum:]-" | cut -c1-63 | sed "s/^-*//;s/-*$//")',
            'echo "Setting hostname to: $NEW_HOSTNAME"',
            'echo "$NEW_HOSTNAME" > /etc/hostname',
            'hostnamectl set-hostname "$NEW_HOSTNAME" 2>/dev/null || true',
            'sed -i "s/^127.0.1.1.*/127.0.1.1\\t$NEW_HOSTNAME/g" /etc/hosts',
            "",
            "# User",
            "FIRSTUSER=`getent passwd 1000 | cut -d: -f1`",
            'if [ -z "$FIRSTUSER" ]; then',
            f'    useradd --create-home --shell /bin/bash --uid 1000 {shlex.quote(user)}',
            f'    usermod -aG sudo,adm,dialout,cdrom,audio,video,plugdev,games,users,input,netdev,gpio,i2c,spi {shlex.quote(user)} 2>/dev/null',
            f'elif [ "$FIRSTUSER" != {shlex.quote(user)} ]; then',
            f'    usermod -l {shlex.quote(user)} "$FIRSTUSER"',
            f'    usermod -m -d /home/{user} {shlex.quote(user)}',
            f'    groupmod -n {shlex.quote(user)} "$FIRSTUSER" 2>/dev/null',
            "    if [ -f /etc/sudoers.d/010_pi-nopasswd ]; then",
            f'        mv /etc/sudoers.d/010_pi-nopasswd /etc/sudoers.d/010_{user}-nopasswd',
            f'        sed -i "s/^pi /{user} /" /etc/sudoers.d/010_{user}-nopasswd',
            "    fi",
            "fi",
            "# Set the password. The SHA-512 hash ($6$...) is read from a",
            "# separate file (cm4_pwhash) rather than embedded inline, because",
            "# a $6$... hash inside a shell string gets mangled by variable",
            "# expansion. 'chpasswd -e' takes 'user:hash' on stdin.",
            "PWHASH=''",
            "for HF in /boot/firmware/cm4_pwhash /boot/cm4_pwhash; do",
            '    if [ -r "$HF" ]; then',
            '        PWHASH=$(cat "$HF")',
            "        break",
            "    fi",
            "done",
            'if [ -n "$PWHASH" ]; then',
            f'    if printf "%s:%s\\n" {shlex.quote(user)} "$PWHASH" | chpasswd -e; then',
            f'        echo "password set OK for user {user}"',
            "    else",
            f'        echo "ERROR: chpasswd failed for user {user}" >&2',
            "    fi",
            "else",
            '    echo "ERROR: password hash file not found" >&2',
            "fi",
            "# Remove the hash file so the password hash is not left on the",
            "# boot partition after first boot.",
            "rm -f /boot/firmware/cm4_pwhash /boot/cm4_pwhash 2>/dev/null || true",
            "# Make sure the account is not locked.",
            f'passwd -u {shlex.quote(user)} 2>/dev/null || true',
            "",
            "# --- Enable SSH (Bookworm/Trixie) -------------------------------",
            "# SSH on recent Pi OS can be socket-activated. Enable BOTH the",
            "# service and the socket, and remove the 'disabled' marker. Don't",
            "# rely on 'systemctl start' here - the reboot at the end starts it",
            "# cleanly. Also unmask in case the image shipped it masked.",
            "systemctl unmask ssh.service 2>/dev/null || true",
            "systemctl unmask ssh.socket 2>/dev/null || true",
            "systemctl enable ssh.service 2>/dev/null || true",
            "systemctl enable ssh.socket 2>/dev/null || true",
            "# Some images key SSH off this file; create it AND remove any",
            "# 'ssh disabled' state raspi-config may have left.",
            "touch /boot/firmware/ssh 2>/dev/null || true",
            "rm -f /etc/ssh/sshd_not_to_be_run 2>/dev/null || true",
            "# Generate host keys now if the image didn't ship them.",
            "if [ ! -f /etc/ssh/ssh_host_rsa_key ]; then",
            "    ssh-keygen -A 2>/dev/null || dpkg-reconfigure openssh-server 2>/dev/null || true",
            "fi",
            "# raspi-config's own helper, as a belt-and-suspenders fallback.",
            "raspi-config nonint do_ssh 0 2>/dev/null || true",
            "rm -f /etc/ssh/sshd_config.d/rename_user.conf",
            "",
            "# --- mDNS workstation advertisement (Avahi) ---------------------",
            "# The GW2000 Programmer's Verify tab discovers gateways by",
            "# browsing the _workstation._tcp mDNS service. Current Debian /",
            "# Raspberry Pi OS ships avahi-daemon with publish-workstation=no",
            "# by default, so a freshly-imaged board does NOT advertise that",
            "# service and is invisible to discovery. Force it on here so",
            "# every gateway this flasher produces is reliably discoverable.",
            "AVAHI_CONF=/etc/avahi/avahi-daemon.conf",
            'if [ -f "$AVAHI_CONF" ]; then',
            "    if grep -qE '^[[:space:]]*#?[[:space:]]*publish-workstation' "
            '"$AVAHI_CONF"; then',
            "        # Key present (set to no, or commented out) - force to yes.",
            "        sed -i -E "
            "'s/^[[:space:]]*#?[[:space:]]*publish-workstation[[:space:]]*=.*/"
            "publish-workstation=yes/' "
            '"$AVAHI_CONF"',
            "    elif grep -qE '^\\[publish\\]' \"$AVAHI_CONF\"; then",
            "        # [publish] section exists but no key - add it there.",
            "        sed -i -E '/^\\[publish\\]/a publish-workstation=yes' "
            '"$AVAHI_CONF"',
            "    else",
            "        # No [publish] section at all - append one.",
            '        printf \'\\n[publish]\\npublish-workstation=yes\\n\' '
            '>> "$AVAHI_CONF"',
            "    fi",
            '    echo "Set publish-workstation=yes in $AVAHI_CONF"',
            "else",
            '    echo "WARNING: $AVAHI_CONF not found - is avahi-daemon '
            'installed?" >&2',
            "fi",
            "# Make sure the daemon is enabled so the advertisement actually",
            "# goes out after the reboot at the end of firstrun.sh.",
            "systemctl unmask avahi-daemon.service avahi-daemon.socket "
            "2>/dev/null || true",
            "systemctl enable avahi-daemon.service 2>/dev/null || true",
            "systemctl enable avahi-daemon.socket 2>/dev/null || true",
            "",
        ]
        if ssid:
            firstrun += [
                "# --- Wi-Fi access point (backhaul for CareBloom anchors) ----",
                "# The gateway broadcasts its own AP on wlan0. Room anchors",
                "# join this network to reach the gateway. The SSID is derived",
                "# from the eth0 MAC (same {MAC} substitution as the hostname)",
                "# so the AP name matches the hostname.",
                "#",
                "# IMPORTANT: firstrun.sh runs very early in first boot, BEFORE",
                "# the NetworkManager daemon is up - so 'nmcli' commands here",
                "# fail with 'NetworkManager is not running'. Instead we write",
                "# the connection profile directly as a keyfile into",
                "# /etc/NetworkManager/system-connections/. NetworkManager",
                "# loads every profile in that directory when it starts, so",
                "# the AP comes up cleanly on the reboot at the end of this",
                "# script - no daemon needs to be running right now.",
                "",
                "# Wi-Fi regulatory country - AP mode needs it set or the",
                "# radio may refuse to start the access point.",
                f"raspi-config nonint do_wifi_country {country} 2>/dev/null || true",
                f"iw reg set {country} 2>/dev/null || true",
                "rfkill unblock wifi 2>/dev/null || true",
                "",
                f"AP_SSID_TEMPLATE={shlex.quote(ssid)}",
                'AP_SSID=$(echo "$AP_SSID_TEMPLATE" | sed -e "s/{MAC}/$MAC/g" -e "s/{MAC6}/$MAC6/g" -e "s/{MACUPPER}/$MACUPPER/g")',
                f"AP_PSK={shlex.quote(psk)}",
                'echo "Configuring Wi-Fi AP with SSID: $AP_SSID"',
                "",
                "# Generate a stable UUID for the connection profile.",
                "AP_UUID=$(cat /proc/sys/kernel/random/uuid)",
                "",
                "# Write the NetworkManager keyfile for the AP. mode=ap makes",
                "# wlan0 a Wi-Fi AP; ipv4 method=shared brings up NM's built-in",
                "# DHCP server so joining anchors get addresses automatically.",
                "NM_DIR=/etc/NetworkManager/system-connections",
                "mkdir -p \"$NM_DIR\"",
                "# Remove any stale wlan0 profiles so the AP is the only one.",
                "rm -f \"$NM_DIR\"/*.nmconnection 2>/dev/null || true",
                'cat > "$NM_DIR/CareBloom-AP.nmconnection" <<EOF_AP',
                "[connection]",
                "id=CareBloom-AP",
                "uuid=$AP_UUID",
                "type=wifi",
                "interface-name=wlan0",
                "autoconnect=true",
                "",
                "[wifi]",
                "mode=ap",
                "band=bg",
                "ssid=$AP_SSID",
                "",
                "[wifi-security]",
                "key-mgmt=wpa-psk",
                "psk=$AP_PSK",
                "",
                "[ipv4]",
                "method=shared",
                "",
                "[ipv6]",
                "method=ignore",
                "EOF_AP",
                "# Keyfiles must be owner-only or NetworkManager refuses them.",
                'chmod 600 "$NM_DIR/CareBloom-AP.nmconnection"',
                'chown root:root "$NM_DIR/CareBloom-AP.nmconnection"',
                "# Make sure NetworkManager actually manages wlan0.",
                "systemctl enable NetworkManager 2>/dev/null || true",
                'echo "AP profile written to $NM_DIR/CareBloom-AP.nmconnection"',
                "",
            ]
        firstrun += [
            "# Disable Bluetooth - the CareBloom gateway uses Wi-Fi only.",
            "# Three layers: the disable-bt overlay stops the BT hardware from",
            "# initialising at all (this is what removes the bluetoothd LE-audio",
            "# / SAP 'Operation not permitted' boot-log noise); disabling the",
            "# hciuart and bluetooth services stops anything running before the",
            "# next reboot picks up the overlay.",
            "for CFG in /boot/firmware/config.txt /boot/config.txt; do",
            "    if [ -f \"$CFG\" ] && ! grep -q '^dtoverlay=disable-bt' \"$CFG\"; then",
            "        printf '\\n# CareBloom: gateway is Wi-Fi only, disable Bluetooth\\n' >> \"$CFG\"",
            "        echo 'dtoverlay=disable-bt' >> \"$CFG\"",
            "    fi",
            "done",
            "systemctl disable hciuart 2>/dev/null || true",
            "systemctl disable bluetooth.service 2>/dev/null || true",
            "systemctl stop bluetooth.service 2>/dev/null || true",
            "",
            "# --- Program-run identifier -------------------------------------",
            "# The flasher wrote a unique token for this program run onto the",
            "# boot partition. Copy it to /etc/gw2k_program_id so the Verify",
            "# tab can read it back over SSH and positively identify THIS",
            "# board - even on a re-program, where the hostname is unchanged.",
            "for PIDF in /boot/firmware/gw2k_program_id /boot/gw2k_program_id; do",
            '    if [ -r "$PIDF" ]; then',
            '        cp "$PIDF" /etc/gw2k_program_id',
            "        chmod 644 /etc/gw2k_program_id",
            '        echo "program id installed: $(cat /etc/gw2k_program_id)"',
            "        break",
            "    fi",
            "done",
            "# Remove the boot-partition copy so a later re-program's token",
            "# can't be confused with this one.",
            "rm -f /boot/firmware/gw2k_program_id /boot/gw2k_program_id "
            "2>/dev/null || true",
            "",
            "# Self-destruct",
            "rm -f /boot/firmware/firstrun.sh /boot/firstrun.sh",
            "sed -i 's| systemd.run.*||g' /boot/firmware/cmdline.txt 2>/dev/null || true",
            "sed -i 's| systemd.run.*||g' /boot/cmdline.txt 2>/dev/null || true",
            'echo "firstrun.sh complete at $(date)"',
            "exit 0",
            "",
        ]
        firstrun_text = "\n".join(firstrun)

        try:
            firstrun_path = os.path.join(bootfs, "firstrun.sh")
            tmp = "/tmp/firstrun.sh"
            with open(tmp, "w") as f:
                f.write(firstrun_text)
            run_stream(["sudo", "install", "-m", "0755", tmp, firstrun_path], self.log)
            os.unlink(tmp)

            run_stream(["sudo", "touch", os.path.join(bootfs, "ssh")], self.log)

            # Write the password hash to its own file (NOT interpolated into
            # firstrun.sh, because a $6$... hash gets mangled by shell
            # variable expansion). firstrun.sh reads this and deletes it.
            pwhash_path = os.path.join(bootfs, "cm4_pwhash")
            tmph = "/tmp/cm4_pwhash"
            with open(tmph, "w") as f:
                f.write(pw_hash + "\n")
            run_stream(["sudo", "install", "-m", "0600", tmph, pwhash_path], self.log)
            os.unlink(tmph)

            # Write this run's unique program-id token onto the boot
            # partition. firstrun.sh copies it to /etc/gw2k_program_id and
            # then deletes this boot-partition copy. Verify reads the token
            # back over SSH to identify the board just programmed.
            program_id = getattr(self, "program_id", None)
            if program_id:
                pid_path = os.path.join(bootfs, "gw2k_program_id")
                tmpp = "/tmp/gw2k_program_id"
                with open(tmpp, "w") as f:
                    f.write(program_id + "\n")
                run_stream(["sudo", "install", "-m", "0644", tmpp, pid_path],
                           self.log)
                os.unlink(tmpp)
                self.log(f"Wrote program-id token: {program_id}")
            else:
                self.log("(No program-id token set — Verify will fall back "
                         "to LAN-snapshot / operator pick.)")

            cmdline_path = os.path.join(bootfs, "cmdline.txt")
            with open(cmdline_path) as f:
                cmdline = f.read().strip()
            cmdline = re.sub(r"\s*systemd\.run\S*", "", cmdline)
            cmdline = re.sub(r"\s*systemd\.unit\S*", "", cmdline)
            cmdline = cmdline.strip() + (
                " systemd.run=/boot/firmware/firstrun.sh"
                " systemd.run_success_action=reboot"
                " systemd.unit=kernel-command-line.target")
            # cmdline.txt is on a FAT partition mounted user-writable usually,
            # but be defensive.
            tmpc = "/tmp/cmdline.txt"
            with open(tmpc, "w") as f:
                f.write(cmdline + "\n")
            run_stream(["sudo", "install", "-m", "0644", tmpc, cmdline_path], self.log)
            os.unlink(tmpc)

            self.log(f"Wrote firstrun.sh; hostname template: {host_template}")
            self.log("(Final hostname will be derived from the gateway's eth0 MAC at first boot.)")
            self.expected_hostname_template = host_template
            self.expected_hostname = None  # not known until target boots
            self.expected_user = user
            self.expected_pw = pw
            return True
        except Exception as e:
            self.log(f"Failed writing boot config: {e}")
            return False

    # ---- Verify workflow --------------------------------------------------
    def _start_verify_thread(self):
        self._start_transcript(VERIFY_LOG, "GW2000 Verify")
        self.verify_btn.configure(state="disabled")
        self.verify_results.configure(state="normal")
        self.verify_results.delete("1.0", "end")
        self.verify_status.configure(text="Searching for the board…",
                                      foreground="#0a7")
        threading.Thread(target=self._verify_workflow, daemon=True).start()

    def _result(self, s):
        self._write_transcript(VERIFY_LOG, s)
        def upd():
            self.verify_results.insert("end", s + "\n")
            self.verify_results.see("end")
        self.after(0, upd)

    def _set_verify_status(self, text, color="#000"):
        """Thread-safe update of the Verify tab's status line."""
        self.after(0, lambda: self.verify_status.configure(
            text=text, foreground=color))

    def _ask_pick_board(self, boards):
        """Show a modal dialog listing discovered gateways and let the
        operator pick the one they just programmed. Called from the verify
        worker thread - marshals the dialog onto the UI thread and blocks
        until the operator chooses. Returns the chosen (ip, hostname) tuple,
        or None if cancelled."""
        result = {}
        done = threading.Event()

        def show():
            dlg = tk.Toplevel(self)
            dlg.title("Select the gateway to verify")
            dlg.transient(self)
            dlg.grab_set()
            ttk.Label(dlg, padding=12, justify="left",
                      text=("More than one CareBloom gateway was found on "
                            "the network.\nSelect the one you just "
                            "programmed:")).pack(anchor="w")
            lb = tk.Listbox(dlg, height=min(10, len(boards)), width=46,
                            font=("DejaVu Sans Mono", 11))
            for ip, host in boards:
                lb.insert("end", f"{host}   ({ip})")
            lb.selection_set(0)
            lb.pack(padx=12, pady=4, fill="both", expand=True)

            def choose():
                sel = lb.curselection()
                if sel:
                    result["board"] = boards[sel[0]]
                done.set()
                dlg.destroy()

            def cancel():
                done.set()
                dlg.destroy()

            btns = ttk.Frame(dlg, padding=12)
            btns.pack(fill="x")
            ttk.Button(btns, text="Verify this gateway",
                       command=choose).pack(side="left", padx=4)
            ttk.Button(btns, text="Cancel",
                       command=cancel).pack(side="right", padx=4)
            lb.bind("<Double-Button-1>", lambda e: choose())
            dlg.bind("<Escape>", lambda e: cancel())
            dlg.protocol("WM_DELETE_WINDOW", cancel)

        self.after(0, show)
        done.wait()
        return result.get("board")

    def _ask_manual_target(self, reason=""):
        """Show a modal asking the operator to enter the gateway's IP
        address or hostname directly, bypassing network discovery. Used
        both as the fallback when discovery fails and from the 'Enter MAC
        Manually' button. Thread-safe: marshals onto the UI thread and
        blocks. Returns an (ip, hostname) tuple, or None if cancelled.

        The operator may type an IP, a hostname, or a MAC. A MAC (or bare
        12 hex digits) is turned into the CareBloom<MAC> hostname."""
        result = {}
        done = threading.Event()

        def show():
            dlg = tk.Toplevel(self)
            dlg.title("Enter gateway manually")
            dlg.transient(self)
            dlg.grab_set()
            msg = ("Enter the gateway's IP address, hostname, or Ethernet "
                   "MAC.\nUse this when the gateway can't be found "
                   "automatically\n(for example on a network that blocks "
                   "mDNS).")
            if reason:
                msg = reason + "\n\n" + msg
            ttk.Label(dlg, padding=12, justify="left",
                      text=msg).pack(anchor="w")
            row = ttk.Frame(dlg, padding=(12, 0))
            row.pack(fill="x")
            ttk.Label(row, text="IP / hostname / MAC:").pack(side="left")
            var = tk.StringVar()
            ent = ttk.Entry(row, textvariable=var, width=30)
            ent.pack(side="left", padx=6)
            ent.focus_set()
            hint = ttk.Label(
                dlg, padding=(12, 2), foreground="#888",
                text="e.g.  192.168.0.115   or   CareBloom88a29eceff84   "
                     "or   88:a2:9e:ce:ff:84")
            hint.pack(anchor="w")

            def ok():
                raw = var.get().strip()
                if not raw:
                    return
                ip, host = "", ""
                # Plain IPv4 address?
                if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", raw):
                    ip = raw
                else:
                    # MAC or 12 hex digits -> CareBloom<MAC> hostname.
                    hexonly = re.sub(r"[^0-9A-Fa-f]", "", raw)
                    if len(hexonly) == 12 and re.fullmatch(
                            r"[0-9A-Fa-f:.\-]+", raw):
                        host = "CareBloom" + hexonly.lower()
                    else:
                        # Treat as a hostname (strip any .local).
                        host = raw.split(".")[0]
                result["target"] = (ip, host)
                done.set()
                dlg.destroy()

            def cancel():
                done.set()
                dlg.destroy()

            btns = ttk.Frame(dlg, padding=12)
            btns.pack(fill="x")
            ttk.Button(btns, text="Verify this gateway",
                       command=ok).pack(side="left", padx=4)
            ttk.Button(btns, text="Cancel",
                       command=cancel).pack(side="right", padx=4)
            ent.bind("<Return>", lambda e: ok())
            dlg.bind("<Escape>", lambda e: cancel())
            dlg.protocol("WM_DELETE_WINDOW", cancel)

        self.after(0, show)
        done.wait()
        return result.get("target")

    def _manual_verify_entry(self):
        """Handler for the 'Enter MAC Manually' button. Runs on the UI
        thread, so it must NOT call the blocking _ask_manual_target directly
        (that would deadlock: _ask_manual_target marshals the dialog onto the
        UI thread and then waits, but the UI thread would be blocked here).
        Instead, do all of it - prompt and verify - in a worker thread."""

        def worker():
            target = self._ask_manual_target()
            if not target:
                return
            self._start_transcript(VERIFY_LOG, "GW2000 Verify (manual)")
            self.after(0, lambda: (
                self.verify_btn.configure(state="disabled"),
                self.verify_results.configure(state="normal"),
                self.verify_results.delete("1.0", "end"),
                self.verify_status.configure(
                    text="Verifying entered gateway…", foreground="#0a7")))
            ok = False
            try:
                ok = self._do_verify(manual_target=target)
            except Exception as e:
                self._result(f"EXCEPTION: {e}")
            self._verify_finish(ok)

        threading.Thread(target=worker, daemon=True).start()

    def _verify_finish(self, ok):
        """Apply the PASS/FAIL outcome to the Verify tab. Thread-safe -
        marshals the UI update onto the UI thread. Shared by the Find and
        Verify path and the manual-entry path."""
        def finish():
            self.verify_btn.configure(state="normal")
            if ok:
                self.verify_status.configure(
                    text="✓ PASS — board is up and healthy.",
                    foreground="#080")
                # Autofill the downstream tabs from the verified board so
                # the operator doesn't have to press "Use verified board".
                target = (self.found_host.get().strip()
                          or self.found_ip.get().strip())
                if target:
                    self.install_host.set(target)
                mac = getattr(self, "found_mac", "") or ""
                if mac:
                    self.label_mac.set(mac)
            else:
                self.verify_status.configure(
                    text="✗ FAIL — see results above.",
                    foreground="#c00")
        self.after(0, finish)

    def _verify_workflow(self, manual_target=None):
        ok = False
        try:
            ok = self._do_verify(manual_target=manual_target)
        except Exception as e:
            self._result(f"EXCEPTION: {e}")
        finally:
            self._verify_finish(ok)

    def _read_program_id(self, ip, user, pw):
        """SSH into a board and read /etc/gw2k_program_id. Returns the token
        string, or None if it can't be read (board still booting, file not
        there yet, SSH not up, wrong credentials, etc.). Never raises -
        callers treat None as 'not this board / not ready'."""
        client = None
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(hostname=ip, username=user, password=pw,
                           timeout=8, allow_agent=False, look_for_keys=False)
            stdin, stdout, stderr = client.exec_command(
                "cat /etc/gw2k_program_id 2>/dev/null", timeout=10)
            token = stdout.read().decode(errors="replace").strip()
            return token or None
        except Exception:
            return None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _match_by_program_id(self, boards, user, pw):
        """Given discovered (ip, hostname) boards, SSH into each and look for
        the one whose /etc/gw2k_program_id matches this session's token.

        Returns:
          (ip, host)            - the board carrying this run's token
          None                  - token known, but no board has it yet
                                   (caller should keep waiting / retry)
        Only called when self.program_id is set.
        """
        want = self.program_id
        for ip, host in boards:
            tok = self._read_program_id(ip, user, pw)
            if tok is None:
                self._result(f"  {host} ({ip}): no program-id readable yet.")
                continue
            if tok == want:
                self._result(f"  {host} ({ip}): program-id MATCH.")
                return ip, host
            self._result(f"  {host} ({ip}): program-id is from a different "
                          f"run — not this board.")
        return None

    def _select_verify_target(self, boards):
        """Fallback target selector, used when program-id matching did not
        identify the board (no token this session, or the token could not be
        read - e.g. the board is still booting). Given the discovered
        (ip, hostname) gateways, decide which one to verify.

        Returns (ip, host), or (None, None) if no board could be chosen
        (caller then offers manual entry).

        Strategy:
          1. LAN-snapshot diff: if exactly one board is new since
             programming, use it.
          2. If exactly one board exists at all, use it.
          3. Otherwise (Option C): ask the operator to pick from the list.
        """
        if not boards:
            self._result("Could not find any gateway within the time limit.")
            self._result("Check: BOOT switch OFF, Ethernet connected, "
                          "5V/3A USB-C supply powering the board.")
            return None, None

        snap = self.lan_snapshot
        new_boards = None
        if snap is not None:
            new_boards = [b for b in boards if b[1].lower() not in snap]

        if new_boards is not None and len(new_boards) == 1:
            ip, host = new_boards[0]
            self._result(f"Identified the newly-programmed gateway by LAN "
                          f"snapshot: {host} at {ip}\n")
            return ip, host
        if len(boards) == 1:
            ip, host = boards[0]
            self._result(f"One gateway found: {host} at {ip}\n")
            return ip, host

        # Option C: more than one candidate and no automatic way to tell them
        # apart - ask the operator which board they just programmed. This is
        # the safety net for the re-program case (the board's hostname is
        # unchanged, so the snapshot diff can't single it out).
        pick_list = new_boards if new_boards else boards
        self._result(f"{len(pick_list)} candidate gateway(s) on the LAN and "
                      "no automatic match — asking the operator to pick.")
        self._set_verify_status(
            "Select the gateway you just programmed.", "#a60")
        chosen = self._ask_pick_board(pick_list)
        if not chosen:
            return None, None
        ip, host = chosen
        self._result(f"Operator selected: {host} at {ip}\n")
        return ip, host

    def _do_verify(self, manual_target=None):
        template = (getattr(self, "expected_hostname_template", None)
                    or self.hostname.get())
        user = self.expected_user or self.username.get()
        pw = self.expected_pw or self.password.get()

        if paramiko is None:
            self._result("ERROR: paramiko not installed. "
                          "Run: sudo apt install python3-paramiko")
            return False

        # ---- Manual target path: skip discovery entirely ------------------
        if manual_target is not None:
            m_ip, m_host = manual_target
            if not m_ip and m_host:
                self._result(f"Manual entry: resolving {m_host}.local ...")
                m_ip = self._resolve_hostname(m_host)
                if not m_ip:
                    self._result(f"Could not resolve {m_host}. Trying the "
                                  "name directly for SSH.")
            if m_ip:
                self._result(f"Manual entry: verifying {m_host or m_ip} "
                              f"at {m_ip}\n")
            else:
                self._result(f"Manual entry: verifying {m_host} "
                              "(by hostname)\n")
            ip = m_ip or m_host
            host = m_host or ""
        else:
            # Enforce a settle delay after programming. First boot runs
            # firstrun.sh then reboots, so the board isn't discoverable for
            # ~90-120 s. If the operator clicked Find and Verify sooner,
            # wait out the remainder. If 2 min already elapsed, no-op.
            if self.program_finished_at is not None:
                elapsed = time.time() - self.program_finished_at
                remaining = POST_PROGRAM_SETTLE_SECS - elapsed
                if remaining > 0:
                    self._result(
                        f"Programming finished {int(elapsed)} s ago. The "
                        "board is still completing first boot (firstrun.sh "
                        "runs, then the board reboots once).")
                    self._result(
                        f"Waiting {int(remaining)} s before discovery so "
                        "the board has time to come up...")
                    while True:
                        remaining = (POST_PROGRAM_SETTLE_SECS
                                     - (time.time()
                                        - self.program_finished_at))
                        if remaining <= 0:
                            break
                        mins, secs = divmod(int(remaining), 60)
                        self._set_verify_status(
                            f"Board still booting — discovery starts in "
                            f"{mins}:{secs:02d}", "#a60")
                        time.sleep(1)
                    self._result("Settle delay complete — starting "
                                  "discovery.\n")
                    self._set_verify_status("Searching for the board…",
                                            "#000")
                else:
                    self._result(
                        f"Programming finished {int(elapsed)} s ago "
                        "(past the 2-min settle window) — starting "
                        "discovery immediately.\n")

            self._result(f"Hostname template: {template}")

            have_token = bool(self.program_id)
            if have_token:
                # Primary path: identify the board by the unique program-id
                # token written during programming. This works even when
                # re-programming a board already on the LAN. Discovery here
                # just enumerates boards (snapshot=None so it returns
                # promptly); we then SSH each one to find the token match,
                # retrying because the freshly-programmed board may still be
                # running firstrun.sh and not yet answer.
                self._result("Identifying the programmed board by its "
                              "program-id token…\n")
                token_deadline = time.time() + 300
                ip, host = None, None
                while time.time() < token_deadline:
                    boards = self._collect_all_boards(
                        template, deadline=time.time() + 60, snapshot=None)
                    if boards:
                        self._result(
                            f"Checking {len(boards)} gateway(s) for this "
                            "run's program-id…")
                        match = self._match_by_program_id(boards, user, pw)
                        if match is not None:
                            ip, host = match
                            self._result(
                                f"Confirmed by program-id: {host} at {ip}\n")
                            break
                    remaining = int(token_deadline - time.time())
                    if remaining <= 0:
                        break
                    self._result(
                        f"Programmed board not confirmed yet — it may still "
                        f"be running first boot. Retrying (~{remaining}s "
                        "left)…\n")
                    self._set_verify_status(
                        f"Waiting for the programmed board to finish first "
                        f"boot (~{remaining}s left)", "#a60")
                    time.sleep(8)

                if ip is None:
                    # Token never matched. The board may be unreachable, or
                    # firstrun.sh did not install the token. Fall back to the
                    # snapshot diff / operator pick on whatever was found.
                    self._result("Could not confirm the board by program-id. "
                                  "Falling back to manual selection.\n")
                    boards = self._collect_all_boards(
                        template, deadline=time.time() + 30, snapshot=None)
                    ip, host = self._select_verify_target(boards)
            else:
                # No token this session (Verify run without a prior Program).
                # Use the original snapshot-diff discovery + Option C pick.
                self._result("Each gateway names itself CareBloom<eth0-MAC>, "
                              "so we list every CareBloom gateway on the "
                              "LAN…\n")
                boards = self._collect_all_boards(
                    template, deadline=time.time() + 300,
                    snapshot=self.lan_snapshot)
                ip, host = self._select_verify_target(boards)

            if ip is None:
                # Discovery failed or operator declined - offer manual entry.
                fb = self._ask_manual_target(
                    reason="The gateway could not be found automatically.")
                if not fb:
                    self._result("Verification cancelled.")
                    return False
                m_ip, m_host = fb
                if not m_ip and m_host:
                    self._result(f"Manual entry: resolving "
                                  f"{m_host}.local ...")
                    m_ip = self._resolve_hostname(m_host)
                ip = m_ip or m_host
                host = m_host or ""
                self._result(f"Manual entry: verifying {host or ip}\n")

        # Record for other tabs (Label Generation 'Use verified board's MAC').
        self.found_ip.set(ip)
        self.found_host.set(host or "")
        if host:
            hex_only = re.sub(r"[^0-9A-Fa-f]", "", host)
            if len(hex_only) >= 12:
                try:
                    self.found_mac = format_mac_colons(hex_only[-12:])
                except ValueError:
                    self.found_mac = hex_only[-12:]

        # Patient SSH connect. The board may be found on the network (ARP /
        # mDNS) while it is still booting - sshd comes up late, after
        # firstrun.sh and the first-boot reboot. "Connection refused" on
        # port 22 is a transient mid-boot state, not a failure, so keep
        # retrying for a while rather than giving up after a few tries.
        # An AUTHENTICATION failure, by contrast, is real (wrong password) -
        # retrying won't help, so stop immediately on that.
        client = None
        last_err = None
        ssh_deadline = time.time() + 180   # retry sshd for up to 3 minutes
        attempt = 0
        while time.time() < ssh_deadline:
            attempt += 1
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(hostname=ip, username=user, password=pw,
                               timeout=10, allow_agent=False,
                               look_for_keys=False)
                break
            except paramiko.AuthenticationException as e:
                # Wrong credentials - retrying is pointless.
                last_err = e
                self._result(f"SSH authentication failed: {e}")
                self._result("Check the username / password on the "
                              "Configure tab.")
                client = None
                break
            except Exception as e:
                last_err = e
                client = None
                remaining = int(ssh_deadline - time.time())
                if remaining <= 0:
                    break
                self._result(
                    f"SSH not ready yet (attempt {attempt}): {e}")
                self._set_verify_status(
                    f"Board found — waiting for SSH to come up "
                    f"(~{remaining}s left)", "#a60")
                time.sleep(5)
        if client is None:
            self._result(f"\nCould not SSH in after {attempt} attempts. "
                          f"Last error: {last_err}")
            self._result("The board was found on the network but SSH never "
                          "became reachable. If it was still booting, wait "
                          "a moment and run Find and Verify again.")
            return False
        self._result(f"SSH OK (after {attempt} attempt(s)).\n")

        checks = [
            ("Identity",    "cat /etc/os-release | head -5"),
            ("Kernel",      "uname -a"),
            ("Model",       "tr -d '\\000' </proc/device-tree/model; echo"),
            ("Hostname",    "hostnamectl --static"),
            ("Uptime",      "uptime"),
            ("Clock",       "echo \"now: $(date)\"; echo \"booted: "
                            "$(uptime -s)\"; timedatectl 2>/dev/null "
                            "| grep -E 'System clock|NTP' || true"),
            ("Memory",      "free -h"),
            ("Disk",        "df -h /"),
            ("CPU temp",    "vcgencmd measure_temp 2>/dev/null || cat /sys/class/thermal/thermal_zone0/temp"),
            ("Throttled?",  "vcgencmd get_throttled 2>/dev/null || echo n/a"),
            ("Network",     "ip -br addr; echo; ip route"),
            # Current boot only (-b 0). Show seconds-since-boot timestamps
            # (-o short-monotonic) instead of wall-clock, because a Pi with no
            # RTC reports a fake build-date time until NTP syncs - wall-clock
            # timestamps on early boot entries are misleading. Benign,
            # expected lines (PCIe link-down on an empty M.2 slot, Bluetooth
            # LE-audio/SAP plugin gripes, wpa_supplicant interface-type
            # probes) are filtered out so only actionable errors show.
            ("Boot errors (this boot)",
             "journalctl -b 0 -p err -o short-monotonic --no-pager 2>/dev/null "
             "| grep -v -E "
             "'fd500000.pcie: link down|bluetoothd.*(vcp|mcp|bap|sap)|"
             "wpa_supplicant.*Registration to specific type' "
             "| tail -n 8 || true"),
            ("AP status",
             "nmcli -t -f NAME,DEVICE,STATE connection show --active "
             "2>/dev/null | grep -i wlan || echo '(no active wlan connection)'"),
        ]
        all_ok = True
        for label, cmd in checks:
            try:
                stdin, stdout, stderr = client.exec_command(cmd, timeout=15)
                out = stdout.read().decode(errors="replace").rstrip()
                err = stderr.read().decode(errors="replace").rstrip()
                self._result(f"=== {label} ===")
                if out: self._result(out)
                if err: self._result("[stderr] " + err)
                rc = stdout.channel.recv_exit_status()
                # These checks are informational - a non-zero exit (e.g. grep
                # finding no matches) is not a verify failure.
                if rc != 0 and label not in (
                        "Boot errors (this boot)", "AP status"):
                    all_ok = False
            except Exception as e:
                self._result(f"=== {label} ===\n[error] {e}")
                all_ok = False
        client.close()

        try:
            new = not os.path.exists(LOG_FILE)
            with open(LOG_FILE, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp", "hostname", "ip", "mac", "user",
                                "image", "result"])
                w.writerow([datetime.now().isoformat(timespec="seconds"),
                            host or "", ip,
                            getattr(self, "found_mac", "") or "",
                            user,
                            os.path.basename(self.image_path.get()),
                            "PASS" if all_ok else "FAIL"])
            self._result(f"\nLogged to {LOG_FILE}")
        except Exception as e:
            self._result(f"\nCouldn't write log: {e}")
        return all_ok

    def _expected_hostname_from_mac(self, template, mac_no_colons):
        """Replace {MAC}/{MAC6}/{MACUPPER} tokens in template the same way
        the on-target firstrun.sh does, so we can predict what hostname the
        target chose."""
        mac = mac_no_colons.lower()
        mac6 = mac[-6:]
        macup = mac.upper()
        h = (template
             .replace("{MAC}", mac)
             .replace("{MAC6}", mac6)
             .replace("{MACUPPER}", macup))
        h = re.sub(r"[^A-Za-z0-9-]", "", h)[:63].strip("-")
        return h

    def _resolve_hostname(self, hostname):
        """Resolve a short hostname to an IPv4 address via mDNS (.local) then
        plain DNS. Returns the IP string, or '' if it can't be resolved.

        This is the fallback for hosts that avahi-browse DISCOVERS (a '+'
        record) but whose service-resolve ('=' record) times out - common
        when a board is still early in boot: it announces its service but
        its mDNS responder isn't fully answering yet. Resolving the
        '<hostname>.local' name directly succeeds in that window even when
        the service-resolve does not."""
        fqdn = hostname if "." in hostname else hostname + ".local"
        if which("avahi-resolve"):
            try:
                out = subprocess.check_output(
                    ["avahi-resolve", "-4", "-n", fqdn],
                    text=True, stderr=subprocess.DEVNULL, timeout=8)
                parts = out.split()
                if len(parts) >= 2 and ":" not in parts[1]:
                    return parts[1]
            except Exception:
                pass
        try:
            info = socket.getaddrinfo(fqdn, None, socket.AF_INET)
            if info:
                return info[0][4][0]
        except Exception:
            pass
        return ""

    def _avahi_browse_workstations(self):
        """Browse mDNS for hosts and return a list of (ip, hostname) tuples
        (IPv4 only, hostname is the short name).

        Browses ONLY the _workstation._tcp service type - the type Raspberry
        Pi OS advertises its hostname under. This is important: a bare
        'avahi-browse -atrpk' resolves EVERY service on the network,
        including printers and other devices whose resolves can time out
        (e.g. a Brother printer's _privet/_ipp services), dragging the whole
        call past its timeout and making it return nothing. Restricting to
        _workstation._tcp keeps it fast and reliable.

        Handles BOTH record types from avahi-browse:
          '=' resolved records - give the IP directly.
          '+' found-but-unresolved records - happen when a board is still
              booting (it announces its service but its mDNS responder
              isn't answering address queries yet). For these we extract
              the hostname from the service name and resolve it directly.
              Without this, a freshly-booted board is invisible until its
              service-resolve stops timing out.

        Returns [] on any failure (avahi missing, timeout, etc.)."""
        if not which("avahi-browse"):
            return []
        try:
            out = subprocess.check_output(
                ["avahi-browse", "_workstation._tcp", "-rptk"],
                text=True, stderr=subprocess.DEVNULL, timeout=45)
        except subprocess.TimeoutExpired:
            self._result("(avahi-browse timed out)")
            return []
        except Exception:
            return []

        by_host = {}        # short hostname(lower) -> ip
        unresolved = set()  # short hostnames seen only as '+' records

        for line in out.splitlines():
            f = line.split(";")
            if line.startswith("="):
                # =;iface;proto;name;type;domain;host;ip;port;txt
                if len(f) < 8 or f[2] != "IPv4":
                    continue
                host = f[6].split(".")[0]
                ip = f[7]
                if host and ip and ":" not in ip:
                    by_host[host.lower()] = ip
            elif line.startswith("+"):
                # +;iface;proto;name;type;domain
                # The service name (field 3) is '<hostname>\032[MAC]' on
                # Raspberry Pi OS - the leading token is the hostname.
                if len(f) < 4:
                    continue
                svc = f[3]
                # Hostname = text up to the first escaped space (\032) or
                # literal space; strip any avahi backslash-escapes.
                name = re.split(r"\\032| ", svc)[0]
                name = re.sub(r"\\(\d{3})",
                               lambda m: chr(int(m.group(1))), name)
                name = name.split(".")[0].strip()
                if name:
                    unresolved.add(name.lower())

        # For any host seen only as '+' (no '=' record), resolve it directly.
        for name in unresolved:
            if name in by_host:
                continue
            ip = self._resolve_hostname(name)
            if ip:
                by_host[name] = ip

        return [(ip, host) for host, ip in by_host.items()]

    def _snapshot_lan_boards(self, hostname_template):
        """Return the set of CareBloom gateway hostnames (lowercased) visible
        on the LAN right now, via a single quick mDNS browse. Used to record
        'what was already here' before programming, so Verify can later spot
        the newly-programmed board as the new arrival. Best-effort: returns
        an empty set if avahi-browse isn't available or finds nothing."""
        prefix_l = hostname_template.split("{")[0].lower()
        names = set()
        for ip, host in self._avahi_browse_workstations():
            if host.lower().startswith(prefix_l):
                names.add(host.lower())
        return names

    def _collect_all_boards(self, hostname_template, deadline,
                            snapshot=None):
        """Discover CareBloom gateways on the LAN; return [(ip, hostname)].

        If snapshot is given (the set of gateway hostnames present before
        programming), keeps retrying until a board NOT in the snapshot
        appears - the newly-programmed board - or the deadline passes. If
        snapshot is None, returns as soon as any board is found.
        """
        prefix = hostname_template.split("{")[0]
        prefix_l = prefix.lower()
        self._result(f"Discovering all gateways with names starting "
                      f"'{prefix}'...")

        found = {}   # hostname(lower) -> (ip, hostname)

        def add(ip, host):
            if not ip or not host:
                return
            if ":" in ip:        # skip IPv6
                return
            if host.lower().startswith(prefix_l):
                found[host.lower()] = (ip, host)

        def mdns_browse_all():
            """Enumerate mDNS workstation hosts via the shared helper."""
            for ip, host in self._avahi_browse_workstations():
                add(ip, host)

        def arp_sweep_all():
            """Ping-sweep local subnets, then resolve every responding host's
            name and keep the CareBloom ones."""
            for net in local_subnets():
                try:
                    network = ipaddress.ip_network(net)
                except Exception:
                    continue
                hosts = list(network.hosts())
                if len(hosts) > 512:
                    self._result(f"Skipping ping-sweep of {net}: too large "
                                  f"({len(hosts)} hosts). Relying on mDNS.")
                    continue
                self._result(f"Scanning {net} ({len(hosts)} hosts)...")
                threads = []
                for ipa in hosts:
                    t = threading.Thread(
                        target=lambda i=str(ipa): subprocess.call(
                            ["ping", "-c", "1", "-W", "1", i],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL),
                        daemon=True)
                    t.start()
                    threads.append(t)
                    if len(threads) >= 64:
                        for tt in threads:
                            tt.join()
                        threads = []
                for tt in threads:
                    tt.join()
                try:
                    arp = subprocess.check_output(
                        ["ip", "neigh", "show"], text=True)
                except Exception:
                    arp = ""
                for line in arp.splitlines():
                    m = re.match(r"^(\d+\.\d+\.\d+\.\d+)\s", line)
                    if not m:
                        continue
                    ipa = m.group(1)
                    name = ""
                    if which("avahi-resolve"):
                        try:
                            o = subprocess.check_output(
                                ["avahi-resolve", "-a", ipa],
                                text=True, stderr=subprocess.DEVNULL,
                                timeout=5)
                            p = o.split()
                            if len(p) >= 2:
                                name = p[1].split(".")[0]
                        except Exception:
                            pass
                    if not name:
                        try:
                            name = socket.gethostbyaddr(
                                ipa)[0].split(".")[0]
                        except Exception:
                            name = ""
                    add(ipa, name)

        while time.time() < deadline:
            self._result("Browsing mDNS...")
            mdns_browse_all()
            if not found:
                # mDNS came up empty - fall back to a ping-sweep.
                arp_sweep_all()
            if found:
                boards = sorted(found.values(), key=lambda b: b[1].lower())
                # If we have a snapshot of what was on the LAN before
                # programming, keep retrying until a board that ISN'T in the
                # snapshot appears - that new arrival is the board just
                # programmed. Returning as soon as ANY board is found would
                # give up too early when a pre-existing gateway answers
                # first and the freshly-programmed board is still booting.
                if snapshot is not None:
                    new = [b for b in boards
                           if b[1].lower() not in snapshot]
                    if new:
                        self._result(
                            f"Found {len(boards)} gateway(s); "
                            f"{len(new)} new since programming: "
                            + ", ".join(h for _, h in new))
                        return boards
                    remaining = int(deadline - time.time())
                    self._result(
                        f"Found only previously-known gateway(s): "
                        + ", ".join(h for _, h in boards)
                        + f". Waiting for the new board to appear "
                        f"(~{remaining}s left)...")
                    self._set_verify_status(
                        f"Waiting for the programmed gateway to come "
                        f"online (~{remaining}s left)", "#a60")
                    time.sleep(5)
                    continue
                # No snapshot - return as soon as any board is found.
                self._result(f"Found {len(boards)} gateway(s): "
                              + ", ".join(h for _, h in boards))
                return boards
            self._result("No gateways found yet; retrying...")
            time.sleep(5)
        # Deadline passed. Return whatever we have (may be only old boards,
        # or nothing) - the caller decides how to report it.
        if found:
            return sorted(found.values(), key=lambda b: b[1].lower())
        return []

    def _find_board_by_mac(self, hostname_template, deadline):
        """Find the freshly-programmed board on the LAN. Returns (ip, hostname).

        The board names itself from its own eth0 MAC using hostname_template
        (e.g. 'CareBloom{MAC}'). We do NOT try to guess which host is 'new' -
        that breaks when re-programming a board that's been on the LAN before.
        Instead we find the static prefix of the template (the part before
        the first {...} token, e.g. 'Carebloom') and look for any host whose
        mDNS hostname starts with that prefix.

        Strategy, in order:
          1. Try direct mDNS resolution of the prefix as a wildcard isn't
             possible, so we ping-sweep, collect Pi-MAC hosts, and for each
             one resolve its hostname (reverse mDNS / avahi) and check the
             prefix.
          2. If avahi-resolve of an IP doesn't yield a name, predict the
             hostname from the MAC and try forward resolution to confirm.
        """
        # Static prefix of the template = everything before the first '{'.
        prefix = hostname_template.split("{")[0]
        prefix_l = prefix.lower()
        self._result(f"Looking for any host whose name starts with '{prefix}'")

        def record_found(ip, host, mac=None):
            """Record discovery results so other tabs (e.g. Label Generation)
            can use them. found_mac is derived from the hostname when not
            given directly - the board names itself CareBloom<eth0-MAC>, so
            the trailing 12 hex digits of the hostname ARE the MAC. This runs
            on every discovery path (mDNS and ping-sweep) so found_mac is set
            no matter how the board was found."""
            if not mac and host:
                hex_only = re.sub(r"[^0-9A-Fa-f]", "", host)
                if len(hex_only) >= 12:
                    mac = hex_only[-12:]
            if mac:
                try:
                    self.found_mac = format_mac_colons(mac)
                except ValueError:
                    self.found_mac = mac
            return ip, host

        def resolve_ip_to_name(ip):
            """Reverse-resolve an IP to a hostname via avahi, then DNS."""
            if which("avahi-resolve"):
                try:
                    out = subprocess.check_output(
                        ["avahi-resolve", "-a", ip],
                        text=True, stderr=subprocess.DEVNULL, timeout=5)
                    parts = out.split()
                    if len(parts) >= 2:
                        return parts[1].split(".")[0]
                except Exception:
                    pass
            try:
                name = socket.gethostbyaddr(ip)[0]
                return name.split(".")[0]
            except Exception:
                return ""

        def mdns_find_by_prefix():
            """Return (ip, name) for the first mDNS workstation host whose
            hostname starts with the CareBloom prefix, via the shared helper.
            Netmask-independent - no subnet scan needed."""
            for ip, host in self._avahi_browse_workstations():
                if host.lower().startswith(prefix_l):
                    return ip, host
            return None, None

        # --- Step 1: direct mDNS discovery (fast, netmask-independent) ------
        self._result("Browsing mDNS for a CareBloom host...")
        ip, host = mdns_find_by_prefix()
        if ip:
            self._result(f"Found via mDNS: {host} at {ip}")
            return record_found(ip, host)
        self._result("mDNS browse found nothing yet; falling back to "
                      "LAN ping-sweep.")

        # --- Step 2: ping-sweep + ARP fallback -----------------------------
        scanned_any = False
        while time.time() < deadline:
            subs = local_subnets()
            if not subs:
                self._result("WARNING: no scannable subnet found (the host's "
                              "network may be wider than /22). Retrying mDNS "
                              "instead.")
                ip, host = mdns_find_by_prefix()
                if ip:
                    self._result(f"Found via mDNS: {host} at {ip}")
                    return record_found(ip, host)
                time.sleep(5)
                continue
            for net in subs:
                try:
                    network = ipaddress.ip_network(net)
                except Exception:
                    continue
                hosts = list(network.hosts())
                if len(hosts) > 512:
                    self._result(f"Skipping ping-sweep of {net}: too large "
                                  f"({len(hosts)} hosts). Relying on mDNS.")
                    continue
                scanned_any = True
                self._result(f"Scanning {net} ({len(hosts)} hosts)...")
                threads = []
                for ip in hosts:
                    t = threading.Thread(
                        target=lambda i=str(ip): subprocess.call(
                            ["ping", "-c", "1", "-W", "1", i],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL),
                        daemon=True)
                    t.start()
                    threads.append(t)
                    if len(threads) >= 64:
                        for tt in threads:
                            tt.join()
                        threads = []
                for tt in threads:
                    tt.join()

                try:
                    arp = subprocess.check_output(
                        ["ip", "neigh", "show"], text=True)
                except Exception:
                    arp = ""

                pi_hosts = []
                other_hosts = []
                for line in arp.splitlines():
                    m = re.match(
                        r"^(\d+\.\d+\.\d+\.\d+)\s.*\s([0-9a-f:]{17})",
                        line, re.I)
                    if not m:
                        continue
                    ip, mac = m.group(1), m.group(2).lower()
                    if any(mac.startswith(o) for o in PI_MAC_PREFIXES):
                        pi_hosts.append((ip, mac))
                    else:
                        other_hosts.append((ip, mac))

                # Fallback: Raspberry Pi keeps registering new MAC OUI blocks,
                # so a board can have a MAC that PI_MAC_PREFIXES doesn't list
                # yet. Don't let an unlisted OUI hide a board - also check any
                # non-Pi-OUI host whose hostname matches the CareBloom prefix.
                for ip, mac in other_hosts:
                    actual = resolve_ip_to_name(ip)
                    if actual and actual.lower().startswith(prefix_l):
                        self._result(
                            f"Found {ip} by hostname ({actual}); its MAC "
                            f"OUI {mac[:8]} is not in PI_MAC_PREFIXES - "
                            "consider adding it.")
                        pi_hosts.append((ip, mac))

                if pi_hosts:
                    self._result(f"Candidate hosts found: {len(pi_hosts)} - "
                                  "resolving hostnames...")

                for ip, mac in pi_hosts:
                    # First: what the board *should* be named, from its MAC.
                    mac_no_colons = mac.replace(":", "")
                    predicted = self._expected_hostname_from_mac(
                        hostname_template, mac_no_colons)

                    # Resolve the host's actual name.
                    actual = resolve_ip_to_name(ip)

                    matched = False
                    if actual and actual.lower().startswith(prefix_l):
                        matched = True
                        name = actual
                    elif predicted.lower().startswith(prefix_l):
                        # Confirm the predicted name resolves to this IP.
                        try:
                            resolved = socket.gethostbyname(
                                f"{predicted}.local")
                            if resolved == ip:
                                matched = True
                                name = predicted
                        except Exception:
                            pass

                    if matched:
                        self._result(
                            f"MATCH: {ip} ({mac}) -> hostname: {name}")
                        return record_found(ip, name, mac=mac)
                    else:
                        self._result(
                            f"  {ip} ({mac}) name='{actual or '?'}' "
                            f"predicted='{predicted}' - no prefix match")
            time.sleep(5)
        return None, ""

    # ---- App installation workflow ----------------------------------------
    def _start_install_thread(self):
        problems = []
        zip_path = self.app_zip_path.get().strip()
        if not zip_path or not os.path.isfile(zip_path):
            problems.append("App archive not found (set it on the Configure tab)")
        low = zip_path.lower()
        if not low.endswith((".zip", ".tar.gz", ".tgz", ".tar")):
            problems.append("App file must be a .tar.gz, .tgz, .tar or .zip")
        if not self.app_name.get().strip():
            problems.append("App folder name is empty (e.g. CARE001)")
        if not self.install_host.get().strip():
            problems.append("Target host is empty")
        if not self.username.get().strip():
            problems.append("Username is empty (Configure tab)")
        if not self.password.get():
            problems.append("Password is empty (Configure tab)")
        if paramiko is None:
            problems.append("paramiko not installed")
        if problems:
            messagebox.showerror("Fix before installing", "\n".join(problems))
            return

        self._reset_install_steps()
        self._start_transcript(INSTALL_LOG, "Carebloom App Installation")
        self.install_results.delete("1.0", "end")
        self.install_btn.configure(state="disabled")
        self.install_status.configure(text="Installing...", foreground="#0a7")
        threading.Thread(target=self._install_workflow, daemon=True).start()

    def _install_workflow(self):
        ok = False
        try:
            ok = self._do_install()
        except Exception as e:
            self._iresult(f"EXCEPTION: {e}")
        finally:
            def finish():
                self.install_btn.configure(state="normal")
                if ok:
                    self.install_status.configure(
                        text="✓ Application installed — GW2000 is rebooting "
                             "(~60-90 s).",
                        foreground="#080")
                else:
                    self.install_status.configure(
                        text="✗ Installation failed — see output above.",
                        foreground="#c00")
            self.after(0, finish)

    def _ssh_exec(self, client, cmd, label=None, sudo=False, get_pty=False,
                  watch_errors=False):
        """Run a command over an open SSH client. If sudo=True, runs via
        'sudo -S' and feeds the password on stdin. Streams output to the
        install results pane. Returns the exit status.

        Reads the raw channel (not stdout.readline) because apt/dpkg emit
        progress bars terminated by carriage returns, not newlines -
        readline() would block forever waiting for a '\\n'. We also pull
        stdout and stderr together to avoid a pipe-buffer deadlock.

        If watch_errors=True, scans output for genuine failure markers and
        appends them to self._install_saw_errors."""
        pw = self.password.get()
        if sudo:
            # -S reads password from stdin, -p '' suppresses the prompt text
            full = f"sudo -S -p '' bash -c {shlex.quote(cmd)}"
        else:
            full = cmd
        if label:
            self._iresult(f"--- {label} ---")
        self._iresult(f"$ {cmd}")

        # Open a session channel directly so we control PTY size and reads.
        chan = client.get_transport().open_session(timeout=30)
        if get_pty:
            # Give dpkg/apt a real terminal size so they don't stall or
            # emit zero-width progress bars.
            chan.get_pty(term="xterm", width=120, height=40)
        chan.settimeout(None)
        chan.exec_command(full)

        if sudo:
            try:
                chan.sendall(pw + "\n")
            except Exception:
                pass

        error_patterns = (
            "Failed to enable unit",        # service file genuinely missing
            "command not found",
            "Unit file .* does not exist",
            "is not a directory",
            "Read-only file system",
            "Could not open lock file",     # ran without sudo
            "are you root",
        )
        benign_substrings = (
            "mv: cannot stat",
            "dos2unix:",
            "Skipping",
            "Binary symbol",
        )

        def check(line):
            if not watch_errors:
                return
            low = line.lower()
            for b in benign_substrings:
                if b.lower() in low:
                    return
            for pat in error_patterns:
                if re.search(pat, line, re.I):
                    self._install_saw_errors.append(line.strip())
                    break

        def emit(line):
            line = strip_ansi(line).rstrip()
            if not line:
                return
            # With a PTY, the password we send on stdin is echoed back by
            # the terminal. Don't print it to the transcript.
            if sudo and line.strip() == pw.strip():
                return
            if "[sudo] password" in line:
                return
            self._iresult(line)
            check(line)

        # Read raw bytes from both stdout and stderr until the command exits.
        # Split on \r and \n so apt/dpkg progress bars become discrete lines.
        buf = ""
        last_activity = time.time()
        STALL_LIMIT = 900  # 15 min with zero output => assume hung
        while True:
            got_data = False
            if chan.recv_ready():
                data = chan.recv(65536).decode(errors="replace")
                if data:
                    buf += data
                    got_data = True
            if chan.recv_stderr_ready():
                data = chan.recv_stderr(65536).decode(errors="replace")
                if data:
                    buf += data
                    got_data = True

            # Split out complete lines on either terminator.
            while True:
                idx = min(
                    [i for i in (buf.find("\n"), buf.find("\r")) if i >= 0],
                    default=-1)
                if idx < 0:
                    break
                emit(buf[:idx])
                buf = buf[idx + 1:]

            if got_data:
                last_activity = time.time()
            else:
                if chan.exit_status_ready() and not chan.recv_ready() \
                        and not chan.recv_stderr_ready():
                    break
                if time.time() - last_activity > STALL_LIMIT:
                    self._iresult(f"[no output for {STALL_LIMIT}s - "
                                  f"assuming the remote command hung]")
                    try:
                        chan.close()
                    except Exception:
                        pass
                    return 124  # timeout-style exit code
                time.sleep(0.1)

        # Flush any trailing partial line.
        if buf.strip():
            emit(buf)

        rc = chan.recv_exit_status()
        if rc != 0:
            self._iresult(f"[exit {rc}]")
        return rc

    def _do_install(self):
        host = self.install_host.get().strip()
        user = self.username.get().strip()
        pw = self.password.get()
        zip_path = self.app_zip_path.get().strip()
        app_name = self.app_name.get().strip()
        zip_name = os.path.basename(zip_path)
        remote_zip = f"/tmp/{zip_name}"
        # IMPORTANT: setupSystemLocal.sh contains a hard-coded 'cd /tmp' and
        # then uses relative paths (cp CARE001/etc/..., cp -r CARE001 ...).
        # So the app folder MUST be extracted into /tmp - that is the only
        # place the script will find it. (This is why a manual run from /tmp
        # worked but runs from /home/pi failed.) We extract into /tmp and
        # the app folder ends up at /tmp/<app_name> e.g. /tmp/CARE001.
        extract_dir = "/tmp"
        app_dir = f"{extract_dir}/{app_name}"

        # STEP 1: SSH connect
        self._set_install_step(0, "running")
        self._iresult(f"Connecting to {user}@{host} ...")
        client = None
        last_err = None
        for attempt in range(4):
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(hostname=host, username=user, password=pw,
                               timeout=10, allow_agent=False,
                               look_for_keys=False)
                break
            except Exception as e:
                last_err = e
                self._iresult(f"  attempt {attempt+1} failed: {e}")
                time.sleep(5)
                client = None
        if client is None:
            self._iresult(f"Could not connect: {last_err}")
            self._set_install_step(0, "fail")
            return False
        self._iresult("SSH connected.")
        self._set_install_step(0, "ok")

        try:
            # STEP 2: SCP the zip to /tmp
            self._set_install_step(1, "running")
            self._iresult(f"--- Transferring {zip_name} to {remote_zip} ---")
            try:
                size = os.path.getsize(zip_path)
                sftp = client.open_sftp()
                last_pct = [-1]
                def progress(sent, total):
                    pct = int(sent * 100 / total) if total else 0
                    if pct != last_pct[0] and pct % 10 == 0:
                        last_pct[0] = pct
                        self._iresult(f"  {pct}%  ({sent}/{total} bytes)")
                sftp.put(zip_path, remote_zip, callback=progress)
                sftp.close()
                self._iresult(f"Transfer complete ({size} bytes).")
            except Exception as e:
                self._iresult(f"SCP failed: {e}")
                self._set_install_step(1, "fail")
                return False
            self._set_install_step(1, "ok")

            # STEP 3: extract into /tmp (where setupSystemLocal.sh expects it)
            self._set_install_step(2, "running")
            low = zip_path.lower()
            is_tar = low.endswith((".tar.gz", ".tgz", ".tar"))
            if is_tar:
                # tar handles .gz automatically with -z; works for plain .tar too.
                # tar is always present on Pi OS, so no fallback install needed.
                extract_cmd = (
                    f"cd {shlex.quote(extract_dir)} && "
                    f"tar -xzf {shlex.quote(remote_zip)} 2>/dev/null || "
                    f"tar -xf {shlex.quote(remote_zip)}"
                )
                rc = self._ssh_exec(client, extract_cmd,
                                    label=f"Extracting tarball into {extract_dir}")
            else:
                # .zip path
                rc = self._ssh_exec(
                    client,
                    f"cd {shlex.quote(extract_dir)} && "
                    f"unzip -o {shlex.quote(remote_zip)}",
                    label=f"Unzipping into {extract_dir}")
                if rc != 0:
                    # unzip may be missing on a Lite image
                    self._iresult("unzip failed - attempting to install it...")
                    self._ssh_exec(
                        client,
                        "DEBIAN_FRONTEND=noninteractive apt-get install -y unzip",
                        label="Install unzip", sudo=True)
                    rc = self._ssh_exec(
                        client,
                        f"cd {shlex.quote(extract_dir)} && "
                        f"unzip -o {shlex.quote(remote_zip)}",
                        label="Unzipping (retry)")
            if rc != 0:
                self._set_install_step(2, "fail")
                return False
            # Sanity-check the app folder exists
            rc = self._ssh_exec(
                client, f"test -d {shlex.quote(app_dir)}",
                label=f"Checking {app_dir} exists")
            if rc != 0:
                self._iresult(
                    f"ERROR: expected folder {app_dir} not found after "
                    f"extraction. Check the 'App folder name' on the "
                    f"Configure tab - it must match the top-level folder "
                    f"inside the archive.")
                self._set_install_step(2, "fail")
                return False
            self._set_install_step(2, "ok")

            # STEP 4: apt install dos2unix
            self._set_install_step(3, "running")
            rc = self._ssh_exec(
                client,
                "DEBIAN_FRONTEND=noninteractive apt-get install -y dos2unix",
                label="apt install dos2unix", sudo=True)
            if rc != 0:
                self._iresult("dos2unix install failed - is the gateway online?")
                self._set_install_step(3, "fail")
                return False
            self._set_install_step(3, "ok")

            # STEP 5: dos2unix + chmod on bin/ and etc/
            self._set_install_step(4, "running")
            # Run all four operations; bin/* and etc/* get dos2unix + chmod +x.
            # (etc only chmods *.sh, matching the original manual procedure.)
            fixcmd = (
                f"cd {shlex.quote(app_dir)} && "
                f"dos2unix ./bin/* && "
                f"chmod +x ./bin/* && "
                f"dos2unix ./etc/* && "
                f"chmod +x ./etc/*.sh"
            )
            rc = self._ssh_exec(client, fixcmd,
                                label="Fix line endings + permissions",
                                sudo=True)
            if rc != 0:
                self._set_install_step(4, "fail")
                return False
            self._set_install_step(4, "ok")

            # STEP 6: run setupSystemLocal.sh
            self._set_install_step(5, "running")
            # setupSystemLocal.sh contains a hard-coded 'cd /tmp' near the top
            # and then uses relative paths (cp CARE001/etc/..., cp -r CARE001
            # ...). The CARE001 folder must therefore live in /tmp. We extract
            # there (extract_dir) and launch the script from /tmp too, exactly
            # matching the manual procedure that worked.
            setup_script = f"./{app_name}/bin/setupSystemLocal.sh"
            self._install_saw_errors = []
            rc = self._ssh_exec(
                client,
                f"cd {shlex.quote(extract_dir)} && {setup_script}",
                label="Running setupSystemLocal.sh", sudo=True, get_pty=True,
                watch_errors=True)
            # setupSystemLocal.sh does not 'set -e' - it exits 0 even when cp /
            # systemctl steps fail. So a 0 exit code is NOT proof of success.
            # We scan its output for tell-tale failure lines.
            if rc != 0:
                self._iresult(f"setupSystemLocal.sh exited with code {rc}.")
                self._set_install_step(5, "fail")
                return False
            if self._install_saw_errors:
                self._iresult("")
                self._iresult("WARNING: setupSystemLocal.sh exited 0 but its "
                              "output contained failures:")
                for e in self._install_saw_errors[:20]:
                    self._iresult("  " + e)
                self._iresult("")
                self._iresult("The Carebloom app did NOT install cleanly. "
                              "This is a problem inside setupSystemLocal.sh "
                              "(it does not stop on errors), not the programmer.")
                self._set_install_step(5, "fail")
                return False
            self._set_install_step(5, "ok")

            self._iresult("\n=== Application installed successfully ===")

            # setupSystemLocal.sh ends with "Please reboot the system..." -
            # the Carebloom services only come up after a reboot. Trigger it
            # now. The reboot drops the SSH connection, so we issue it in the
            # background on the target and don't wait for an exit status
            # (there won't be one - the link dies first).
            self._iresult("")
            self._iresult("Rebooting the GW2000 to start the Carebloom "
                          "services...")
            try:
                # 'sleep 2' lets our exec call return cleanly before the box
                # goes down; nohup + & detaches it from the dying SSH session.
                rb = ("sudo -S -p '' bash -c "
                      + shlex.quote("nohup sh -c 'sleep 2; reboot' "
                                    ">/dev/null 2>&1 &"))
                stdin, stdout, stderr = client.exec_command(rb, timeout=10)
                try:
                    stdin.write(self.password.get() + "\n")
                    stdin.flush()
                except Exception:
                    pass
                self._iresult("Reboot command sent. The GW2000 will be "
                              "back up in ~60-90 seconds.")
            except Exception as e:
                # Not fatal - the install itself succeeded.
                self._iresult(f"(Could not send reboot command: {e})")
                self._iresult("Please reboot the GW2000 manually to start "
                              "the Carebloom services.")
            return True
        finally:
            try:
                client.close()
            except Exception:
                pass


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
