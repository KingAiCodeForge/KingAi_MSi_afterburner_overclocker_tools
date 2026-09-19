#!/usr/bin/env python3
"""Read-only summary of one explicitly identified Afterburner GPU config."""

from __future__ import annotations

import argparse
import json
import sys

from profile_safety import (
    PROFILE_NAMES,
    SafetyError,
    get_ini_value,
    get_section,
    resolve_config,
    validate_vf_hex,
)


SECTIONS = ("Startup",) + PROFILE_NAMES
SETTING_KEYS = (
    "CoreClkBoost",
    "MemClkBoost",
    "CoreVoltageBoost",
    "PowerLimit",
    "ThermalLimit",
    "FanMode",
    "FanSpeed",
)


def analyze(config: str, gpu_id: str, sections: list[str]) -> dict:
    path = resolve_config(config_path=config, gpu_id=gpu_id)
    raw = path.read_bytes()
    text = raw.decode("ascii", errors="strict")
    result = {
        "schema_version": 1,
        "gpu_id": gpu_id.upper(),
        "config_name": path.name,
        "sections": [],
    }
    for section_name in sections:
        try:
            block = get_section(text, section_name)
        except SafetyError as exc:
            if str(exc).startswith("Missing required"):
                continue
            raise
        settings = {
            key: value
            for key in SETTING_KEYS
            if (value := get_ini_value(block, key)) is not None
        }
        vf_hex = get_ini_value(block, "VFCurve")
        result["sections"].append(
            {
                "name": section_name,
                "settings": settings,
                "vf_curve": validate_vf_hex(vf_hex) if vf_hex else None,
            }
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Exact per-GPU .cfg path.")
    parser.add_argument("--gpu-id", required=True, help="Exact complete GPU identity.")
    parser.add_argument(
        "--section",
        action="append",
        choices=SECTIONS,
        help="Exact section to inspect; repeat as needed. Default: all known slots.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = analyze(args.config, args.gpu_id, args.section or list(SECTIONS))
    except (OSError, UnicodeError, SafetyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"GPU: {result['gpu_id']}")
        print(f"Config: {result['config_name']}")
        for section in result["sections"]:
            curve = section["vf_curve"]
            curve_label = (
                f"{curve['point_count']} points, "
                f"{curve['min_voltage_mv']:.1f}-{curve['max_voltage_mv']:.1f} mV"
                if curve
                else "no VF curve"
            )
            print(f"[{section['name']}] {curve_label}")
            for key, value in section["settings"].items():
                print(f"  {key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
