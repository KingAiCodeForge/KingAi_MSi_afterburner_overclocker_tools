#!/usr/bin/env python3
"""Export decoded MSI Afterburner profile V/F curves to explicit CSV fields."""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from profile_safety import SafetyError, resolve_config, validate_vf_hex
from vf_curve_format import VFCurveFormatError, VFPoint, decode_vf_curve


SECTIONS = ["Startup", "Profile1", "Profile2", "Profile3", "Profile4", "Profile5"]


def ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def find_config(custom_path: str, gpu_id: str) -> str:
    return str(resolve_config(config_path=custom_path, gpu_id=gpu_id))


def parse_ini_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = None
    lines: list[str] = []
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
            value = line.split("=", 1)[1].strip()
            return value if value else None
    return None


def decode_vf_hex(hex_str: str) -> list[VFPoint]:
    """Decode a complete blob or return an empty list for malformed input."""

    try:
        validate_vf_hex(hex_str)
        return list(decode_vf_curve(hex_str).points)
    except (SafetyError, VFCurveFormatError):
        return []


def extract_gpu_info(config_path: str) -> str:
    return Path(config_path).stem


def export_section(
    section_name: str,
    block: str,
    output_dir: str,
    gpu_info: str,
    config_path: str,
    active_only: bool = False,
    execute: bool = False,
) -> bool:
    """Plan or export one section.  Return whether it contained a curve."""

    vf_hex = get_ini_value(block, "VFCurve")
    if not vf_hex:
        print(f"{ts()} [{section_name}] - no VF curve data, skipping")
        return False

    points = decode_vf_hex(vf_hex)
    if not points:
        print(f"{ts()} [{section_name}] - invalid VF curve, skipping")
        return False

    if active_only:
        points = [point for point in points if abs(point.offset_mhz) > 0.01]
        if not points:
            print(f"{ts()} [{section_name}] - no nonzero offsets, skipping")
            return False

    core_raw = get_ini_value(block, "CoreClkBoost")
    memory_raw = get_ini_value(block, "MemClkBoost")
    power = get_ini_value(block, "PowerLimit") or "N/A"
    thermal = get_ini_value(block, "ThermalLimit") or "N/A"
    fan_mode = get_ini_value(block, "FanMode") or "N/A"
    fan_speed = get_ini_value(block, "FanSpeed") or "N/A"

    core_mhz = f"{int(core_raw) / 1000:+.0f}" if core_raw else "N/A"
    memory_mhz = f"{int(memory_raw) / 1000:+.0f}" if memory_raw else "N/A"

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_section = section_name.replace(" ", "_")
    filename = f"vf_curve_{safe_section}_{stamp}.csv"
    csv_path = Path(output_dir) / filename

    if not execute:
        print(
            f"{ts()} [{section_name}] PLAN ONLY -> {filename} "
            f"({len(points)} points)"
        )
        return True

    with csv_path.open("x", newline="", encoding="utf-8") as handle:
        handle.write(f"# VF Curve Export - {section_name}\n")
        handle.write(f"# Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        handle.write(f"# GPU: {gpu_info}\n")
        handle.write(f"# Config: {os.path.basename(config_path)}\n")
        handle.write(
            f"# Core offset: {core_mhz} MHz | "
            f"Mem offset: {memory_mhz} MHz\n"
        )
        handle.write(
            f"# Power: {power}% | Thermal: {thermal}C | "
            f"Fan: {fan_speed}% (mode={fan_mode})\n"
        )
        handle.write(f"# Points: {len(points)}\n")
        handle.write("# Effective frequency = base frequency + offset.\n")
        handle.write("# Import is plan-only unless --execute writes a hex artifact.\n")
        handle.write("#\n")

        writer = csv.writer(handle)
        writer.writerow(
            [
                "index",
                "voltage_mV",
                "base_frequency_MHz",
                "offset_MHz",
                "effective_frequency_MHz",
            ]
        )
        for point in points:
            writer.writerow(
                [
                    point.index,
                    f"{point.voltage_mv:.2f}",
                    f"{point.base_frequency_mhz:.2f}",
                    f"{point.offset_mhz:.2f}",
                    f"{point.effective_frequency_mhz:.2f}",
                ]
            )

    print(f"{ts()} [{section_name}] -> {filename} ({len(points)} points)")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export MSI Afterburner VF curves to CSV files."
    )
    parser.add_argument(
        "--config",
        "-c",
        required=True,
        help="Exact path to the per-GPU .cfg file.",
    )
    parser.add_argument(
        "--gpu-id",
        required=True,
        help="Exact complete GPU identity (the config filename stem).",
    )
    parser.add_argument(
        "--section",
        "-s",
        default=None,
        help="Export one section (for example Startup). Default: all.",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default=".",
        help="Directory for CSV artifacts. Default: current directory.",
    )
    parser.add_argument(
        "--active-only",
        "-a",
        action="store_true",
        help="Export only points whose stored offset is nonzero.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write CSV files. Without this flag, plan only.",
    )
    args = parser.parse_args()

    try:
        config_path = find_config(args.config, args.gpu_id)
    except SafetyError as exc:
        parser.error(str(exc))
    gpu_info = extract_gpu_info(config_path)

    print(f"{ts()} Config: {os.path.basename(config_path)}")
    print(f"{ts()} GPU: {gpu_info}")
    print(f"{ts()} Output dir: {os.path.abspath(args.output_dir)}")
    print()

    if args.execute:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    with open(config_path, encoding="ascii", errors="replace") as handle:
        sections = parse_ini_sections(handle.read())

    target_sections = [args.section] if args.section else SECTIONS
    exported = 0
    for section_name in target_sections:
        if section_name in sections:
            if export_section(
                section_name,
                sections[section_name],
                args.output_dir,
                gpu_info,
                config_path,
                args.active_only,
                args.execute,
            ):
                exported += 1
        elif args.section:
            print(f"{ts()} [{section_name}] - not found in config")

    print()
    if exported:
        action = "Exported" if args.execute else "Planned"
        print(
            f"{ts()} {action} {exported} curve(s) in "
            f"{os.path.abspath(args.output_dir)}"
        )
        print(
            f"{ts()} Import with: python encode_vf_curve.py "
            "--config <cfg> --gpu-id <id> --from-csv <file.csv>"
        )
    else:
        print(f"{ts()} No valid curves found.")
        sys.exit(1)


if __name__ == "__main__":
    main()
