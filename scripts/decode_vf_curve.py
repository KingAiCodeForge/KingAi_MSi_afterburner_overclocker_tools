#!/usr/bin/env python3
"""
Decode MSI Afterburner VF (Voltage-Frequency) curves from .cfg profile files.

Reads the per-GPU config file, decodes the binary hex VF curve data for each
profile section, and prints a human-readable table of voltage/frequency points.

Compatible with NVIDIA GTX 10-series (Pascal) and newer.

Usage:
    python decode_vf_curve.py
    python decode_vf_curve.py --config "path/to/custom.cfg"
    python decode_vf_curve.py --all       # Show every point, not just inflections
"""

import argparse
import glob
import os
import struct
import sys
from datetime import datetime


# ── Logging tee ─────────────────────────────────────────────────────────────
# Duplicates all print() output to both console and a timestamped log file.
class _Tee:
    """Write to both a file and the original stream."""
    def __init__(self, stream, log_file):
        self._stream = stream
        self._log = log_file

    def write(self, data):
        self._stream.write(data)
        self._log.write(data)

    def flush(self):
        self._stream.flush()
        self._log.flush()

    # Pass through any other attribute to the original stream
    def __getattr__(self, name):
        return getattr(self._stream, name)


def _init_log(script_name: str):
    """Set up file logging. Returns the log file path."""
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
HEADER_BYTES = 8          # uint32 version + uint32 count
BYTES_PER_POINT = 12      # 3 x float32
HEX_PER_POINT = 24        # 12 bytes * 2
HEADER_HEX = 16           # 8 bytes * 2

SECTIONS = ["Startup", "Profile1", "Profile2", "Profile3", "Profile4", "Profile5"]


def ts() -> str:
    """Timestamp prefix for log lines."""
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def find_config(custom_path: str | None = None) -> str:
    """Find the GPU config file. Uses custom path or auto-detects."""
    if custom_path:
        if not os.path.isfile(custom_path):
            print(f"{ts()} ERROR: Config not found: {custom_path}", file=sys.stderr)
            sys.exit(1)
        return custom_path

    pattern = os.path.join(AB_PROFILES_DIR, "VEN_10DE*.cfg")
    matches = glob.glob(pattern)
    if not matches:
        print(f"{ts()} ERROR: No NVIDIA config found in {AB_PROFILES_DIR}", file=sys.stderr)
        print(f"{ts()} Is MSI Afterburner installed? Has it been run at least once?", file=sys.stderr)
        sys.exit(1)

    # If multiple GPUs, pick the largest file (most likely has profiles configured)
    best = max(matches, key=os.path.getsize)
    if len(matches) > 1:
        print(f"{ts()} Found {len(matches)} GPU configs, using largest: {os.path.basename(best)}")
    return best


def parse_ini_sections(text: str) -> dict[str, str]:
    """Parse INI-style text into {section_name: section_body} dict."""
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
    """Get a value from an INI section block. Returns None if key missing or empty."""
    for line in block.splitlines():
        if line.strip().startswith(key + "="):
            val = line.split("=", 1)[1].strip()
            return val if val else None
    return None


