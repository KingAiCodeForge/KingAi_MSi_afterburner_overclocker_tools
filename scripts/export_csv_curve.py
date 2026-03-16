#!/usr/bin/env python3
"""
Export MSI Afterburner VF curves to CSV files for sharing and analysis.

Reads VFCurve hex blobs from a per-GPU .cfg and writes CSV files with
voltage, frequency, and adjustment data. Includes metadata header comments.

Also supports importing CSV curves back into Afterburner hex format
(via encode_vf_curve.py --from-csv).

Compatible with NVIDIA GTX 10-series (Pascal) and newer.

Usage:
    # Export all profiles to CSV files in current directory
    python export_csv_curve.py

    # Export a specific section only
    python export_csv_curve.py --section Startup

    # Export to a specific directory
    python export_csv_curve.py --output-dir ./curves/

    # Export from a custom config
    python export_csv_curve.py --config "path/to/gpu.cfg"

    # Export only active (non-zero-adjustment) points
    python export_csv_curve.py --active-only
"""

import argparse
import csv
import glob
import os
import struct
import sys
from datetime import datetime


# ── Logging tee ─────────────────────────────────────────────────────────────
class _Tee:
    def __init__(self, stream, log_file):
        self._stream = stream
        self._log = log_file

    def write(self, data):
        self._stream.write(data)
        self._log.write(data)

    def flush(self):
        self._stream.flush()
        self._log.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _init_log(script_name: str):
    logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(logs_dir, f"{script_name}_{stamp}.log")
    log_file = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, log_file)
    sys.stderr = _Tee(sys.__stderr__, log_file)
    return log_path


# ── Constants ───────────────────────────────────────────────────────────────
AB_PROFILES_DIR = os.environ.get(
    "AB_PROFILES_DIR",
    os.path.join(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        "MSI Afterburner", "Profiles",
    ),
)
HEADER_HEX = 16
HEX_PER_POINT = 24
SECTIONS = ["Startup", "Profile1", "Profile2", "Profile3", "Profile4", "Profile5"]


def ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def find_config(custom_path: str | None = None) -> str:
    if custom_path:
        if not os.path.isfile(custom_path):
            print(f"{ts()} ERROR: Config not found: {custom_path}", file=sys.stderr)
            sys.exit(1)
        return custom_path

    pattern = os.path.join(AB_PROFILES_DIR, "VEN_10DE*.cfg")
    matches = glob.glob(pattern)
    if not matches:
        print(f"{ts()} ERROR: No NVIDIA config found in {AB_PROFILES_DIR}", file=sys.stderr)
        sys.exit(1)

    best = max(matches, key=os.path.getsize)
    return best


def parse_ini_sections(text: str) -> dict[str, str]:
    sections = {}
    current = None
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current is not None:
                sections[current] = "\n".join(lines)
            current = stripped[1:-1]
            lines = []
        else:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines)
    return sections


def get_ini_value(block: str, key: str) -> str | None:
    for line in block.splitlines():
        if line.strip().startswith(key + "="):
            val = line.split("=", 1)[1].strip()
            return val if val else None
    return None


