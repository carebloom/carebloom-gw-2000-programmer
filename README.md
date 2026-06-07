# Care Bloom GW2000 Programmer

Production / factory tool for programming, verifying, and labeling Care Bloom
GW2000 gateways at the flash station.

The GW2000 gateway is a Raspberry Pi **CM4** mounted on a Waveshare
**CM4-IO-Base-C** board. This tool runs on a host Raspberry Pi (a **Pi 5** is
the reference host, though a Pi 4 or a CM4-on-Pi4-adapter also works) and
provides a six-step GUI workflow:

1. **Configure** — set OS image, hostname template, Wi-Fi AP credentials.
   The first-boot OS login is fixed at `pi` / `raspberry`; it is *not*
   exposed in the UI (see "First-boot vs. shipping credentials" below).
2. **Program** — write the OS image to the CM4's eMMC via `rpiboot`, then
   stage a `firstrun.sh` that sets the hostname, the fixed first-boot
   `pi`/`raspberry` login, SSH, and mDNS advertisement on first boot.
3. **Verify** — discover the freshly-programmed board on the LAN, SSH in,
   and run health checks (identity, kernel, hostname, network, AP status).
4. **App Installation** — copy and run the Care Bloom application firmware
   tarball over SSH (the gateway-firmware payload, including
   `setupSystemLocal.sh` and the `CARE001` tree, lives in a separate repo).
5. **Label Generation** — print a QR / serial label to a Zebra ZD410
   thermal printer at `/dev/usb/lp0`.
6. **Passwords** — rotate the root, `pi`, and Web Control Panel passwords
   from the post-install constant (`carebloom-eng`) to unique per-device
   values before the gateway is boxed and shipped.

A program log is appended to `~/gw2k_program_log.csv` and full per-tab
transcripts are written to `~/gw2k-programmer-logs/`.

## Repository layout

```
gw2kprog.py            The programmer GUI (Tkinter). Runs in place from this
                       repo; the desktop launcher created by setup_host.sh
                       points straight at it, so `git pull` updates are
                       picked up next time the app is restarted.
setup_host.sh          One-time host provisioning. Installs apt deps, builds
                       rpiboot, installs udev/polkit/sudoers rules, adds the
                       operator to the plugdev and lp groups, pre-downloads
                       a default OS image, and creates the desktop launcher.
gateway-firmware/      Created by setup_host.sh. Drop the Care Bloom
                       application firmware tarball (`.tar.gz`) here; the
                       App Installation tab auto-picks the newest archive.
```

The companion **gateway-firmware repo** (not part of this repo) contains
`setupSystemLocal.sh`, `avahi-daemon.conf`, the `CARE001` payload, and the
Care Bloom application binaries. See "Gateway-firmware coordination" below.

## Host requirements

- Raspberry Pi 5, Pi 4, or a CM4 on a Pi-4-form-factor adapter, running
  Raspberry Pi OS **Bookworm** or **Trixie** (desktop).
- USB-C from the host to the CM4-IO-Base-C's USB-C port for `rpiboot`
  (carries the mass-storage link).
- A **USB-C power/data splitter** with a real 5V/3A supply on the CM4
  side is **required** in production — see "Hardware notes" below.
  Programming a CM4 over a single USB-A→USB-C cable from the host is
  power-marginal and will eventually fail.
