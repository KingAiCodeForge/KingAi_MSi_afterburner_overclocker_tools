#!/usr/bin/env python3
"""Plan or execute one guarded Afterburner profile-slot edit."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from profile_safety import (
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
    validate_bound,
    validate_profile_name,
    validate_settings,
    validate_vf_hex,
)


def _read_int(block: str, key: str, default: int) -> int:
    value = get_ini_value(block, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise SafetyError(f"{key} must be an integer, got {value!r}.") from exc


def _load_vf_curve(args: argparse.Namespace, text: str) -> tuple[str | None, str | None]:
    if args.copy_startup_vf:
        startup = get_section(text, "Startup")
        vf_hex = get_ini_value(startup, "VFCurve")
        if vf_hex is None:
            raise SafetyError("[Startup] does not contain VFCurve.")
        return vf_hex, "Startup"
    if args.vf_curve is None:
        return None, None

    possible_path = Path(args.vf_curve)
    if possible_path.is_file():
        vf_hex = possible_path.read_text(encoding="ascii").strip()
        source = str(possible_path.resolve())
    else:
        vf_hex = args.vf_curve.strip()
        source = "command-line"
    if not re.fullmatch(r"[0-9A-Fa-f\s]+", vf_hex):
        raise SafetyError("--vf-curve must be hexadecimal or an existing .hex file.")
    return "".join(vf_hex.split()), source


def build_plan(args: argparse.Namespace) -> tuple[dict, list[PlannedWrite]]:
    profile = validate_profile_name(args.profile)
    cfg_path = resolve_config(gpu_id=args.gpu_id, config_path=args.config)
    config_original = cfg_path.read_bytes()
    text = config_original.decode("ascii", errors="strict")
    block = get_section(text, profile)
    vf_hex, vf_source = _load_vf_curve(args, text)

    core_mhz = args.core if args.core is not None else _read_int(block, "CoreClkBoost", 0) / 1000
    memory_mhz = args.mem if args.mem is not None else _read_int(block, "MemClkBoost", 0) / 1000
    power = args.power if args.power is not None else _read_int(block, "PowerLimit", 100)
    thermal = args.thermal if args.thermal is not None else _read_int(block, "ThermalLimit", 83)
    fan_mode = _read_int(block, "FanMode", 0)
    fan = args.fan if args.fan is not None else _read_int(block, "FanSpeed", 0)
    if args.fan is not None:
        fan_mode = 1

    validate_settings(
        {
            "core_mhz": core_mhz,
            "memory_mhz": memory_mhz,
            "power_percent": power,
            "thermal_c": thermal,
        }
    )
    if fan_mode not in (0, 1):
        raise SafetyError(f"FanMode must be 0 or 1, got {fan_mode}.")
    if fan_mode == 1:
        validate_bound("fan_percent", fan)
    if int(core_mhz * 1000) != core_mhz * 1000:
        raise SafetyError("Core offset must resolve to a whole number of kHz.")
    if int(memory_mhz * 1000) != memory_mhz * 1000:
        raise SafetyError("Memory offset must resolve to a whole number of kHz.")

    existing_vf = get_ini_value(block, "VFCurve")
    final_vf = vf_hex if vf_hex is not None else existing_vf
    if vf_hex is not None:
        reference_vf = existing_vf
        if reference_vf is None:
            reference_vf = get_ini_value(get_section(text, "Startup"), "VFCurve")
        if reference_vf is None:
            raise SafetyError(
                "Cannot validate a replacement VFCurve without a curve from "
                "the selected config."
            )
        vf_summary = validate_vf_hex(vf_hex, reference_hex=reference_vf)
    else:
        vf_summary = validate_vf_hex(final_vf) if final_vf else None

    if args.core is not None:
        block = set_ini_value(block, "CoreClkBoost", int(core_mhz * 1000))
    if args.mem is not None:
        block = set_ini_value(block, "MemClkBoost", int(memory_mhz * 1000))
    if args.power is not None:
        block = set_ini_value(block, "PowerLimit", power)
    if args.thermal is not None:
        block = set_ini_value(block, "ThermalLimit", thermal)
    if args.fan is not None:
        block = set_ini_value(block, "FanMode", 1)
        block = set_ini_value(block, "FanSpeed", fan)
    if vf_hex is not None:
        block = set_ini_value(block, "VFCurve", vf_hex)

    changed = replace_section(text, profile, block)
    config_write = PlannedWrite(
        target=cfg_path,
        data=ascii_bytes(changed),
        label="per-GPU profile config",
        expected_before_sha256=sha256_bytes(config_original),
    )

    top_level = cfg_path.parent / f"{profile}.cfg"
    if not top_level.is_file():
        raise SafetyError(f"Exact top-level profile target is missing: {top_level}.")
    if top_level.is_symlink():
        raise SafetyError(f"Refusing symlinked top-level profile: {top_level}")
    top_original = top_level.read_bytes()
    patched_top = patch_profile_contents(top_original.decode("ascii", errors="strict"))
    top_write = PlannedWrite(
        target=top_level,
        data=ascii_bytes(patched_top),
        label=f"{profile} activation metadata",
        expected_before_sha256=sha256_bytes(top_original),
    )
    writes = [config_write, top_write]
    plan = {
        "schema_version": 1,
        "mode": "execute" if args.execute else "plan",
        "gpu_id": args.gpu_id.upper(),
        "config": str(cfg_path),
        "profile": profile,
        "settings": {
            "core_mhz": core_mhz,
            "memory_mhz": memory_mhz,
            "power_percent": power,
            "thermal_c": thermal,
            "fan_mode": fan_mode,
            "fan_percent": fan if fan_mode == 1 else None,
        },
        "vf_source": vf_source,
        "vf_curve": vf_summary,
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
            "Plan one exact Afterburner profile edit. No files are changed unless "
            "--execute is supplied."
        )
    )
    parser.add_argument("--config", required=True, help="Exact per-GPU .cfg path.")
    parser.add_argument("--gpu-id", required=True, help="Exact complete GPU identity.")
    parser.add_argument("--profile", required=True, help="Profile slot 1..5.")
    parser.add_argument("--core", type=float, help="Core offset MHz.")
    parser.add_argument("--mem", type=float, help="Memory offset MHz.")
    parser.add_argument("--power", type=int, help="Power limit percent.")
    parser.add_argument("--thermal", type=int, help="Thermal target C.")
    parser.add_argument("--fan", type=int, help="Manual fan percent.")
    vf_group = parser.add_mutually_exclusive_group()
    vf_group.add_argument(
        "--vf-curve",
        help="Validated VFCurve hex string or existing .hex file.",
    )
    vf_group.add_argument(
        "--copy-startup-vf",
        action="store_true",
        help="Copy the validated VFCurve from [Startup].",
    )
    parser.add_argument(
        "--backup-dir",
        help="Verified backup directory. Default: <profiles>/KingAiBackups.",
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
        "--execute",
        action="store_true",
        help="Perform the transaction. Without this flag, plan only.",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not any(
        value is not None
        for value in (args.core, args.mem, args.power, args.thermal, args.fan, args.vf_curve)
    ) and not args.copy_startup_vf:
        parser.error("Specify at least one setting or VF-curve source.")

    try:
        if args.execute and not args.acknowledge_afterburner_closed:
            raise SafetyError(
                "--execute requires --acknowledge-afterburner-closed to "
                "reduce application rewrite races."
            )
        plan, writes = build_plan(args)
        if not args.execute:
            if args.json:
                print(json.dumps(plan, indent=2, sort_keys=True))
            else:
                print("PLAN ONLY - no files were modified")
                print(f"GPU: {plan['gpu_id']}")
                print(f"Profile: {plan['profile']}")
                for key, value in plan["settings"].items():
                    print(f"{key}: {value}")
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
                "target": str(item.target),
                "backup": str(item.backup) if item.backup else None,
                "before_sha256": item.before_sha256,
                "after_sha256": item.after_sha256,
            }
            for item in receipts
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
                    {"schema_version": 1, "result": "error", "error": str(exc)},
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