def decode_vf_hex(hex_str: str) -> list[dict]:
    if len(hex_str) < HEADER_HEX:
        return []

    header_bytes = bytes.fromhex(hex_str[:HEADER_HEX])
    _version, point_count = struct.unpack_from("<II", header_bytes)

    data_hex = hex_str[HEADER_HEX:]
    max_points = min(point_count, len(data_hex) // HEX_PER_POINT)

    points = []
    for i in range(max_points):
        offset = i * HEX_PER_POINT
        raw = bytes.fromhex(data_hex[offset:offset + HEX_PER_POINT])
        adj, volt, freq = struct.unpack_from("<fff", raw)
        points.append({
            "index": i,
            "adjustment": round(adj, 2),
            "voltage": round(volt, 2),
            "frequency": round(freq, 2),
        })

    return points


def extract_gpu_info(cfg_path: str) -> str:
    """Extract GPU identifier from config filename."""
    basename = os.path.basename(cfg_path)
    # e.g. VEN_10DE&DEV_2482&SUBSYS_40901584&REV_A1&BUS_1&DEV_0&FN_0.cfg
    return basename.replace(".cfg", "")


def export_section(
    section_name: str,
    block: str,
    output_dir: str,
    gpu_info: str,
    cfg_path: str,
    active_only: bool = False,
) -> bool:
    """Export one section's VF curve to CSV. Returns True if exported."""
    vf_hex = get_ini_value(block, "VFCurve")
    if not vf_hex or len(vf_hex) < HEADER_HEX:
        print(f"{ts()} [{section_name}] — no VF curve data, skipping")
        return False

    points = decode_vf_hex(vf_hex)
    if not points:
        print(f"{ts()} [{section_name}] — could not decode VF curve, skipping")
        return False

    if active_only:
        points = [p for p in points if abs(p["adjustment"]) > 0.01]
        if not points:
            print(f"{ts()} [{section_name}] — no active adjustments (stock curve), skipping")
            return False

    # Read additional settings for metadata
    core_raw = get_ini_value(block, "CoreClkBoost")
    mem_raw = get_ini_value(block, "MemClkBoost")
    pwr = get_ini_value(block, "PowerLimit") or "N/A"
    therm = get_ini_value(block, "ThermalLimit") or "N/A"
    fan_mode = get_ini_value(block, "FanMode") or "N/A"
    fan_spd = get_ini_value(block, "FanSpeed") or "N/A"

    core_mhz = f"{int(core_raw) / 1000:.0f}" if core_raw else "N/A"
    mem_mhz = f"+{int(mem_raw) / 1000:.0f}" if mem_raw else "N/A"

    # Generate filename
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_section = section_name.replace(" ", "_")
    csv_filename = f"vf_curve_{safe_section}_{stamp}.csv"
    csv_path = os.path.join(output_dir, csv_filename)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        # Metadata header (comment lines)
        f.write(f"# VF Curve Export — {section_name}\n")
        f.write(f"# Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# GPU: {gpu_info}\n")
        f.write(f"# Config: {os.path.basename(cfg_path)}\n")
        f.write(f"# Core offset: {core_mhz} MHz | Mem offset: {mem_mhz} MHz\n")
        f.write(f"# Power: {pwr}% | Thermal: {therm}C | Fan: {fan_spd}% (mode={fan_mode})\n")
        f.write(f"# Points: {len(points)}\n")
        f.write(f"#\n")
        f.write(f"# To import this curve back:\n")
        f.write(f"#   python encode_vf_curve.py --from-csv {csv_filename}\n")
        f.write(f"#   python create_profile.py --profile N --vf-curve output.hex\n")
        f.write(f"#\n")

        writer = csv.writer(f)
        writer.writerow(["voltage_mV", "frequency_MHz", "adjustment_MHz"])

        for pt in points:
            writer.writerow([
                f"{pt['voltage']:.2f}",
                f"{pt['frequency']:.2f}",
                f"{pt['adjustment']:.2f}",
            ])

    print(f"{ts()} [{section_name}] -> {csv_filename} ({len(points)} points)")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export MSI Afterburner VF curves to CSV files."
    )
    parser.add_argument("--config", "-c", help="Path to per-GPU .cfg file. Auto-detects if omitted.")
    parser.add_argument("--section", "-s", default=None,
                        help="Export only this section (e.g. Startup, Profile1). Default: all.")
    parser.add_argument("--output-dir", "-o", default=".",
                        help="Directory to write CSV files. Default: current directory.")
    parser.add_argument("--active-only", "-a", action="store_true",
                        help="Only export points with non-zero adjustments (skip stock curve points).")
    args = parser.parse_args()

    log_path = _init_log("export_csv_curve")

    cfg_path = find_config(args.config)
    gpu_info = extract_gpu_info(cfg_path)

    print(f"{ts()} Log file: {log_path}")
    print(f"{ts()} Config: {os.path.basename(cfg_path)}")
    print(f"{ts()} GPU: {gpu_info}")
    print(f"{ts()} Output dir: {os.path.abspath(args.output_dir)}")
    print()

    os.makedirs(args.output_dir, exist_ok=True)

    with open(cfg_path, "r", encoding="ascii", errors="replace") as f:
        text = f.read()

    sections = parse_ini_sections(text)

    # Determine which sections to export
    if args.section:
        target_sections = [args.section]
    else:
        target_sections = SECTIONS

    exported = 0
    for sec_name in target_sections:
        if sec_name in sections:
            if export_section(sec_name, sections[sec_name], args.output_dir,
                              gpu_info, cfg_path, args.active_only):
                exported += 1
        else:
            if args.section:  # Only warn if user specifically requested this section
                print(f"{ts()} [{sec_name}] — not found in config")

    print()
    if exported > 0:
        print(f"{ts()} Exported {exported} curve(s) to {os.path.abspath(args.output_dir)}")
        print(f"{ts()} Import back with: python encode_vf_curve.py --from-csv <file.csv>")
    else:
        print(f"{ts()} No curves exported. Run Afterburner first to generate baseline curves.")


if __name__ == "__main__":
    main()
