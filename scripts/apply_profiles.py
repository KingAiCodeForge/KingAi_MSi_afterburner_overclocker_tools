#!/usr/bin/env python3
"""Plan or execute a guarded, offline edit of Afterburner profile files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from profile_safety import (
    PROFILE_NAMES,
    PlannedWrite,
    SafetyError,
    TransactionError,
    ascii_bytes,
    atomic_write_transaction,
    get_ini_value,
    get_section,
    patch_profile_contents,
    replace_section,
    resolve_config,
    set_ini_value,
    sha256_bytes,
    validate_profile_name,
    validate_settings,
    validate_vf_hex,
)
from vf_curve_format import VFPoint, decode_vf_curve, encode_vf_curve


EXAMPLE_MEMORY_MHZ = (0, 0, 0, 0, 0)
EXAMPLE_FAN_PERCENT = (50, 55, 60, 65, 70)


def _project_effective_curve(
    source_hex: str,
    target_reference_hex: str,
) -> tuple[str, dict[str, int | float]]:
    """Represent the source effective curve using only target offset edits."""

    source_curve = decode_vf_curve(source_hex)
    target_curve = decode_vf_curve(target_reference_hex)
    if source_curve.point_count != target_curve.point_count:
        raise SafetyError(
            "Source and target VFCurve point counts differ; refusing to "
            "replace target structural data."
        )

    projected: list[VFPoint] = []
    for source_point, target_point in zip(source_curve.points, target_curve.points):
        if abs(source_point.voltage_mv - target_point.voltage_mv) > 0.01:
            raise SafetyError(
                "Source and target VFCurve voltage grids differ at point "
                f"{target_point.index}: {source_point.voltage_mv} vs "
                f"{target_point.voltage_mv} mV."
            )
        projected.append(
            VFPoint(
                index=target_point.index,
                voltage_mv=target_point.voltage_mv,
                base_frequency_mhz=target_point.base_frequency_mhz,
                offset_mhz=(
                    source_point.effective_frequency_mhz
                    - target_point.base_frequency_mhz
                ),
            )
        )

    replacement_hex = encode_vf_curve(target_curve, projected)
    summary = validate_vf_hex(
        replacement_hex,
        reference_hex=target_reference_hex,
    )
    return replacement_hex, summary


def _integer_value(block: str, key: str, default: int | None = None) -> int:
    raw = get_ini_value(block, key)
    if raw is None:
        if default is None:
            raise SafetyError(f"Missing required {key} value.")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SafetyError(f"{key} must be an integer, got {raw!r}.") from exc


def build_plan(args: argparse.Namespace) -> tuple[dict, list[PlannedWrite]]:
    cfg_path = resolve_config(gpu_id=args.gpu_id, config_path=args.config)
    profiles_dir = cfg_path.parent
    selected = [validate_profile_name(item) for item in args.profile]
    if len(set(selected)) != len(selected):
        raise SafetyError("Each --profile may be selected only once.")
    memory_values = args.memory if args.memory is not None else list(EXAMPLE_MEMORY_MHZ)
    fan_values = args.fan if args.fan is not None else list(EXAMPLE_FAN_PERCENT)
    power = args.power if args.power is not None else 100
    thermal = args.thermal if args.thermal is not None else 83

    config_original = cfg_path.read_bytes()
    text = config_original.decode("ascii", errors="strict")
    source_name = args.source_profile
    source = get_section(text, source_name)
    vf_hex = get_ini_value(source, "VFCurve")
    if vf_hex is None:
        raise SafetyError(f"[{source_name}] does not contain VFCurve.")
    vf_summary = validate_vf_hex(vf_hex)

    source_core_khz = _integer_value(source, "CoreClkBoost", 0)
    source_core_mhz = source_core_khz / 1000
    core_mhz = args.core if args.core is not None else source_core_mhz
    validate_settings(
        {
            "core_mhz": core_mhz,
            "power_percent": power,
            "thermal_c": thermal,
        }
    )
    if int(core_mhz * 1000) != core_mhz * 1000:
        raise SafetyError("Core offset must resolve to a whole number of kHz.")
    core_khz = int(core_mhz * 1000)

    changed = text
    profile_plans: list[dict] = []
    writes: list[PlannedWrite] = []
    for profile_name in selected:
        profile_index = PROFILE_NAMES.index(profile_name)
        memory_mhz = memory_values[profile_index]
        fan_percent = fan_values[profile_index]
        validate_settings(
            {
                "memory_mhz": memory_mhz,
                "fan_percent": fan_percent,
            }
        )

        block = get_section(changed, profile_name)
        target_vf_hex = get_ini_value(block, "VFCurve")
        if target_vf_hex is None:
            raise SafetyError(
                f"[{profile_name}] does not contain a reference VFCurve; "
                "offset-only projection is not possible."
            )
        validate_vf_hex(target_vf_hex)
        projected_vf_hex, projected_vf_summary = _project_effective_curve(
            vf_hex,
            target_vf_hex,
        )
        block = set_ini_value(block, "CoreClkBoost", core_khz)
        block = set_ini_value(block, "MemClkBoost", memory_mhz * 1000)
        block = set_ini_value(block, "PowerLimit", power)
        block = set_ini_value(block, "ThermalLimit", thermal)
        block = set_ini_value(block, "FanMode", 1)
        block = set_ini_value(block, "FanSpeed", fan_percent)
        block = set_ini_value(block, "VFCurve", projected_vf_hex)
        changed = replace_section(changed, profile_name, block)

        top_level = profiles_dir / f"{profile_name}.cfg"
        if not top_level.is_file():
            raise SafetyError(
                f"Exact top-level profile target is missing: {top_level}. "
                "Open/save that slot in Afterburner before planning an edit."
            )
        if top_level.is_symlink():
            raise SafetyError(f"Refusing symlinked top-level profile: {top_level}")
        top_original = top_level.read_bytes()
        top_text = top_original.decode("ascii", errors="strict")
        patched_top = patch_profile_contents(top_text)
        writes.append(
            PlannedWrite(
                target=top_level,
                data=ascii_bytes(patched_top),
                label=f"{profile_name} activation metadata",
                expected_before_sha256=sha256_bytes(top_original),
            )
        )
        profile_plans.append(
            {
                "profile": profile_name,
                "core_mhz": core_mhz,
                "memory_mhz": memory_mhz,
                "power_percent": power,
                "thermal_c": thermal,
                "fan_percent": fan_percent,
                "vf_source": source_name,
                "vf_curve": projected_vf_summary,
                "vf_edit": "target offsets only",
            }
        )

    config_bytes = ascii_bytes(changed)
    writes.insert(
        0,
        PlannedWrite(
            target=cfg_path,
            data=config_bytes,
            label="per-GPU profile config",
            expected_before_sha256=sha256_bytes(config_original),
        ),
    )
    plan = {
        "schema_version": 1,
        "mode": "execute" if args.execute else "plan",
        "gpu_id": args.gpu_id.upper(),
        "config": str(cfg_path),
        "source_profile": source_name,
        "uses_example_values": any(
            value is None for value in (args.memory, args.fan, args.power, args.thermal)
        ),
        "vf_curve": vf_summary,
        "profiles": profile_plans,
        "targets": [
            {
                "label": write.label,
                "path": str(write.target),
                "before_sha256": write.expected_before_sha256,
                "planned_sha256": sha256_bytes(write.data),
                "changed": write.expected_before_sha256 != sha256_bytes(write.data),
            }
            for write in writes
        ],
    }
    return plan, writes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan a tiered Afterburner profile edit. No files are changed unless "
            "--execute is supplied."
        )
    )
    parser.add_argument("--config", required=True, help="Exact per-GPU .cfg path.")
    parser.add_argument(
        "--gpu-id",
        required=True,
        help="Exact complete GPU identity (the selected config filename stem).",
    )
    parser.add_argument(
        "--source-profile",
        choices=("Startup",) + PROFILE_NAMES,
        default="Startup",
        help="Exact section supplying the validated VF curve and default core offset.",
    )
    parser.add_argument(
        "--profile",
        action="append",
        choices=PROFILE_NAMES,
        default=None,
        help="Exact target slot; repeat as needed. Default: Profile1..Profile5.",
    )
    parser.add_argument(
        "--core",
        type=float,
        help="Core offset in MHz. Default: validated source-profile value.",
    )
    parser.add_argument(
        "--memory",
        nargs=5,
        type=int,
        default=None,
        metavar=("P1", "P2", "P3", "P4", "P5"),
        help=(
            "Explicit memory offsets in MHz for Profile1..Profile5. Required "
            "for --execute."
        ),
    )
    parser.add_argument(
        "--fan",
        nargs=5,
        type=int,
        default=None,
        metavar=("P1", "P2", "P3", "P4", "P5"),
        help=(
            "Explicit manual fan percentages for Profile1..Profile5. Required "
            "for --execute."
        ),
    )
    parser.add_argument(
        "--power", type=int, help="Explicit power limit percent; required for --execute."
    )
    parser.add_argument(
        "--thermal", type=int, help="Explicit thermal target C; required for --execute."
    )
    parser.add_argument(
        "--acknowledge-source-profile",
        action="store_true",
        help=(
            "Required for --execute: confirms the selected source VF curve and "
            "optional copied core offset were independently reviewed."
        ),
    )
    parser.add_argument(
        "--acknowledge-afterburner-closed",
        action="store_true",
        help=(
            "Required for --execute: confirms MSI Afterburner was fully exited "
            "before the transaction."
        ),
    )
    parser.add_argument(
        "--backup-dir",
        help="Verified backup directory. Default: <profiles>/KingAiBackups.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the planned transaction. Without this flag, plan only.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.profile is None:
        args.profile = list(PROFILE_NAMES)

    try:
        if args.execute:
            missing = [
                name
                for name, value in (
                    ("--memory", args.memory),
                    ("--fan", args.fan),
                    ("--power", args.power),
                    ("--thermal", args.thermal),
                )
                if value is None
            ]
            if missing:
                raise SafetyError(
                    "--execute requires explicit tuning inputs: " + ", ".join(missing)
                )
            if not args.acknowledge_source_profile:
                raise SafetyError(
                    "--execute requires --acknowledge-source-profile after "
                    "reviewing the selected source VF curve and core offset."
                )
            if not args.acknowledge_afterburner_closed:
                raise SafetyError(
                    "--execute requires --acknowledge-afterburner-closed to "
                    "reduce application rewrite races."
                )
        plan, writes = build_plan(args)
        if not args.execute:
            if args.json:
                print(json.dumps(plan, indent=2, sort_keys=True))
            else:
                heading = (
                    "EXAMPLE PLAN ONLY - no files were modified"
                    if plan["uses_example_values"]
                    else "PLAN ONLY - no files were modified"
                )
                print(heading)
                print(f"GPU: {plan['gpu_id']}")
                print(f"Config: {plan['config']}")
                for profile in plan["profiles"]:
                    print(
                        "{profile}: core {core_mhz:+g} MHz, memory "
                        "{memory_mhz:+d} MHz, power {power_percent}%, "
                        "thermal {thermal_c} C, fan {fan_percent}%".format(**profile)
                    )
                if plan["uses_example_values"]:
                    print(
                        "Example values cannot be executed; provide explicit "
                        "--memory, --fan, --power, and --thermal."
                    )
                print(
                    "Execution also requires --acknowledge-source-profile after "
                    "independent source-curve review."
                )
                print("Re-run with --execute only after reviewing this plan.")
            return 0

        config = Path(plan["config"])
        backup_dir = (
            Path(args.backup_dir)
            if args.backup_dir
            else config.parent / "KingAiBackups"
        )
        receipts = atomic_write_transaction(writes, backup_dir=backup_dir)
        result = dict(plan)
        result["result"] = "applied"
        result["receipts"] = [
            {
                "target": str(receipt.target),
                "backup": str(receipt.backup) if receipt.backup else None,
                "before_sha256": receipt.before_sha256,
                "after_sha256": receipt.after_sha256,
            }
            for receipt in receipts
        ]
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print("APPLIED - all replacements and backups verified")
            for receipt in receipts:
                print(f"{receipt.target} -> {receipt.after_sha256}")
        return 0
    except (OSError, SafetyError, TransactionError, UnicodeError) as exc:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "result": "error",
                        "error": str(exc),
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
