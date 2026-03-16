#!/usr/bin/env python3
"""Cross-generation VF curve comparison."""
import os, struct, re

AB_PROFILES_DIR = os.environ.get("AB_PROFILES_DIR", os.path.join(
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    "MSI Afterburner", "Profiles"
))

configs = {
    "Pascal GTX 1080 Ti": os.path.join(AB_PROFILES_DIR, "VEN_10DE&DEV_1B81&SUBSYS_33011462&REV_A1&BUS_1&DEV_0&FN_0.cfg"),
    "Ampere RTX 3070 Ti": os.path.join(AB_PROFILES_DIR, "VEN_10DE&DEV_2482&SUBSYS_40901458&REV_A1&BUS_1&DEV_0&FN_0.cfg"),
}

for name, cfg_path in configs.items():
    with open(cfg_path, "r", encoding="ascii", errors="replace") as f:
        text = f.read()
    
    # Find any VFCurve
    m = re.search(r"VFCurve=([0-9A-Fa-f]{100,})", text)
    if not m:
        print(f"{name}: no VFCurve found"); continue
    
    vf = m.group(1)
    hdr = bytes.fromhex(vf[:16])
    ver, cnt = struct.unpack("<II", hdr)
    
    data = vf[16:]
    footer_start = 256 * 24
    footer_hex = data[footer_start:]
    
    volts = []
    freqs = []
    for i in range(cnt):
        raw = bytes.fromhex(data[i*24:(i+1)*24])
        adj, volt, freq = struct.unpack("<fff", raw)
        volts.append(round(volt, 2))
        freqs.append(round(freq, 2))
    
    steps = sorted(set(round(volts[i+1] - volts[i], 2) for i in range(len(volts)-1)))
    
    footer_bytes = len(footer_hex) // 2 if footer_hex else 0
    
    # Check for thermal floor
    has_floor = False
    if footer_hex:
        fdata = bytes.fromhex(footer_hex)
        for j in range(len(fdata)//4):
            val = struct.unpack_from("<I", fdata, j*4)[0]
            if val == 3 and j*4 + 4 + 3*16 <= len(fdata):
                has_floor = True
                break
    
    # Check hex case
    has_upper = any(c in "ABCDEF" for c in vf)
    has_lower = any(c in "abcdef" for c in vf)
    
    print(f"=== {name} ===")
    print(f"  version: 0x{ver:08X} ({ver})")
    print(f"  point_count: {cnt}")
    print(f"  total hex: {len(vf)} chars")
    print(f"  voltage range: {min(volts):.2f} - {max(volts):.2f} mV")
    print(f"  voltage steps: {steps}")
    print(f"  freq range: {min(freqs):.1f} - {max(freqs):.1f} MHz")
    print(f"  footer: {footer_bytes} bytes")
    print(f"  thermal floor: {'yes' if has_floor else 'no'}")
    print(f"  hex case: {'UPPER' if has_upper and not has_lower else 'lower' if has_lower and not has_upper else 'mixed'}")
    print()

    # Per-GPU config keys
    for sec in ["Startup"]:
        m2 = re.search(rf"\[{sec}\](.*?)(?=\[|\Z)", text, re.DOTALL)
        if m2:
            keys = re.findall(r"^(\w+)=", m2.group(1), re.MULTILINE)
            print(f"  [{sec}] keys: {keys}")
    
    # Check for FanMode2/FanSpeed2
    if "FanMode2=" in text:
        m3 = re.search(r"FanMode2=(\S*)", text)
        m4 = re.search(r"FanSpeed2=(\S*)", text)
        print(f"  FanMode2: '{m3.group(1) if m3 else 'N/A'}'")
        print(f"  FanSpeed2: '{m4.group(1) if m4 else 'N/A'}'")
    
    print()
