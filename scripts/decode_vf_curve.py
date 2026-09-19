#!/usr/bin/env python3
"""Read-only decoder for MSI Afterburner profile V/F curves."""

from __future__ import annotations

import argparse
import os
from datetime import datetime

from profile_safety import SafetyError, resolve_config
from vf_curve_format import (
    VF_POINT_BYTES,
    VFCurveFormatError,
    decode_vf_curve,
)


SECTIONS = ["Startup", "Profile1", "Profile2", "Profile3", "Profile4", "Profile5"]
EFFECTIVE_DIP_NOTICE_MHZ = 15.1


def ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def find_config(custom_path: str, gpu_id: str) -> str:
    """Resolve the exact config and verify its complete GPU identity."""

    return str(resolve_config(config_path=custom_path, gpu_id=gpu_id))


def parse_ini_sections(text: str) -> dict[str, str]:
    """Parse INI-style text into section bodies."""

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
    """Return one simple INI value from a section body."""

    for line in block.splitlines():
        if line.strip().startswith(key + "="):
            value = line.split("=", 1)[1].strip()
            return value if value else None
    return None


def decode_vf_hex(hex_str: str) -> list[dict[str, int | float]]:
    """Decode points with explicit base, offset, and effective-frequency labels."""

    try:
        curve = decode_vf_curve(hex_str)
    except VFCurveFormatError:
        return []
    return [point.as_dict() for point in curve.points]


def print_section(name: str, block: str, show_all: bool = False) -> None:
    """Print decoded settings for one profile section."""

    core_raw = get_ini_value(block, "CoreClkBoost")
    mem_raw = get_ini_value(block, "MemClkBoost")
    power = get_ini_value(block, "PowerLimit") or "N/A"
    thermal = get_ini_value(block, "ThermalLimit") or "N/A"
    fan_mode = get_ini_value(block, "FanMode") or "N/A"
    fan_speed = get_ini_value(block, "FanSpeed") or "N/A"
    vf_hex = get_ini_value(block, "VFCurve")

    core = f"{int(core_raw) / 1000:.0f}" if core_raw else "N/A"
    memory = f"+{int(mem_raw) / 1000:.0f}" if mem_raw else "N/A"
    fan_label = (
        "manual" if fan_mode == "1" else "auto" if fan_mode == "0" else fan_mode
    )

    print(f"{ts()} {'=' * 50}")
    print(f"{ts()} === {name} ===")
    print(
        f"{ts()}   Core: {core} MHz | Mem: {memory} MHz | "
        f"Power: {power}% | Thermal: {thermal}C"
    )
    print(f"{ts()}   Fan: {fan_speed}% ({fan_label})")
    print()

    if not vf_hex:
        print(f"{ts()}   No VF curve data")
        print()
        return

    points = decode_vf_hex(vf_hex)
    if not points:
        print(
            f"{ts()}   VF curve present but could not decode "
            f"({len(vf_hex)} hex chars)"
        )
        print()
        return

    print(
        f"{ts()}   VF Curve ({len(points)} points, "
        f"{VF_POINT_BYTES} bytes/point, {len(vf_hex)} hex chars):"
    )
    print(
        f"{ts()}   {'Idx':>5}  {'Voltage':>10}  {'Base':>10}  "
        f"{'Offset':>10}  {'Effective':>10}"
    )
    print(
        f"{ts()}   {'-----':>5}  {'-------':>10}  {'----':>10}  "
        f"{'------':>10}  {'---------':>10}"
    )

    previous_effective = None
    for point in points:
        effective = float(point["effective_frequency_mhz"])
        is_inflection = (
            previous_effective is None
            or abs(effective - previous_effective) > 0.5
        )
        is_edge = point["index"] == 0 or point["index"] >= len(points) - 2
        if show_all or is_inflection or is_edge:
            marker = ""
            if (
                previous_effective is not None
                and effective < previous_effective - EFFECTIVE_DIP_NOTICE_MHZ
            ):
                marker = "  <-- large effective-frequency decrease; inspect"
            print(
                f"{ts()}   [{point['index']:>3}]  "
                f"{point['voltage_mv']:>8.1f} mV  "
                f"{point['base_frequency_mhz']:>8.1f} MHz  "
                f"{point['offset_mhz']:>+8.1f} MHz  "
                f"{effective:>8.1f} MHz{marker}"
            )
        previous_effective = effective

    voltages = [float(point["voltage_mv"]) for point in points]
    effective_frequencies = [
        float(point["effective_frequency_mhz"]) for point in points
    ]
    print()
    print(
        f"{ts()}   Range: {min(voltages):.1f}-{max(voltages):.1f} mV | "
        f"Peak effective: {max(effective_frequencies):.1f} MHz"
    )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decode MSI Afterburner VF curves from .cfg profile files."
    )
    parser.add_argument(
        "--config",
        "-c",
        required=True,
        help="Exact path to a specific per-GPU .cfg file.",
    )
    parser.add_argument(
        "--gpu-id",
        required=True,
        help="Exact complete GPU identity (the config filename stem).",
    )
    parser.add_argument(
        "--all",
        "-a",
        action="store_true",
        help="Show all VF points, not just effective-frequency inflections.",
    )
    args = parser.parse_args()

    try:
        config_path = find_config(args.config, args.gpu_id)
    except SafetyError as exc:
        parser.error(str(exc))
    print(f"{ts()} Reading config: {os.path.basename(config_path)}")
    print(f"{ts()} Full path: {config_path}")
    print()

    with open(config_path, encoding="ascii", errors="replace") as handle:
        text = handle.read()

    sections = parse_ini_sections(text)
    for section_name in SECTIONS:
        if section_name in sections:
            print_section(section_name, sections[section_name], show_all=args.all)
        else:
            print(f"{ts()} === {section_name} === (not found in config)")
            print()


if __name__ == "__main__":
    main()
