#!/usr/bin/env python3
"""Analyze the actual VF curve data from disk - one-time diagnostic."""
import os
import struct
import re

AB_PROFILES_DIR = os.environ.get(
    "AB_PROFILES_DIR",
    os.path.join(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        "MSI Afterburner", "Profiles",
    ),
)

CFG = os.path.join(AB_PROFILES_DIR, "VEN_10DE&DEV_2482&SUBSYS_40901458&REV_A1&BUS_1&DEV_0&FN_0.cfg")

with open(CFG, "r", encoding="ascii", errors="replace") as f:
    text = f.read()

for sec_name in ["Startup", "Profile4"]:
    m = re.search(rf"\[{sec_name}\].*?VFCurve=([0-9A-Fa-f]+)", text, re.DOTALL)
    if not m:
        print(f"[{sec_name}] — no VFCurve"); continue
    
    vf = m.group(1)
    hdr = bytes.fromhex(vf[:16])
    ver, cnt = struct.unpack("<II", hdr)
    
    print(f"=== [{sec_name}] ===")
    print(f"Header: version=0x{ver:08X} ({ver}), point_count={cnt}")
    print(f"Total hex: {len(vf)}")
    print(f"Data area: 16 + {256*24} = {16 + 256*24}")
    print(f"Footer: {len(vf) - 16 - 256*24} hex chars = {(len(vf) - 16 - 256*24)//2} bytes")
    print()

    # Decode points  
    data = vf[16:]
    print("VF Curve (inflection points only):")
    prev_freq = -1.0
    for i in range(cnt):
        raw = bytes.fromhex(data[i*24:(i+1)*24])
        adj, volt, freq = struct.unpack("<fff", raw)
        if abs(freq - prev_freq) > 0.5 or i == 0 or i == cnt-1:
            print(f"  [{i:3d}]  adj={adj:8.1f}  volt={volt:8.2f} mV  freq={freq:8.1f} MHz")
        prev_freq = freq

    # Check if the non-active slots are really zero
    zero_start = cnt * 24
    zero_end = 256 * 24
    zero_area = data[zero_start:zero_end]
    non_zero_in_padding = sum(1 for c in zero_area if c != '0')
    print(f"\n  Padding area ({256-cnt} slots): {'all zeros' if non_zero_in_padding == 0 else f'{non_zero_in_padding} non-zero chars!'}")

    # Footer analysis
    footer_hex = vf[16 + 256*24:]
    if footer_hex:
        fdata = bytes.fromhex(footer_hex)
        print(f"\n  Footer ({len(fdata)} bytes):")
        # Print first 40 bytes as uint32s
        for j in range(min(len(fdata)//4, 10)):
            val = struct.unpack_from("<I", fdata, j*4)[0]
            fval = struct.unpack_from("<f", fdata, j*4)[0]
            print(f"    offset {j*4:3d}: uint32={val:10d} (0x{val:08X})  float={fval:12.2f}")
        
        # Find the thermal floor data
        for j in range(len(fdata)//4):
            val = struct.unpack_from("<I", fdata, j*4)[0]
            if val == 3 and j*4 + 4 + 3*16 <= len(fdata):
                print(f"\n  Thermal floor data at offset {j*4}:")
                for k in range(3):
                    fp_off = j*4 + 4 + k*16
                    f1, f2, f3, f4 = struct.unpack_from("<ffff", fdata, fp_off)
                    print(f"    Floor[{k}]: freq={f1:.1f} MHz, temp={f2:.1f} C, freq2={f3:.1f}, temp2={f4:.1f}")
                break
    
    print()
    print("=" * 60)
    print()

# Also check the dummy VEN_0000 config
print("=== VEN_0000 (dummy/template) ===")
with open(os.path.join(AB_PROFILES_DIR, "VEN_0000&DEV_0000&SUBSYS_00000000&REV_00&BUS_0&DEV_0&FN_0.cfg"), "r", encoding="ascii", errors="replace") as f:
    t2 = f.read()
print(t2[:500])
m = re.search(r"VFCurve=([0-9A-Fa-f]+)", t2)
if m:
    vf2 = m.group(1)
    hdr2 = bytes.fromhex(vf2[:16])
    v2, c2 = struct.unpack("<II", hdr2)
    print(f"\nVFCurve: version=0x{v2:08X}, count={c2}, length={len(vf2)} hex chars")
    # Decode first 5 points
    data2 = vf2[16:]
    for i in range(min(c2, 5)):
        raw = bytes.fromhex(data2[i*24:(i+1)*24])
        adj, volt, freq = struct.unpack("<fff", raw)
        print(f"  [{i:3d}]  adj={adj:8.1f}  volt={volt:8.2f} mV  freq={freq:8.1f} MHz")

# Also check the old 1080 Ti config
print()
print("=== VEN_10DE&DEV_1B81 (GTX 1080 Ti) ===")
cfg_1080 = os.path.join(AB_PROFILES_DIR, "VEN_10DE&DEV_1B81&SUBSYS_33011462&REV_A1&BUS_1&DEV_0&FN_0.cfg")
with open(cfg_1080, "r", encoding="ascii", errors="replace") as f:
    t3 = f.read()

# Section headers
sections = re.findall(r"^\[(.+)\]", t3, re.MULTILINE)
print(f"Sections: {sections}")

m3 = re.search(r"\[Startup\].*?VFCurve=([0-9A-Fa-f]+)", t3, re.DOTALL)
if m3:
    vf3 = m3.group(1)
    hdr3 = bytes.fromhex(vf3[:16])
    v3, c3 = struct.unpack("<II", hdr3)
    print(f"Startup VFCurve: version=0x{v3:08X}, count={c3}, length={len(vf3)} hex chars")
    data3 = vf3[16:]
    prev = -1.0
    for i in range(c3):
        if i*24+24 > len(data3): break
        raw = bytes.fromhex(data3[i*24:(i+1)*24])
        adj, volt, freq = struct.unpack("<fff", raw)
        if abs(freq - prev) > 0.5 or i == 0 or i == c3-1:
            print(f"  [{i:3d}]  adj={adj:8.1f}  volt={volt:8.2f} mV  freq={freq:8.1f} MHz")
        prev = freq

# Check fields in 1080 Ti that differ from 3070 Ti
for sec in ["Startup", "Profile1"]:
    m = re.search(rf"\[{sec}\](.*?)(?=\[|\Z)", t3, re.DOTALL)
    if m:
        body = m.group(1)
        keys = re.findall(r"^(\w+)=", body, re.MULTILINE)
        print(f"\n[{sec}] keys: {keys}")
