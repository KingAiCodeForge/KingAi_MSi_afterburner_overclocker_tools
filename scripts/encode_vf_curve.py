#!/usr/bin/env python3
"""
Encode VF (Voltage-Frequency) curves for MSI Afterburner .cfg files.

Generates a valid VFCurve hex blob from:
  - An undervolt specification (target voltage + frequency)
  - A flat clock offset (±MHz applied to all points)
  - A CSV file with per-point adjustments

Requires a reference VF curve (from an existing config) to copy the footer
(contains undocumented GPU Boost thermal floor data).

Compatible with NVIDIA GTX 10-series (Pascal) and newer.

Usage:
    # Undervolt: 1890 MHz locked at 850 mV, flatten above
    python encode_vf_curve.py --undervolt 850 1890

    # Flat offset: +150 MHz to all points
    python encode_vf_curve.py --offset 150

    # Import from CSV (voltage_mV, frequency_MHz, adjustment_MHz)
    python encode_vf_curve.py --from-csv my_curve.csv

    # Specify a reference config (auto-detects by default)
    python encode_vf_curve.py --undervolt 850 1890 --config "path/to/gpu.cfg"

    # Output to file instead of stdout
    python encode_vf_curve.py --undervolt 850 1890 -o my_curve.hex

    # Preview without writing (decode the generated curve)
    python encode_vf_curve.py --undervolt 850 1890 --preview
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
HEADER_BYTES = 8           # uint32 version + uint32 count
BYTES_PER_POINT = 12       # 3 x float32
HEX_PER_POINT = 24         # 12 bytes * 2
HEADER_HEX = 16            # 8 bytes * 2
TOTAL_SLOTS = 256          # Fixed buffer size (including empty padding)

# Default VF curve grid (Pascal → Ampere → Ada: 127 points, 450–1237.5 mV, 6.25 mV steps)
DEFAULT_POINT_COUNT = 127
VOLTAGE_START_MV = 450.0
VOLTAGE_STEP_MV = 6.25


def ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def find_config(custom_path: str | None = None) -> str:
    """Find a GPU config file to use as footer reference."""
    if custom_path:
        if not os.path.isfile(custom_path):
            print(f"{ts()} ERROR: Config not found: {custom_path}", file=sys.stderr)
            sys.exit(1)
        return custom_path

    pattern = os.path.join(AB_PROFILES_DIR, "VEN_10DE*.cfg")
    matches = glob.glob(pattern)
    if not matches:
        print(f"{ts()} ERROR: No NVIDIA config found in {AB_PROFILES_DIR}", file=sys.stderr)
        print(f"{ts()} A reference config is needed for the VF curve footer.", file=sys.stderr)
        sys.exit(1)

    best = max(matches, key=os.path.getsize)
    return best


def get_ini_value(text: str, section: str, key: str) -> str | None:
    """Get a value from a specific section in INI text."""
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped[1:-1] == section
        elif in_section and stripped.startswith(key + "="):
            val = stripped.split("=", 1)[1].strip()
            return val if val else None
    return None


# ── Decode helpers (shared with decode_vf_curve.py) ─────────────────────────
def decode_vf_hex(hex_str: str) -> tuple[list[dict], int, str]:
    """
    Decode a VFCurve hex string.

    Returns:
        points: list of {index, adjustment, voltage, frequency} dicts
        point_count: number of active points from header
        footer_hex: the footer portion of the hex string (everything after 256 slots)
    """
    if len(hex_str) < HEADER_HEX:
        return [], 0, ""

    header_bytes = bytes.fromhex(hex_str[:HEADER_HEX])
    _version, point_count = struct.unpack_from("<II", header_bytes)

    data_start = HEADER_HEX
    data_end = data_start + TOTAL_SLOTS * HEX_PER_POINT
    footer_hex = hex_str[data_end:] if len(hex_str) > data_end else ""

    data_hex = hex_str[data_start:]
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

    return points, point_count, footer_hex


def encode_vf_hex(points: list[dict], point_count: int, footer_hex: str) -> str:
    """
    Encode VF curve points into a hex string for Afterburner .cfg.

    Args:
        points: list of {adjustment, voltage, frequency} dicts (only active points)
        point_count: number stored in header (should match len(points))
        footer_hex: hex string copied verbatim from reference curve

    Returns:
        Complete VFCurve hex string ready for .cfg file
    """
    # Header: version=0x20000, point_count
    # Version is 0x20000 (131072) NOT 2 — verified from real Afterburner configs
    header = struct.pack("<II", 0x20000, point_count)

    # Active points
    point_data = b""
    for pt in points:
        point_data += struct.pack("<fff", pt["adjustment"], pt["voltage"], pt["frequency"])

    # Pad to 256 slots with zero bytes
    empty_count = TOTAL_SLOTS - len(points)
    padding = b"\x00" * (empty_count * BYTES_PER_POINT)

    # Combine
    full_binary = header + point_data + padding
    # Afterburner uses UPPERCASE hex in configs — match for byte-identical roundtrips
    hex_str = full_binary.hex().upper()

    # Append footer verbatim (preserves original case)
    if footer_hex:
        hex_str += footer_hex

    return hex_str


def get_reference_data(cfg_path: str) -> tuple[list[dict], int, str]:
    """
    Read the [Startup] VF curve from a config as reference.

    Returns:
        points: decoded reference points
        point_count: from header
        footer_hex: the footer to preserve
    """
    with open(cfg_path, "r", encoding="ascii", errors="replace") as f:
        text = f.read()

    # Try Startup first, then Profile1..5
    for section in ["Startup", "Profile1", "Profile2", "Profile3", "Profile4", "Profile5"]:
        vf_hex = get_ini_value(text, section, "VFCurve")
        if vf_hex and len(vf_hex) > HEADER_HEX:
            points, point_count, footer_hex = decode_vf_hex(vf_hex)
            if points:
                print(f"{ts()} Reference curve from [{section}]: {len(points)} points, "
                      f"footer: {len(footer_hex)} hex chars")
                return points, point_count, footer_hex

    print(f"{ts()} ERROR: No valid VFCurve found in any section of {cfg_path}", file=sys.stderr)
    print(f"{ts()} Run Afterburner at least once so it generates a baseline curve.", file=sys.stderr)
    sys.exit(1)


# ── Curve generation modes ──────────────────────────────────────────────────

def make_undervolt_curve(
    ref_points: list[dict],
    target_voltage_mv: float,
    target_freq_mhz: float,
) -> list[dict]:
    """
    Create an undervolt curve: set target frequency at target voltage,
    flatten all points above to the same frequency.

    Points below the target voltage keep their original frequencies.
    The adjustment field is calculated as (new_freq - original_freq).
    """
    new_points = []
    for pt in ref_points:
        new_pt = dict(pt)

        if pt["voltage"] >= target_voltage_mv:
            # At or above target: flatten to target frequency
            new_pt["frequency"] = target_freq_mhz
            new_pt["adjustment"] = round(target_freq_mhz - pt["frequency"] + pt["adjustment"], 2)
        # Below target: keep original

        new_points.append(new_pt)

    return new_points


def make_offset_curve(ref_points: list[dict], offset_mhz: float) -> list[dict]:
    """
    Create a flat offset curve: shift every point by ±offset_mhz.
    """
    new_points = []
    for pt in ref_points:
        new_pt = dict(pt)
        new_pt["adjustment"] = round(pt["adjustment"] + offset_mhz, 2)
        new_pt["frequency"] = round(pt["frequency"] + offset_mhz, 2)
        new_points.append(new_pt)
    return new_points


def load_csv_curve(csv_path: str, ref_points: list[dict]) -> list[dict]:
    """
    Load a VF curve from CSV. Expected columns: voltage_mV, frequency_MHz, adjustment_MHz

    If adjustment_MHz is omitted, it defaults to 0 for matching voltages
    or is calculated as (csv_freq - ref_freq) for known reference points.
    """
    if not os.path.isfile(csv_path):
        print(f"{ts()} ERROR: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    # Build a lookup from reference points: voltage → (freq, adj)
    ref_lookup = {}
    for pt in ref_points:
        ref_lookup[round(pt["voltage"], 2)] = (pt["frequency"], pt["adjustment"])

    csv_points = {}
    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        # Normalize column names (strip whitespace, lowercase)
        if reader.fieldnames:
            reader.fieldnames = [n.strip().lower().replace(" ", "_") for n in reader.fieldnames]

        for row in reader:
            # Skip comment rows
            first_val = list(row.values())[0] if row else ""
            if first_val and first_val.strip().startswith("#"):
                continue

            voltage = float(row.get("voltage_mv") or row.get("voltage", 0))
            frequency = float(row.get("frequency_mhz") or row.get("frequency", 0))
            adj_str = row.get("adjustment_mhz") or row.get("adjustment", "")

            if adj_str.strip():
                adjustment = float(adj_str)
            else:
                # Calculate from reference if available
                ref_data = ref_lookup.get(round(voltage, 2))
                if ref_data:
                    adjustment = round(frequency - ref_data[0] + ref_data[1], 2)
                else:
                    adjustment = 0.0

            csv_points[round(voltage, 2)] = {
                "voltage": round(voltage, 2),
                "frequency": round(frequency, 2),
                "adjustment": round(adjustment, 2),
            }

    # Merge CSV points into reference curve (CSV overrides matching voltages)
    new_points = []
    for pt in ref_points:
        v = round(pt["voltage"], 2)
        if v in csv_points:
            new_pt = dict(pt)
            new_pt["voltage"] = csv_points[v]["voltage"]
            new_pt["frequency"] = csv_points[v]["frequency"]
            new_pt["adjustment"] = csv_points[v]["adjustment"]
            new_points.append(new_pt)
        else:
            new_points.append(dict(pt))

    csv_used = sum(1 for pt in ref_points if round(pt["voltage"], 2) in csv_points)
    print(f"{ts()} CSV: {len(csv_points)} entries, {csv_used} matched reference voltage points")

    return new_points


def preview_curve(points: list[dict], label: str = "Generated") -> None:
    """Print a human-readable preview of the curve (inflection points only)."""
    print(f"{ts()} === {label} Curve ({len(points)} points) ===")
    print(f"{ts()}   {'Idx':>5}  {'Voltage':>10}  {'Frequency':>10}  {'Adjustment':>10}")
    print(f"{ts()}   {'-----':>5}  {'-------':>10}  {'---------':>10}  {'----------':>10}")

    prev_freq = -1.0
    for pt in points:
        is_inflection = abs(pt["frequency"] - prev_freq) > 0.5
        is_edge = pt["index"] == 0 or pt["index"] >= len(points) - 2
        if is_inflection or is_edge:
            print(f"{ts()}   [{pt['index']:>3}]  {pt['voltage']:>8.1f} mV  "
                  f"{pt['frequency']:>8.1f} MHz  (adj: {pt['adjustment']:>7.1f})")
        prev_freq = pt["frequency"]

    voltages = [p["voltage"] for p in points]
    freqs = [p["frequency"] for p in points]
    print()
    print(f"{ts()}   Range: {min(voltages):.1f}–{max(voltages):.1f} mV | Peak: {max(freqs):.1f} MHz")
    print()


# ── Main ────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Encode VF curves for MSI Afterburner .cfg files."
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--undervolt", "-u",
        nargs=2, metavar=("VOLTAGE_MV", "FREQ_MHZ"), type=float,
        help="Create undervolt curve: lock at VOLTAGE_MV with FREQ_MHZ, flatten above."
    )
    mode.add_argument(
        "--offset", "-f",
        type=float, metavar="MHZ",
        help="Flat clock offset applied to all points (positive=OC, negative=downclock)."
    )
    mode.add_argument(
        "--from-csv", "-i",
        metavar="CSV_PATH",
        help="Import curve from CSV file (columns: voltage_mV, frequency_MHz[, adjustment_MHz])."
    )

    parser.add_argument(
        "--config", "-c",
        help="Path to reference .cfg file. Auto-detects from Afterburner Profiles if omitted."
    )
    parser.add_argument(
        "--section", "-s",
        default="Startup",
        help="Config section to use as reference (default: Startup)."
    )
    parser.add_argument(
        "--output", "-o",
        help="Write hex blob to file instead of stdout."
    )
    parser.add_argument(
        "--preview", "-p",
        action="store_true",
        help="Print a human-readable table of the generated curve."
    )
    args = parser.parse_args()

    log_path = _init_log("encode_vf_curve")

    # Get reference curve
    cfg_path = find_config(args.config)
    print(f"{ts()} Log file: {log_path}")
    print(f"{ts()} Reference config: {os.path.basename(cfg_path)}")
    print()

    ref_points, point_count, footer_hex = get_reference_data(cfg_path)

    # Generate the new curve
    if args.undervolt:
        target_v, target_f = args.undervolt
        print(f"{ts()} Mode: Undervolt — lock at {target_v:.1f} mV / {target_f:.0f} MHz")
        new_points = make_undervolt_curve(ref_points, target_v, target_f)
    elif args.offset is not None:
        print(f"{ts()} Mode: Flat offset — {args.offset:+.0f} MHz to all points")
        new_points = make_offset_curve(ref_points, args.offset)
    elif args.from_csv:
        print(f"{ts()} Mode: CSV import — {args.from_csv}")
        new_points = load_csv_curve(args.from_csv, ref_points)
    else:
        # Should be unreachable due to required=True
        parser.error("Specify --undervolt, --offset, or --from-csv")
        return

    # Encode to hex
    hex_blob = encode_vf_hex(new_points, point_count, footer_hex)

    print(f"{ts()} Generated VFCurve: {len(hex_blob)} hex chars "
          f"({len(hex_blob) // 2} bytes)")
    print()

    # Verify roundtrip
    rt_points, rt_count, rt_footer = decode_vf_hex(hex_blob)
    if len(rt_points) != len(new_points):
        print(f"{ts()} WARNING: Roundtrip mismatch — encoded {len(new_points)} "
              f"but decoded {len(rt_points)} points", file=sys.stderr)
    else:
        mismatches = 0
        for orig, rt in zip(new_points, rt_points):
            if abs(orig["voltage"] - rt["voltage"]) > 0.01 or \
               abs(orig["frequency"] - rt["frequency"]) > 0.5:
                mismatches += 1
        if mismatches:
            print(f"{ts()} WARNING: {mismatches} points differ after roundtrip!", file=sys.stderr)
        else:
            print(f"{ts()} Roundtrip verification: OK ({len(rt_points)} points match)")

    # Preview
    if args.preview:
        print()
        preview_curve(new_points, "Generated")

    # Output
    if args.output:
        with open(args.output, "w", encoding="ascii") as f:
            f.write(hex_blob)
        print(f"{ts()} Written to: {args.output}")
    else:
        # Print hex blob to stdout (can be piped to create_profile.py)
        print()
        print(f"{ts()} === VFCurve Hex (copy this into .cfg or pass to create_profile.py) ===")
        print(hex_blob)

    print()
    print(f"{ts()} Done.")


if __name__ == "__main__":
    main()
