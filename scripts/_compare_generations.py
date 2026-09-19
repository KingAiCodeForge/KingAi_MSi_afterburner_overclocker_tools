#!/usr/bin/env python3
"""Compare explicitly selected, read-only Afterburner config summaries."""

from __future__ import annotations

import argparse
import json
import sys

from _analyze_config import analyze
from profile_safety import SafetyError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        nargs=3,
        required=True,
        metavar=("LABEL", "GPU_ID", "CONFIG"),
        help=(
            "Label, exact complete GPU identity, and exact config path. "
            "Repeat for each config."
        ),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        results = [
            {
                "label": label,
                "analysis": analyze(config, gpu_id, ["Startup"]),
            }
            for label, gpu_id, config in args.input
        ]
    except (OSError, UnicodeError, SafetyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    payload = {"schema_version": 1, "inputs": results}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for item in results:
            analysis = item["analysis"]
            startup = analysis["sections"][0] if analysis["sections"] else None
            curve = startup["vf_curve"] if startup else None
            if curve:
                print(
                    f"{item['label']}: {analysis['gpu_id']} | "
                    f"version=0x{curve['version']:X} | "
                    f"points={curve['point_count']} | "
                    f"voltage={curve['min_voltage_mv']:.1f}-"
                    f"{curve['max_voltage_mv']:.1f} mV | "
                    f"peak={curve['max_frequency_mhz']:.1f} MHz"
                )
            else:
                print(f"{item['label']}: no Startup VF curve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
