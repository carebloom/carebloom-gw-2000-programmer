#!/usr/bin/env python3
"""
CM4 Flasher — Production flashing & verification tool for the Raspberry Pi CM4
on the Waveshare CM4-IO-Base-C. Runs on a Raspberry Pi host (Trixie/Bookworm
with Desktop), launched by operators via a desktop icon.

Three tabs:
  1. Configure — set image / username / password / hostname / Wi-Fi
  2. Flash     — single button, runs the full flash workflow with green ticks
  3. Verify    — finds the freshly-flashed board on the LAN and SSHes in

All sudo calls are pre-authorized via /etc/sudoers.d/010-cm4-flasher so
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
import threading
import subprocess
import ipaddress
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

try:
    import paramiko
except ImportError:
    paramiko = None


# =============================================================================
# Defaults — adjust if your production layout differs.
# =============================================================================

DEFAULT_RPIBOOT_CANDIDATES = [
    "/opt/usbboot/rpiboot",
    str(Path.home() / "usbboot/rpiboot"),
    "/usr/local/bin/rpiboot",
]
DEFAULT_BOOTFILES_SUBDIR = "mass-storage-gadget64"
DEFAULT_IMAGES_DIR = str(Path.home() / "cm4-images")
DEFAULT_USERNAME = "pi"
DEFAULT_PASSWORD = "raspberry"
DEFAULT_HOSTNAME = "Carebloom{MAC}"   # {MAC} replaced with eth0 MAC at first boot

# Carebloom application installation
DEFAULT_APP_NAME = "CARE001"          # top-level folder name inside the app zip
DEFAULT_APPS_DIR = str(Path.home() / "cm4-apps")  # where app zips live on host

PI_MAC_PREFIXES = ("b8:27:eb", "dc:a6:32", "e4:5f:01",
                   "2c:cf:67", "d8:3a:dd", "28:cd:c1")

LOG_FILE = str(Path.home() / "cm4_flash_log.csv")
CONFIG_FILE = str(Path.home() / ".cm4_flasher.json")


# =============================================================================
# Helpers
# =============================================================================

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
             "NAME,SIZE,TYPE,RM,RO,TRAN,MODEL,VENDOR,MOUNTPOINT,HOTPLUG"],
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
            if 22 <= net.prefixlen <= 30:
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
                # bootfs is FAT32. We can't tell label from lsblk -o easily,
                # so we just look at the first FAT partition (which is bootfs).
                # First, try to read the partition's label/fstype via blkid.
                try:
                    blkid = subprocess.check_output(
                        ["sudo", "blkid", "-o", "export", cp],
                        text=True, stderr=subprocess.DEVNULL)
                    info = {k: v for k, v in (
                        line.split("=", 1) for line in blkid.splitlines()
                        if "=" in line)}
                except Exception:
                    info = {}
                if info.get("LABEL", "").lower() != "bootfs":
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
# Main application
# =============================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CM4 Flasher — Production")
        try:
            self.tk.call("tk", "scaling", 1.3)
        except Exception:
            pass
        self.geometry("1100x820")
        self.minsize(960, 700)

        self._log_q = queue.Queue()
        self.after(80, self._drain_log)

        self.rpiboot_path = tk.StringVar(value=guess_rpiboot())
        self.bootfiles_dir = tk.StringVar(value=guess_bootfiles(self.rpiboot_path.get()))
        self.image_path = tk.StringVar(value="")
        self.username = tk.StringVar(value=DEFAULT_USERNAME)
        self.password = tk.StringVar(value=DEFAULT_PASSWORD)
        self.hostname = tk.StringVar(value=DEFAULT_HOSTNAME)
        self.wifi_ssid = tk.StringVar(value="")
        self.wifi_psk = tk.StringVar(value="")
        self.wifi_country = tk.StringVar(value="US")

        self.found_ip = tk.StringVar(value="")
        self.found_host = tk.StringVar(value="")

        # App installation
        self.app_zip_path = tk.StringVar(value="")
        self.app_name = tk.StringVar(value=DEFAULT_APP_NAME)
        self.install_host = tk.StringVar(value="")

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

    # ---- UI ----------------------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="CM4 Production Flasher",
                  font=("DejaVu Sans", 18, "bold")).pack(side="left")
        ttk.Label(top, text="  Waveshare CM4-IO-Base-C",
                  foreground="#666").pack(side="left", padx=10, pady=8)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=4)
        self.cfg_tab = ttk.Frame(nb)
        self.flash_tab = ttk.Frame(nb)
        self.verify_tab = ttk.Frame(nb)
        self.install_tab = ttk.Frame(nb)
        nb.add(self.cfg_tab, text="1. Configure")
        nb.add(self.flash_tab, text="2. Flash")
        nb.add(self.verify_tab, text="3. Verify")
        nb.add(self.install_tab, text="4. App Installation")
        self.notebook = nb

        self._build_cfg_tab(self.cfg_tab)
        self._build_flash_tab(self.flash_tab)
        self._build_verify_tab(self.verify_tab)
        self._build_install_tab(self.install_tab)

        logf = ttk.LabelFrame(self, text="Log")
        logf.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self.log_widget = scrolledtext.ScrolledText(
            logf, height=10, wrap="word", font=("DejaVu Sans Mono", 10))
        self.log_widget.pack(fill="both", expand=True)
        self.log_widget.configure(state="disabled")

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
                    p = filedialog.askopenfilename(
                        title=title,
                        initialdir=DEFAULT_IMAGES_DIR if "image" in title.lower() else os.path.expanduser("~"))
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
        self._row(f2, 3, "Wi-Fi SSID:", self.wifi_ssid,
                  hint="  (blank = Ethernet only)")
        self._row(f2, 4, "Wi-Fi password:", self.wifi_psk, show="•")
        self._row(f2, 5, "Wi-Fi country:", self.wifi_country)

        f4 = ttk.LabelFrame(wrap, text="Carebloom application")
        f4.pack(fill="x", pady=(0, 8))
        self._row(f4, 0, "App zip:", self.app_zip_path,
                  browse=("file", "Select Carebloom app zip"))
        self._row(f4, 1, "App folder name:", self.app_name,
                  hint="  (top-level folder inside the zip, e.g. CARE001)")

        f3 = ttk.Frame(wrap)
        f3.pack(fill="x", pady=(8, 0))
        ttk.Button(f3, text="Save these as defaults",
                   command=self._save_defaults).pack(side="left", padx=4)
        ttk.Button(f3, text="Load defaults",
                   command=self._load_defaults).pack(side="left", padx=4)

        ttk.Label(wrap, justify="left", foreground="#666", text=(
            "\nWhen ready, switch to the 'Flash' tab.\n"
            "Connect a fresh CM4: 12 V off, BOOT jumper FITTED, USB-C to this Pi.\n"
            "Then click Start — apply 12 V power immediately after."
        )).pack(anchor="w", pady=(12, 0))

    def _build_flash_tab(self, parent):
        wrap = ttk.Frame(parent, padding=12)
        wrap.pack(fill="both", expand=True)

        steps_frame = ttk.LabelFrame(wrap, text="Steps")
        steps_frame.pack(fill="both", expand=False)

        step_defs = [
            ("Detect CM4 via rpiboot",
             "Plug board (BOOT jumper ON, USB-C, then 12 V)."),
            ("Identify eMMC",
             "Confirm a small (~8/16/32 GB) USB disk appears."),
            ("Unmount any partitions",
             "Detach any auto-mounted partitions."),
            ("Flash image",
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
        self.start_btn = ttk.Button(ctrl, text="Start Flash",
                                    command=self._start_flash_thread)
        self.start_btn.pack(side="left", padx=4, ipadx=20, ipady=6)
        ttk.Button(ctrl, text="Reset", command=self._reset_steps).pack(side="left", padx=4)

        self.flash_status = ttk.Label(wrap, text="Ready.",
                                       font=("DejaVu Sans", 13))
        self.flash_status.pack(anchor="w", pady=8)

    def _build_verify_tab(self, parent):
        wrap = ttk.Frame(parent, padding=12)
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, justify="left", text=(
            "After flashing finishes:\n"
            "  1. Disconnect 12 V power from the CM4 board.\n"
            "  2. REMOVE the BOOT jumper.\n"
            "  3. Connect Ethernet to your LAN (or rely on configured Wi-Fi).\n"
            "  4. Reconnect 12 V power.\n"
            "  5. Click 'Find and Verify' below.\n"
            "First boot includes filesystem expansion and a reboot — allow ~90 s."
        )).pack(anchor="w", pady=(0, 8))

        ctrl = ttk.Frame(wrap)
        ctrl.pack(fill="x", pady=4)
        self.verify_btn = ttk.Button(ctrl, text="Find and Verify",
                                     command=self._start_verify_thread)
        self.verify_btn.pack(side="left", padx=4, ipadx=20, ipady=6)
        ttk.Label(ctrl, text="Found at:").pack(side="left", padx=(20, 4))
        ttk.Entry(ctrl, textvariable=self.found_ip, width=18,
                  state="readonly").pack(side="left")
        ttk.Label(ctrl, textvariable=self.found_host,
                  foreground="#666").pack(side="left", padx=8)

        self.verify_status = ttk.Label(wrap, text="",
                                        font=("DejaVu Sans", 14, "bold"))
        self.verify_status.pack(anchor="w", pady=6)

        self.verify_results = scrolledtext.ScrolledText(
            wrap, height=18, wrap="word", font=("DejaVu Sans Mono", 10))
        self.verify_results.pack(fill="both", expand=True, pady=6)

    def _build_install_tab(self, parent):
        wrap = ttk.Frame(parent, padding=12)
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, justify="left", text=(
            "Installs the Carebloom application onto a CM4 that has already\n"
            "been flashed and verified. This automates:\n"
            "  - SCP the app zip to /tmp on the CM4\n"
            "  - Unzip it\n"
            "  - apt install dos2unix\n"
            "  - dos2unix + chmod +x on bin/ and etc/\n"
            "  - Run <app>/bin/setupSystemLocal.sh"
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
            ("Connect to CM4 over SSH",   "Uses the configured user / password."),
            ("Transfer app zip",          "SCP the zip to /tmp on the CM4."),
            ("Unzip the app",             "Extract into the home directory."),
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
        ttk.Button(ctrl, text="Reset",
                   command=self._reset_install_steps).pack(side="left", padx=4)

        self.install_status = ttk.Label(wrap, text="Ready.",
                                         font=("DejaVu Sans", 13))
        self.install_status.pack(anchor="w", pady=6)

        self.install_results = scrolledtext.ScrolledText(
            wrap, height=14, wrap="word", font=("DejaVu Sans Mono", 10))
        self.install_results.pack(fill="both", expand=True, pady=6)

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

    def _iresult(self, s):
        def upd():
            self.install_results.insert("end", s + "\n")
            self.install_results.see("end")
        self.after(0, upd)


    def log(self, s):
        self._log_q.put(s)

    def _drain_log(self):
        try:
            while True:
                s = self._log_q.get_nowait()
                self.log_widget.configure(state="normal")
                self.log_widget.insert("end", s + "\n")
                self.log_widget.see("end")
                self.log_widget.configure(state="disabled")
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
        for i in range(len(self.steps)):
            self._set_step(i, "pending")
        self.flash_status.configure(text="Ready.", foreground="#000")

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

    # ---- Flash workflow ---------------------------------------------------
    def _start_flash_thread(self):
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
        self.start_btn.configure(state="disabled")
        self.flash_status.configure(text="Running…", foreground="#0a7")
        threading.Thread(target=self._flash_workflow, daemon=True).start()

    def _flash_workflow(self):
        ok = False
        try:
            ok = self._do_flash()
        except Exception as e:
            self.log(f"EXCEPTION: {e}")
        finally:
            def finish():
                self.start_btn.configure(state="normal")
                if ok:
                    self.flash_status.configure(
                        text="✓ Flash complete. Remove BOOT jumper, "
                             "connect Ethernet, power-cycle, then go to Verify.",
                        foreground="#080")
                    self.notebook.select(self.verify_tab)
                else:
                    self.flash_status.configure(
                        text="✗ Flash failed — see log.",
                        foreground="#c00")
            self.after(0, finish)

    def _do_flash(self):
        # 1) rpiboot
        self._set_step(0, "running")
        self.log("=== Step 1: rpiboot ===")
        rc, _ = run_stream(
            ["sudo", self.rpiboot_path.get(), "-d", self.bootfiles_dir.get()],
            self.log, timeout=180)
        if rc != 0:
            self.log("rpiboot failed. Check: BOOT jumper fitted, "
                     "USB-C to host, 12 V applied AFTER rpiboot started.")
            self._set_step(0, "fail")
            return False
        self._set_step(0, "ok")

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

        # 4) flash
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
        self.log(f"=== Step 4: flash {os.path.basename(img)} → {node} ===")
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
                        or child.get("label", "").lower() == "bootfs"):
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
        # reflects the actual Ethernet MAC of the CM4 we're flashing.
        # Template tokens supported in the hostname field:
        #   {MAC}     — eth0 MAC, lowercase, no colons (e.g. b827ebabc123)
        #   {MAC6}    — last 6 hex chars of eth0 MAC (e.g. abc123)
        #   {MACUPPER}— full MAC, uppercase, no colons
        # Default template if user left it blank or set 'auto':
        if not host_template or host_template.endswith("auto"):
            host_template = "Carebloom{MAC}"
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
        ]
        if ssid:
            firstrun += [
                "# Wi-Fi (NetworkManager on Bookworm/Trixie)",
                f'nmcli connection add type wifi con-name {shlex.quote(ssid)} ifname wlan0 ssid {shlex.quote(ssid)} \\',
                "    802-11-wireless-security.key-mgmt wpa-psk \\",
                f'    802-11-wireless-security.psk {shlex.quote(psk)} \\',
                "    connection.autoconnect yes",
                f"raspi-config nonint do_wifi_country {country} 2>/dev/null || true",
                "",
            ]
        firstrun += [
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
            self.log("(Final hostname will be derived from the CM4's eth0 MAC at first boot.)")
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
        self.verify_btn.configure(state="disabled")
        self.verify_results.configure(state="normal")
        self.verify_results.delete("1.0", "end")
        self.verify_status.configure(text="Searching for the board…",
                                      foreground="#0a7")
        threading.Thread(target=self._verify_workflow, daemon=True).start()

    def _result(self, s):
        def upd():
            self.verify_results.insert("end", s + "\n")
            self.verify_results.see("end")
        self.after(0, upd)

    def _verify_workflow(self):
        ok = False
        try:
            ok = self._do_verify()
        except Exception as e:
            self._result(f"EXCEPTION: {e}")
        finally:
            def finish():
                self.verify_btn.configure(state="normal")
                if ok:
                    self.verify_status.configure(
                        text="✓ PASS — board is up and healthy.",
                        foreground="#080")
                else:
                    self.verify_status.configure(
                        text="✗ FAIL — see results above.",
                        foreground="#c00")
            self.after(0, finish)

    def _do_verify(self):
        template = (getattr(self, "expected_hostname_template", None)
                    or self.hostname.get())
        user = self.expected_user or self.username.get()
        pw = self.expected_pw or self.password.get()

        if paramiko is None:
            self._result("ERROR: paramiko not installed. "
                          "Run: sudo apt install python3-paramiko")
            return False

        self._result(f"Hostname template: {template}")
        self._result("Final hostname depends on the CM4's Ethernet MAC,")
        self._result("so we search by Pi MAC OUI on the LAN…\n")
        ip, host = self._find_board_by_mac(template,
                                            deadline=time.time() + 300)
        if not ip:
            self._result("Could not find the board within 5 minutes.")
            self._result("Check: BOOT jumper REMOVED, Ethernet connected, "
                          "12 V applied. Wait ~90 s after power-on.")
            return False
        self.found_ip.set(ip)
        self.found_host.set(host or "")
        self._result(f"Found board at {ip}"
                      + (f" — hostname: {host}" if host else "")
                      + "\n")

        client = None
        last_err = None
        for attempt in range(8):
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(hostname=ip, username=user, password=pw,
                               timeout=10, allow_agent=False, look_for_keys=False)
                break
            except Exception as e:
                last_err = e
                self._result(f"SSH attempt {attempt+1} failed: {e}")
                time.sleep(10)
                client = None
        if client is None:
            self._result(f"\nCould not SSH in. Last error: {last_err}")
            return False
        self._result("SSH OK.\n")

        checks = [
            ("Identity",    "cat /etc/os-release | head -5"),
            ("Kernel",      "uname -a"),
            ("Model",       "tr -d '\\000' </proc/device-tree/model; echo"),
            ("Hostname",    "hostnamectl --static"),
            ("Uptime",      "uptime"),
            ("Memory",      "free -h"),
            ("Disk",        "df -h /"),
            ("CPU temp",    "vcgencmd measure_temp 2>/dev/null || cat /sys/class/thermal/thermal_zone0/temp"),
            ("Throttled?",  "vcgencmd get_throttled 2>/dev/null || echo n/a"),
            ("Network",     "ip -br addr; echo; ip route"),
            ("Boot errors", "journalctl -b -p err --no-pager 2>/dev/null | tail -n 5 || true"),
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
                if rc != 0 and label != "Boot errors":
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

    def _find_board_by_mac(self, hostname_template, deadline):
        """Find the freshly-flashed board on the LAN. Returns (ip, hostname).

        The board names itself from its own eth0 MAC using hostname_template
        (e.g. 'Carebloom{MAC}'). We do NOT try to guess which host is 'new' -
        that breaks when re-flashing a board that's been on the LAN before.
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

        while time.time() < deadline:
            for net in local_subnets():
                try:
                    network = ipaddress.ip_network(net)
                except Exception:
                    continue
                hosts = list(network.hosts())
                if len(hosts) > 512:
                    continue
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
                for line in arp.splitlines():
                    m = re.match(
                        r"^(\d+\.\d+\.\d+\.\d+)\s.*\s([0-9a-f:]{17})",
                        line, re.I)
                    if not m:
                        continue
                    ip, mac = m.group(1), m.group(2).lower()
                    if any(mac.startswith(o) for o in PI_MAC_PREFIXES):
                        pi_hosts.append((ip, mac))

                if pi_hosts:
                    self._result(f"Pi-MAC hosts found: {len(pi_hosts)} - "
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
                        self.found_mac = mac
                        self._result(
                            f"MATCH: {ip} ({mac}) -> hostname: {name}")
                        return ip, name
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
            problems.append("App zip not found (set it on the Configure tab)")
        if not zip_path.lower().endswith(".zip"):
            problems.append("App file must be a .zip")
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
                        text="✓ Application installed successfully.",
                        foreground="#080")
                else:
                    self.install_status.configure(
                        text="✗ Installation failed — see output above.",
                        foreground="#c00")
            self.after(0, finish)

    def _ssh_exec(self, client, cmd, label=None, sudo=False, get_pty=False):
        """Run a command over an open SSH client. If sudo=True, runs via
        'sudo -S' and feeds the password on stdin. Streams output to the
        install results pane. Returns the exit status."""
        pw = self.password.get()
        if sudo:
            # -S reads password from stdin, -p '' suppresses the prompt text
            full = f"sudo -S -p '' bash -c {shlex.quote(cmd)}"
        else:
            full = cmd
        if label:
            self._iresult(f"--- {label} ---")
        self._iresult(f"$ {cmd}")
        stdin, stdout, stderr = client.exec_command(full, get_pty=get_pty,
                                                     timeout=600)
        if sudo:
            try:
                stdin.write(pw + "\n")
                stdin.flush()
            except Exception:
                pass
        # Stream stdout live
        for line in iter(stdout.readline, ""):
            if not line:
                break
            self._iresult(line.rstrip())
        err = stderr.read().decode(errors="replace").rstrip()
        # Filter the sudo password echo / blank lines out of stderr
        if err:
            for eline in err.splitlines():
                if eline.strip() and "[sudo] password" not in eline:
                    self._iresult("[stderr] " + eline)
        rc = stdout.channel.recv_exit_status()
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
        # Where the app unzips to. We extract in the user's home directory,
        # so the app folder ends up at ~/<app_name> (e.g. /home/pi/CARE001).
        remote_home = f"/home/{user}"
        app_dir = f"{remote_home}/{app_name}"

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

            # STEP 3: unzip into the home directory
            self._set_install_step(2, "running")
            # -o overwrite without prompting; -d extract dir
            rc = self._ssh_exec(
                client,
                f"cd {shlex.quote(remote_home)} && "
                f"unzip -o {shlex.quote(remote_zip)}",
                label=f"Unzipping into {remote_home}")
            if rc != 0:
                # unzip may be missing on a Lite image
                self._iresult("unzip failed - attempting to install it...")
                self._ssh_exec(client,
                               "DEBIAN_FRONTEND=noninteractive apt-get install -y unzip",
                               label="Install unzip", sudo=True)
                rc = self._ssh_exec(
                    client,
                    f"cd {shlex.quote(remote_home)} && "
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
                    f"ERROR: expected folder {app_dir} not found after unzip. "
                    f"Check the 'App folder name' on the Configure tab.")
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
                self._iresult("dos2unix install failed - is the CM4 online?")
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
            setup_script = f"{app_dir}/bin/setupSystemLocal.sh"
            rc = self._ssh_exec(
                client,
                f"cd {shlex.quote(app_dir)} && {shlex.quote(setup_script)}",
                label="Running setupSystemLocal.sh", sudo=True, get_pty=True)
            if rc != 0:
                self._iresult(f"setupSystemLocal.sh exited with code {rc}.")
                self._set_install_step(5, "fail")
                return False
            self._set_install_step(5, "ok")

            self._iresult("\n=== Application installed successfully ===")
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