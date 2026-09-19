#!/usr/bin/env python3
"""Generate a guarded MSI Afterburner profile V/F curve.

The encoder requires an existing curve as its structural reference.  It
preserves the 12-byte header, voltage grid, base-frequency values, fixed slot
buffer, and opaque footer.  Generation changes only the per-point offset.
Nothing is installed into Afterburner or applied to a GPU.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

from profile_safety import (
    PlannedWrite,
    SafetyError,
    atomic_write_transaction,
    resolve_config,
    sha256_bytes,
    validate_bound,
    validate_vf_hex,
)
from vf_curve_format import (
    VFCurve,
    VFCurveFormatError,
    VFPoint,
    decode_vf_curve,
    encode_vf_curve,
)


SECTIONS = ("Startup", "Profile1", "Profile2", "Profile3", "Profile4", "Profile5")


def ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def find_config(custom_path: str, gpu_id: str) -> str:
    """Resolve one exact reference config and verify its GPU identity."""

    return str(resolve_config(config_path=custom_path, gpu_id=gpu_id))


def get_ini_value(text: str, section: str, key: str) -> str | None:
    """Get a value from one exact INI section."""

    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped[1:-1] == section
        elif in_section and stripped.startswith(key + "="):
            value = stripped.split("=", 1)[1].strip()
            return value if value else None
    return None


def get_reference_data(
    config_path: str,
    requested_section: str | None = None,
) -> tuple[VFCurve, str, str]:
    """Load and validate a reference curve without probing other files."""

    with open(config_path, encoding="ascii", errors="replace") as handle:
        text = handle.read()

    sections: Iterable[str]
    if requested_section is not None:
        if requested_section not in SECTIONS:
            raise SafetyError(
                "--section must be Startup or Profile1 through Profile5."
            )
        sections = (requested_section,)
    else:
        sections = SECTIONS

    for section in sections:
        vf_hex = get_ini_value(text, section, "VFCurve")
        if not vf_hex:
            continue
        validate_vf_hex(vf_hex)
        try:
            curve = decode_vf_curve(vf_hex)
        except VFCurveFormatError as exc:
            raise SafetyError(str(exc)) from exc
        print(
            f"{ts()} Reference curve from [{section}]: "
            f"{curve.point_count} points, footer: {len(curve.footer)} bytes"
        )
        return curve, vf_hex, section

    scope = f"[{requested_section}]" if requested_section else "known profile sections"
    raise SafetyError(
        f"No valid VFCurve found in {scope} of {config_path}. "
        "Save a baseline curve in Afterburner first."
    )


def make_undervolt_curve(
    reference_points: Iterable[VFPoint],
    target_voltage_mv: float,
    target_effective_frequency_mhz: float,
) -> list[VFPoint]:
    """Flatten effective frequency at and above one reference voltage."""

    generated = []
    for point in reference_points:
        offset = point.offset_mhz
        if point.voltage_mv >= target_voltage_mv:
            offset = target_effective_frequency_mhz - point.base_frequency_mhz
        generated.append(
            VFPoint(
                index=point.index,
                voltage_mv=point.voltage_mv,
                base_frequency_mhz=point.base_frequency_mhz,
                offset_mhz=offset,
            )
        )
    return generated


def make_offset_curve(
    reference_points: Iterable[VFPoint],
    offset_delta_mhz: float,
) -> list[VFPoint]:
    """Add one delta to each stored offset while preserving base values."""

    return [
        VFPoint(
            index=point.index,
            voltage_mv=point.voltage_mv,
            base_frequency_mhz=point.base_frequency_mhz,
            offset_mhz=point.offset_mhz + offset_delta_mhz,
        )
        for point in reference_points
    ]


def _normalized_fieldnames(fieldnames: list[str] | None) -> list[str]:
    if not fieldnames:
        raise SafetyError("CSV is missing a header row.")
    return [name.strip().lower().replace(" ", "_") for name in fieldnames]


def _field(row: dict[str, str | None], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return None


def _finite_float(value: str, label: str, row_number: int) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise SafetyError(f"CSV row {row_number}: {label} must be numeric.") from exc
    if not math.isfinite(parsed):
        raise SafetyError(f"CSV row {row_number}: {label} must be finite.")
    return parsed


def load_csv_curve(
    csv_path: str,
    reference_points: Iterable[VFPoint],
) -> list[VFPoint]:
    """Import offsets/effective values while preserving voltage and base fields.

    Canonical columns are ``voltage_mV``, ``base_frequency_MHz``,
    ``offset_MHz``, and ``effective_frequency_MHz``.  The older
    ``adjustment_MHz`` name is accepted as an offset alias.  The older
    ``frequency_MHz`` name is accepted as an effective-frequency alias because
    it can be checked against the immutable reference base.
    """

    path = Path(csv_path)
    if not path.is_file():
        raise SafetyError(f"CSV file not found: {path}")

    reference = tuple(reference_points)
    by_voltage = {round(point.voltage_mv, 2): point for point in reference}
    replacements: dict[float, VFPoint] = {}

    with path.open(newline="", encoding="utf-8-sig") as handle:
        data_lines = (
            line
            for line in handle
            if line.strip() and not line.lstrip().startswith("#")
        )
        reader = csv.DictReader(data_lines)
        reader.fieldnames = _normalized_fieldnames(reader.fieldnames)

        for row_number, row in enumerate(reader, start=2):
            voltage_text = _field(row, "voltage_mv", "voltage")
            if voltage_text is None:
                raise SafetyError(f"CSV row {row_number}: voltage_mV is required.")
            voltage = _finite_float(voltage_text, "voltage_mV", row_number)
            reference_point = by_voltage.get(round(voltage, 2))
            if reference_point is None or abs(reference_point.voltage_mv - voltage) > 0.01:
                raise SafetyError(
                    f"CSV row {row_number}: voltage {voltage} mV does not "
                    "match the selected reference grid."
                )
            voltage_key = round(reference_point.voltage_mv, 2)
            if voltage_key in replacements:
                raise SafetyError(
                    f"CSV row {row_number}: duplicate voltage {voltage} mV."
                )

            index_text = _field(row, "index")
            if index_text is not None:
                index = _finite_float(index_text, "index", row_number)
                if not index.is_integer() or int(index) != reference_point.index:
                    raise SafetyError(
                        f"CSV row {row_number}: index does not match reference "
                        f"slot {reference_point.index}."
                    )

            base_text = _field(
                row,
                "base_frequency_mhz",
                "base_mhz",
                "base_frequency",
                "base",
            )
            if base_text is not None:
                base = _finite_float(base_text, "base_frequency_MHz", row_number)
                if abs(base - reference_point.base_frequency_mhz) > 0.01:
                    raise SafetyError(
                        f"CSV row {row_number}: base frequency is immutable; "
                        f"expected {reference_point.base_frequency_mhz} MHz."
                    )

            offset_text = _field(
                row,
                "offset_mhz",
                "adjustment_mhz",
                "offset",
                "adjustment",
            )
            effective_text = _field(
                row,
                "effective_frequency_mhz",
                "effective_mhz",
                "effective_frequency",
                "frequency_mhz",
                "frequency",
            )
            if offset_text is None and effective_text is None:
                raise SafetyError(
                    f"CSV row {row_number}: provide offset_MHz or "
                    "effective_frequency_MHz."
                )

            offset = None
            if offset_text is not None:
                offset = _finite_float(offset_text, "offset_MHz", row_number)
                validate_bound("vf_adjustment_mhz", offset)

            if effective_text is not None:
                effective = _finite_float(
                    effective_text,
                    "effective_frequency_MHz",
                    row_number,
                )
                validate_bound("vf_frequency_mhz", effective)
                derived_offset = effective - reference_point.base_frequency_mhz
                validate_bound("vf_adjustment_mhz", derived_offset)
                if offset is not None and abs(offset - derived_offset) > 0.05:
                    raise SafetyError(
                        f"CSV row {row_number}: offset and effective frequency "
                        "disagree with the reference base."
                    )
                offset = derived_offset

            assert offset is not None
            replacements[voltage_key] = VFPoint(
                index=reference_point.index,
                voltage_mv=reference_point.voltage_mv,
                base_frequency_mhz=reference_point.base_frequency_mhz,
                offset_mhz=offset,
            )

    if not replacements:
        raise SafetyError("CSV contains no curve rows.")

    print(
        f"{ts()} CSV: {len(replacements)} entries matched "
        f"{len(reference)} reference points"
    )
    return [
        replacements.get(round(point.voltage_mv, 2), point)
        for point in reference
    ]


def preview_curve(points: Iterable[VFPoint], label: str = "Generated") -> None:
    """Print effective-frequency inflections without inferring user intent."""

    selected = tuple(points)
    print(f"{ts()} === {label} Curve ({len(selected)} points) ===")
    print(
        f"{ts()}   {'Idx':>5}  {'Voltage':>10}  {'Base':>10}  "
        f"{'Offset':>10}  {'Effective':>10}"
    )

    previous_effective = None
    for point in selected:
        effective = point.effective_frequency_mhz
        is_inflection = (
            previous_effective is None
            or abs(effective - previous_effective) > 0.5
        )
        is_edge = point.index == 0 or point.index >= len(selected) - 2
        if is_inflection or is_edge:
            print(
                f"{ts()}   [{point.index:>3}]  "
                f"{point.voltage_mv:>8.1f} mV  "
                f"{point.base_frequency_mhz:>8.1f} MHz  "
                f"{point.offset_mhz:>+8.1f} MHz  "
                f"{effective:>8.1f} MHz"
            )
        previous_effective = effective

    voltages = [point.voltage_mv for point in selected]
    effective_frequencies = [
        point.effective_frequency_mhz for point in selected
    ]
    print()
    print(
        f"{ts()}   Range: {min(voltages):.1f}-{max(voltages):.1f} mV | "
        f"Peak effective: {max(effective_frequencies):.1f} MHz"
    )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Encode VF curves for MSI Afterburner .cfg files."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--undervolt",
        "-u",
        nargs=2,
        metavar=("VOLTAGE_MV", "EFFECTIVE_FREQ_MHZ"),
        type=float,
        help=(
            "Set the effective frequency at VOLTAGE_MV and flatten all higher "
            "voltage points to it."
        ),
    )
    mode.add_argument(
        "--offset",
        "-f",
        type=float,
        metavar="MHZ",
        help="Add a flat delta to every stored per-point offset.",
    )
    mode.add_argument(
        "--from-csv",
        "-i",
        metavar="CSV_PATH",
        help=(
            "Import offsets/effective frequencies from CSV; voltage and base "
            "must match the reference."
        ),
    )

    parser.add_argument(
        "--config",
        "-c",
        required=True,
        help="Exact path to the reference per-GPU .cfg file.",
    )
    parser.add_argument(
        "--gpu-id",
        required=True,
        help="Exact complete GPU identity (the config filename stem).",
    )
    parser.add_argument(
        "--section",
        "-s",
        default="Startup",
        help="Exact reference section (default: Startup).",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Write a hex artifact instead of printing it.",
    )
    parser.add_argument(
        "--preview",
        "-p",
        action="store_true",
        help="Print a human-readable generated-curve table.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write --output. Without this flag an output request is plan-only.",
    )
    parser.add_argument(
        "--backup-dir",
        help="Verified backup directory for an existing output file.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing --output (also requires --execute).",
    )
    args = parser.parse_args()
    if args.overwrite and not args.output:
        parser.error("--overwrite requires --output.")

    try:
        config_path = find_config(args.config, args.gpu_id)
        reference, reference_hex, _section = get_reference_data(
            config_path,
            args.section,
        )
    except SafetyError as exc:
        parser.error(str(exc))

    print(f"{ts()} Reference config: {os.path.basename(config_path)}")
    print()

    try:
        if args.undervolt:
            target_voltage, target_effective = args.undervolt
            validate_bound("vf_voltage_mv", target_voltage)
            validate_bound("vf_frequency_mhz", target_effective)
            matching_voltage = next(
                (
                    point.voltage_mv
                    for point in reference.points
                    if abs(point.voltage_mv - target_voltage) <= 0.01
                ),
                None,
            )
            if matching_voltage is None:
                raise SafetyError(
                    "--undervolt voltage must exactly match a voltage point in "
                    "the selected reference curve."
                )
            generated = make_undervolt_curve(
                reference.points,
                matching_voltage,
                target_effective,
            )
            print(
                f"{ts()} Mode: effective-frequency plateau at "
                f"{matching_voltage:.1f} mV / {target_effective:.0f} MHz"
            )
        elif args.offset is not None:
            validate_bound("vf_adjustment_mhz", args.offset)
            generated = make_offset_curve(reference.points, args.offset)
            print(f"{ts()} Mode: add {args.offset:+.0f} MHz to all offsets")
        else:
            generated = load_csv_curve(args.from_csv, reference.points)
            print(f"{ts()} Mode: CSV import - {args.from_csv}")

        blob = encode_vf_curve(reference, generated)
        validate_vf_hex(blob, reference_hex=reference_hex)
        decoded = decode_vf_curve(blob)
    except (SafetyError, VFCurveFormatError) as exc:
        parser.error(str(exc))

    # Exact byte re-encoding catches header or point-field shifts before output.
    if encode_vf_curve(decoded, decoded.points) != blob:
        parser.error("Generated curve failed exact byte-roundtrip verification.")
    print(
        f"{ts()} Generated VFCurve: {len(blob)} hex chars "
        f"({len(blob) // 2} bytes)"
    )
    print(f"{ts()} Roundtrip verification: OK ({len(generated)} points match)")

    if args.preview:
        print()
        preview_curve(generated)

    if args.output:
        output = Path(args.output).expanduser().resolve(strict=False)
        original = output.read_bytes() if output.exists() else None
        if original is not None and not args.overwrite:
            parser.error(
                f"Output already exists: {output}. Use --overwrite with "
                "--execute only after reviewing the replacement."
            )
        if not args.execute:
            print(f"{ts()} PLAN ONLY - would write validated hex to: {output}")
            print(f"{ts()} Re-run with --execute after reviewing the curve.")
        else:
            backup_dir = (
                Path(args.backup_dir)
                if args.backup_dir
                else output.parent / "KingAiBackups"
            )
            atomic_write_transaction(
                [
                    PlannedWrite(
                        output,
                        blob.encode("ascii"),
                        "VF hex export",
                        sha256_bytes(original) if original is not None else None,
                    )
                ],
                backup_dir=backup_dir,
            )
            print(f"{ts()} Written and verified: {output}")
    else:
        print()
        print(f"{ts()} === VFCurve Hex ===")
        print(blob)

    print()
    print(f"{ts()} Done.")


if __name__ == "__main__":
    main()
