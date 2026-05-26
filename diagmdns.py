#!/usr/bin/env python3
"""
GW2000 mDNS discovery diagnostic.
Run this ON THE PROGRAMMER PI:  python3 diag_mdns.py
It reproduces, step by step, exactly what gw2kprog.py's discovery does,
printing what happens at each stage so we can see where a board is lost.
Safe & read-only - it only browses/resolves mDNS, changes nothing.
"""
import re, time, socket, shutil, subprocess

def which(c): return shutil.which(c)

print("=" * 60)
print("STEP 1: avahi-browse _workstation._tcp -ptk  (no -r)")
print("=" * 60)
t0 = time.time()
try:
    out = subprocess.check_output(
        ["avahi-browse", "_workstation._tcp", "-ptk"],
        text=True, stderr=subprocess.DEVNULL, timeout=20)
    print(f"(browse returned in {time.time()-t0:.2f}s)")
    print("--- raw output ---")
    print(out if out.strip() else "(EMPTY)")
except subprocess.TimeoutExpired:
    print("!!! avahi-browse TIMED OUT after 20s")
    out = ""
except Exception as e:
    print(f"!!! avahi-browse raised: {e!r}")
    out = ""

print()
print("=" * 60)
print("STEP 2: parse hostnames from '+' records")
print("=" * 60)
names = set()
for line in out.splitlines():
    if not line.startswith("+"):
        continue
    f = line.split(";")
    if len(f) < 4:
        continue
    svc = f[3]
    name = re.split(r"\\032| ", svc)[0]
    name = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), name)
    name = name.split(".")[0].strip()
    if name:
        names.add(name.lower())
print(f"parsed {len(names)} distinct name(s): {sorted(names)}")

print()
print("=" * 60)
print("STEP 3: resolve each name individually (this is _resolve_hostname)")
print("=" * 60)

def resolve_hostname(hostname):
    fqdn = hostname if "." in hostname else hostname + ".local"
    if which("avahi-resolve"):
        t = time.time()
        try:
            o = subprocess.check_output(
                ["avahi-resolve", "-4", "-n", fqdn],
                text=True, stderr=subprocess.DEVNULL, timeout=8)
            parts = o.split()
            print(f"    avahi-resolve took {time.time()-t:.2f}s -> {parts}")
            if len(parts) >= 2 and ":" not in parts[1]:
                return parts[1]
        except subprocess.TimeoutExpired:
            print(f"    avahi-resolve TIMED OUT after {time.time()-t:.2f}s")
        except Exception as e:
            print(f"    avahi-resolve raised after {time.time()-t:.2f}s: {e!r}")
    t = time.time()
    try:
        info = socket.getaddrinfo(fqdn, None, socket.AF_INET)
        print(f"    getaddrinfo fallback took {time.time()-t:.2f}s -> {info[0][4][0] if info else None}")
        if info:
            return info[0][4][0]
    except Exception as e:
        print(f"    getaddrinfo fallback raised after {time.time()-t:.2f}s: {e!r}")
    return ""

by_host = {}
for name in sorted(names):
    print(f"  resolving '{name}'...")
    t = time.time()
    ip = resolve_hostname(name)
    print(f"  => '{name}' resolved to {ip!r}  (total {time.time()-t:.2f}s)")
    if ip and ":" not in ip:
        by_host[name] = ip
    print()

print("=" * 60)
print("RESULT: _avahi_browse_workstations would return:")
print("=" * 60)
result = [(ip, host) for host, ip in by_host.items()]
print(result if result else "(EMPTY LIST  <-- this is the bug if non-empty expected)")
print()
careblooms = [r for r in result if r[1].startswith("carebloom")]
print(f"CareBloom boards in result: {careblooms}")
print(f"Total elapsed: {time.time()-t0:.2f}s")