def decode_vf_hex(hex_str: str) -> list[dict]:
    """
    Decode a VFCurve hex string into a list of point dicts.

    Each point: {index, adjustment, voltage, frequency}
    - adjustment: per-point clock offset in MHz (float[0])
    - voltage: voltage in mV (float[1])
    - frequency: clock speed in MHz (float[2])
    """
    if len(hex_str) < HEADER_HEX:
        return []

    # Header: version (4 bytes) + point_count (4 bytes), little-endian
    header_bytes = bytes.fromhex(hex_str[:HEADER_HEX])
    _version, point_count = struct.unpack_from("<II", header_bytes)

    data_hex = hex_str[HEADER_HEX:]
    max_points = min(point_count, len(data_hex) // HEX_PER_POINT)

    points = []
    for i in range(max_points):
        offset = i * HEX_PER_POINT
        point_hex = data_hex[offset:offset + HEX_PER_POINT]
        if len(point_hex) < HEX_PER_POINT:
            break

        raw = bytes.fromhex(point_hex)
        adj, volt, freq = struct.unpack_from("<fff", raw)

        points.append({
            "index": i,
            "adjustment": round(adj, 1),
            "voltage": round(volt, 2),
            "frequency": round(freq, 1),
        })

    return points


def print_section(name: str, block: str, show_all: bool = False) -> None:
    """Print the decoded info for one profile section."""
    # Read basic settings
    core_raw = get_ini_value(block, "CoreClkBoost")
    mem_raw = get_ini_value(block, "MemClkBoost")
    pwr = get_ini_value(block, "PowerLimit") or "N/A"
    therm = get_ini_value(block, "ThermalLimit") or "N/A"
    fan_mode = get_ini_value(block, "FanMode") or "N/A"
    fan_spd = get_ini_value(block, "FanSpeed") or "N/A"
    vf_hex = get_ini_value(block, "VFCurve")

    core = f"{int(core_raw) / 1000:.0f}" if core_raw else "N/A"
    mem = f"+{int(mem_raw) / 1000:.0f}" if mem_raw else "N/A"
    fan_label = "manual" if fan_mode == "1" else "auto" if fan_mode == "0" else fan_mode

    print(f"{ts()} {'=' * 50}")
    print(f"{ts()} === {name} ===")
    print(f"{ts()}   Core: {core} MHz | Mem: {mem} MHz | Power: {pwr}% | Thermal: {therm}C")
    print(f"{ts()}   Fan: {fan_spd}% ({fan_label})")
    print()

    if not vf_hex:
        print(f"{ts()}   No VF curve data")
        print()
        return

    points = decode_vf_hex(vf_hex)
    if not points:
        print(f"{ts()}   VF curve present but could not decode ({len(vf_hex)} hex chars)")
        print()
        return

    print(f"{ts()}   VF Curve ({len(points)} points, {BYTES_PER_POINT} bytes/point, {len(vf_hex)} hex chars):")
    print(f"{ts()}   {'Idx':>5}  {'Voltage':>10}  {'Frequency':>10}  {'Adjustment':>10}")
    print(f"{ts()}   {'-----':>5}  {'-------':>10}  {'---------':>10}  {'----------':>10}")

    prev_freq = -1.0
    for pt in points:
        # Show: first, last, frequency inflections, or all if requested
        is_inflection = pt["frequency"] != prev_freq
        is_edge = pt["index"] == 0 or pt["index"] >= len(points) - 2
        if show_all or is_inflection or is_edge:
            marker = ""
            # Detect undervolt clamp: frequency drops compared to previous shown point
            if prev_freq > 0 and pt["frequency"] < prev_freq - 50:
                marker = "  <-- undervolt clamp"
            print(f"{ts()}   [{pt['index']:>3}]  {pt['voltage']:>8.1f} mV  {pt['frequency']:>8.1f} MHz  (adj: {pt['adjustment']:>6.1f}){marker}")
        prev_freq = pt["frequency"]

    # Summary
    voltages = [p["voltage"] for p in points]
    freqs = [p["frequency"] for p in points]
    print()
    print(f"{ts()}   Range: {min(voltages):.1f}–{max(voltages):.1f} mV | Peak: {max(freqs):.1f} MHz")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decode MSI Afterburner VF curves from .cfg profile files."
    )
    parser.add_argument(
        "--config", "-c",
        help="Path to a specific .cfg file. If omitted, auto-detects from Afterburner Profiles folder."
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Show all VF points, not just inflection points where frequency changes."
    )
    args = parser.parse_args()

    log_path = _init_log("decode_vf_curve")

    cfg_path = find_config(args.config)
    print(f"{ts()} Log file: {log_path}")
    print(f"{ts()} Reading config: {os.path.basename(cfg_path)}")
    print(f"{ts()} Full path: {cfg_path}")
    print()

    with open(cfg_path, "r", encoding="ascii", errors="replace") as f:
        text = f.read()

    sections = parse_ini_sections(text)

    for sec_name in SECTIONS:
        if sec_name in sections:
            print_section(sec_name, sections[sec_name], show_all=args.all)
        else:
            print(f"{ts()} === {sec_name} === (not found in config)")
            print()


if __name__ == "__main__":
    main()