- Ethernet from the host and from every gateway under test to the same LAN
  (any IP scheme works; the tool sweeps the host's `/24`).
- For the Label Generation tab: a Zebra ZD410 thermal printer on USB
  (enumerates as `/dev/usb/lp0`).

## Host setup (one-time)

Clone the repo into the operator's home and run the setup script as the
desktop user (it will `sudo` when needed):

```bash
git clone <your-repo-url> ~/carebloom-gw-2000-programmer
cd ~/carebloom-gw-2000-programmer
bash setup_host.sh
```

When it finishes, **log out and back in** (or reboot) so the new
`plugdev`/`lp` group membership and the sudoers/udev rules take effect.
A "GW2000 Programmer" icon appears on the desktop.

`setup_host.sh` is safe to re-run; it converges on the configured state
without duplicating anything. Re-run it after pulling repo updates that
change the rules or the launcher.

## Programming workflow

1. **Configure** the image path, hostname template (default
   `CareBloom{MAC}`), Wi-Fi AP SSID/password, and Wi-Fi country. The
   first-boot OS login (`pi` / `raspberry`) is fixed and not editable.
2. **Program**: set the CM4-IO-Base-C **BOOT switch to ON**, connect the
   USB-C from the splitter's data side to the IO-board's USB-C port (and
   the splitter's 5V/3A supply to the CM4-side power input), and click
   **Program GW2000**. The Steps panel ticks through Detect → Identify
   eMMC → Unmount → Program image (`dd`) → Re-attach for config → Write
   first-boot config → Sync and eject. A post-`dd` size check confirms
   the eMMC didn't drop off the bus mid-write; if it did, Program FAILs
   with a clear message naming the likely cause (under-power / marginal
   cable). On success: unplug USB-C, set **BOOT switch OFF**, connect
   the 5V/3A USB-C power supply directly to the CM4-IO-Base-C, and
   power up.
3. **Verify**: click **Find and Verify**. The tool waits for the board to
   come online, identifies it by a unique per-program token written into
   `/etc/gw2k_program_id` by `firstrun.sh`, SSHes in, and runs health
   checks. The hostname is gated to `CareBloom<12 hex>` — anything else
   (a trailing `.local`, an Avahi `-2` collision suffix) is a hard FAIL.
4. **App Installation**: drop the application firmware tarball into the
   `gateway-firmware/` folder. The App Installation tab picks the newest
   archive, copies it to the gateway, and runs `setupSystemLocal.sh` over
   SSH. After this step, **both** the OS-level root/pi accounts and the
   Web Control Panel are set to the constant `carebloom-eng`.
5. **Label Generation**: prints a label to the Zebra ZD410.
6. **Passwords**: rotate root/pi and WCP from `carebloom-eng` to unique
   values before shipping. See "Passwords tab" below.

### First-boot vs. shipping credentials

The gateway has *three* distinct credential states across its lifecycle,
and operators get confused if they aren't kept straight:

| Stage                            | OS login (root/pi)  | Web Control Panel   |
| -------------------------------- | ------------------- | ------------------- |
| After Program, before App Install| `pi` / `raspberry`  | (not installed yet) |
| After App Installation           | `root`/`pi` = `carebloom-eng` | `carebloom-eng` |
| After Passwords-tab rotation     | unique per device   | unique per device   |

The Configure tab's first-boot login is fixed at `pi` / `raspberry` —
those are the credentials the flasher uses to SSH into the board between
first boot and App Installation. Exposing them in the UI was removed
because nothing the operator does should change them (the App
Installation step blows them away regardless, and the Passwords tab is
where actual per-device credentials get set).

### Token-based identification

The Verify tab does **not** rely on hostname alone to identify the
programmed board. At the start of each Program run, a unique random token
(`secrets.token_hex(8)`) is generated; the flasher writes it onto the
boot partition; `firstrun.sh` copies it to `/etc/gw2k_program_id` on the
booted system. Verify discovers candidate IPs via mDNS *and* a concurrent
ARP/ping-sweep (so a board that isn't advertising mDNS yet still gets
found), then SSHes each candidate and reads the token. The board carrying
*this* run's token is the target — works even when re-programming a
board that's already on the LAN, and works whether or not mDNS is
functioning on the network.

Candidates are filtered to plausible gateways (hostname `CareBloom*`,
`raspberrypi`, or unnamed) before any SSH probe, so the tool never
login-attempts unrelated devices on the LAN.

### Passwords tab

The Passwords tab rotates a finished gateway's credentials from the
post-install constant `carebloom-eng` to unique per-device values. It
operates on a board the operator has already Verified.

**Fields:**

- **Gateway MAC / Gateway IP** — defaults from the last verified board
  (use the "Use verified board" button to refresh).
- **Root seed / WCP seed** — read-only constants (`root_seed` /
  `web_seed`) used by the Generate algorithm.
- **Current root pw / Current WCP pw** — the credentials that
  Test and Write use to authenticate to the gateway. Default
  `carebloom-eng`. After a successful Write, these auto-sync to the
  values that were just written, so a subsequent Test or Write Just
  Works.
- **Root password / WCP password** — the *new* values that Write will
  set on the gateway. Any string is accepted, including
  `carebloom-eng` to revert.

**Operations:**

- **Generate** — derives a deterministic 12-char hex value per password
  from `SHA-256(MAC + seed)` (first 12 hex chars). Useful for
  populating the Root/WCP password fields without typing.
- **Test** — uses the Current pw fields to SSH as root and run
  `CareBloomPwd -v` to verify the WCP. Pure reachability check;
  answers "do the Current passwords work right now?".
- **Write** — uses the Current pw fields to authenticate, then sets the
  gateway's WCP to the WCP password field (via
  `CareBloomPwd -s <current> <new>`) and the OS-level **root and pi**
  accounts to the Root password field (via `chpasswd`). WCP first,
  root last, single SSH session. Partial failures are reported
  explicitly.

**Typical rotation:** Use verified board → Test (confirms reachability
on `carebloom-eng`) → Generate (fills Root/WCP fields with new hex
values) → Write (rotates the gateway; Current fields auto-sync to the
new values) → Test (confirms the rotation took).

**Security note.** The Generate algorithm is deterministic from the MAC
(printed on the device label) and seeds stored in this source file. It
provides per-device *uniqueness*, not unguessable secrets. This matches
the legacy password tool's behavior; for a security-review-grade
solution the seeds would need to move out of source into a per-host
secret store.

## Gateway-firmware coordination

A few settings the flasher relies on live in the gateway-firmware repo,
not this one. If those settings are wrong, programming succeeds but
Verify will fail in confusing ways. The gateway-firmware repo must ship:

- **`setupSystemLocal.sh`** — the hostname set must use the bare name,
  *not* the FQDN. `hostnamectl set-hostname "$(cat /etc/ssid.txt)"`,
  **never** `... .local`. Baking `.local` into the system hostname makes
  Avahi rename the board to `<name>-2`, and the flasher's hostname gate
  treats that as a FAIL.
- **`CARE001/etc/avahi-daemon.conf`** — must NOT hard-set
  `host-name=` (commenting it is fine); leaving an active `host-name=`
  forces every gateway to announce the same mDNS name and collide. Must
  set `publish-workstation=yes` so the Verify tab's fast-path mDNS
  discovery sees the board.

`setupSystemLocal.sh` re-asserts both settings after copying the file, so
even an older `CARE001` tarball with the wrong defaults works correctly.

## Hardware notes (learned the hard way)

- **Use a USB-C power/data splitter for programming.** A single
  USB-A→USB-C from the host has to *both* power the CM4 and carry the
  mass-storage USB link. Under sustained eMMC write load the CM4 browns
  out, the link drops mid-`dd`, and you get a corrupt eMMC that boots
  partially or not at all (board comes up as `raspberrypi`, never runs
  `firstrun.sh` to completion). The splitter lets you power the CM4 from
  a real 5V/3A PSU during programming while the host provides only the
  data path. The post-`dd` size check catches this if it ever recurs.
- **USB-C cables vary wildly.** Mark and reserve known-good cables for
  programming use; don't draw from a bin of random USB-C cables on the
  bench. A cable that passes link negotiation can still fail under
  sustained transfer. When programming starts misbehaving in a way that
  wasn't there yesterday, suspect the data cable first.
- **CM4-IO-Base-C BOOT switch:** ON for programming, OFF for normal boot.
  It's a slide switch near the USB-C port, not a jumper.
- **First boot takes a few minutes.** Filesystem expansion plus
  `firstrun.sh` plus a reboot. Verify's retry loop waits patiently; an
  operator clicking immediately after power-up is supported.

## Logs

- `~/gw2k_program_log.csv` — per-program summary (one row per board).
- `~/gw2k-programmer-logs/program_transcript.log` — full Program-tab log.
- `~/gw2k-programmer-logs/verify_transcript.log` — full Verify-tab log,
  including `Boot-to-ready` measurements.
- `~/gw2k-programmer-logs/install_transcript.log` — App Installation log.
- `~/gw2k-programmer-logs/password_transcript.log` — Passwords-tab log
  (Generate / Test / Write actions and their outcomes).

