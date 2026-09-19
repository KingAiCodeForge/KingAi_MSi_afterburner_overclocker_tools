"""Safety primitives for offline MSI Afterburner profile-file tooling.

This module does not communicate with a GPU or MSI Afterburner.  It only
validates and transactionally edits explicitly selected configuration files.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import struct
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

from vf_curve_format import (
    VF_BUFFER_BYTES,
    VF_HEADER_BYTES,
    VF_POINT_BYTES,
    VFCurveFormatError,
    decode_vf_curve,
)


GPU_ID_RE = re.compile(
    r"^VEN_[0-9A-F]{4}&DEV_[0-9A-F]{4}&SUBSYS_[0-9A-F]{8}"
    r"&REV_[0-9A-F]{2}&BUS_[0-9]+&DEV_[0-9]+&FN_[0-9]+$",
    re.IGNORECASE,
)

SAFE_BOUNDS = {
    "core_mhz": (-500, 500),
    "memory_mhz": (-2000, 2000),
    "power_percent": (50, 120),
    "thermal_c": (60, 90),
    "fan_percent": (20, 100),
    "vf_voltage_mv": (400.0, 1300.0),
    "vf_base_frequency_mhz": (100.0, 4000.0),
    "vf_frequency_mhz": (100.0, 4000.0),
    "vf_adjustment_mhz": (-1000.0, 1000.0),
}

PROFILE_NAMES = tuple(f"Profile{number}" for number in range(1, 6))
SUPPORTED_VF_VERSIONS = frozenset({0x20000})
MAX_VF_FOOTER_BYTES = 64 * 1024
VF_EFFECTIVE_DIP_TOLERANCE_MHZ = 15.1
SECTION_RE_TEMPLATE = r"(?ms)(^\[{section}\][ \t]*\r?\n)(.*?)(?=^\[|\Z)"


class SafetyError(ValueError):
    """Raised when an input fails a software safety invariant."""


class TransactionError(RuntimeError):
    """Raised when a verified file transaction cannot be completed."""


@dataclass(frozen=True)
class PlannedWrite:
    """One exact target and its complete replacement bytes."""

    target: Path
    data: bytes
    label: str
    expected_before_sha256: str | None


@dataclass(frozen=True)
class WriteReceipt:
    """Evidence returned after a successful verified replacement."""

    target: Path
    backup: Path | None
    before_sha256: str | None
    after_sha256: str


def normalize_gpu_id(value: str) -> str:
    gpu_id = value.strip().upper()
    if not GPU_ID_RE.fullmatch(gpu_id):
        raise SafetyError(
            "GPU identity must be the complete Afterburner filename stem, for "
            "example VEN_10DE&DEV_0000&SUBSYS_00000000&REV_00&BUS_0&DEV_0&FN_0"
        )
    return gpu_id


def config_gpu_id(path: os.PathLike[str] | str) -> str:
    candidate = Path(path)
    if candidate.suffix.lower() != ".cfg":
        raise SafetyError(f"Expected a .cfg file, got: {candidate}")
    return normalize_gpu_id(candidate.stem)


def resolve_config(
    *,
    gpu_id: str,
    config_path: os.PathLike[str] | str | None = None,
    profiles_dir: os.PathLike[str] | str | None = None,
) -> Path:
    """Resolve one exact GPU config without scanning or size-based guessing."""

    expected = normalize_gpu_id(gpu_id)
    if config_path is None:
        if profiles_dir is None:
            raise SafetyError("Specify --config or --profiles-dir with --gpu-id.")
        candidate = Path(profiles_dir) / f"{expected}.cfg"
    else:
        candidate = Path(config_path)

    candidate = candidate.expanduser()
    if candidate.is_symlink():
        raise SafetyError(f"Refusing symlinked config: {candidate}")
    candidate = candidate.resolve(strict=False)
    if not candidate.is_file():
        raise SafetyError(f"Config not found: {candidate}")

    actual = config_gpu_id(candidate)
    if actual != expected:
        raise SafetyError(
            f"GPU identity mismatch: expected {expected}, selected {actual}."
        )
    return candidate


def validate_profile_name(value: str | int) -> str:
    raw = str(value)
    if raw.isdigit():
        raw = f"Profile{raw}"
    if raw not in PROFILE_NAMES:
        raise SafetyError("Profile must be one of Profile1 through Profile5.")
    return raw


def validate_bound(name: str, value: int | float) -> int | float:
    if name not in SAFE_BOUNDS:
        raise KeyError(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SafetyError(f"{name} must be numeric.")
    if not math.isfinite(float(value)):
        raise SafetyError(f"{name} must be finite.")
    low, high = SAFE_BOUNDS[name]
    if value < low or value > high:
        raise SafetyError(f"{name}={value} is outside the guardrail [{low}, {high}].")
    return value


def validate_settings(values: Mapping[str, int | float | None]) -> None:
    for name, value in values.items():
        if value is not None:
            validate_bound(name, value)


def validate_vf_hex(
    hex_value: str,
    *,
    reference_hex: str | None = None,
) -> dict[str, int | float]:
    """Validate the populated points in an Afterburner VFCurve blob."""

    compact = "".join(hex_value.split())
    try:
        curve = decode_vf_curve(compact)
    except VFCurveFormatError as exc:
        raise SafetyError(str(exc)) from exc

    raw = bytes.fromhex(compact)
    version = curve.version
    point_count = curve.point_count
    if version not in SUPPORTED_VF_VERSIONS:
        supported = ", ".join(f"0x{item:X}" for item in SUPPORTED_VF_VERSIONS)
        raise SafetyError(
            f"Unsupported VFCurve version 0x{version:X}; supported: {supported}."
        )
    if len(raw) > VF_BUFFER_BYTES + MAX_VF_FOOTER_BYTES:
        raise SafetyError("VFCurve footer is implausibly large.")

    previous_voltage = None
    previous_effective_frequency = None
    min_voltage = math.inf
    max_voltage = -math.inf
    max_base_frequency = -math.inf
    max_effective_frequency = -math.inf
    min_offset = math.inf
    max_offset = -math.inf
    for point in curve.points:
        index = point.index
        voltage = point.voltage_mv
        base_frequency = point.base_frequency_mhz
        offset = point.offset_mhz
        effective_frequency = point.effective_frequency_mhz
        validate_bound("vf_voltage_mv", voltage)
        validate_bound("vf_base_frequency_mhz", base_frequency)
        validate_bound("vf_adjustment_mhz", offset)
        validate_bound("vf_frequency_mhz", effective_frequency)
        if previous_voltage is not None and voltage <= previous_voltage:
            raise SafetyError(
                f"VFCurve voltage must strictly increase at point {index}: "
                f"{previous_voltage} -> {voltage} mV."
            )
        if (
            previous_effective_frequency is not None
            and effective_frequency + VF_EFFECTIVE_DIP_TOLERANCE_MHZ
            < previous_effective_frequency
        ):
            raise SafetyError(
                f"VFCurve effective frequency decreases by more than "
                f"{VF_EFFECTIVE_DIP_TOLERANCE_MHZ:.1f} MHz at point {index}: "
                f"{previous_effective_frequency} -> {effective_frequency} MHz."
            )
        previous_voltage = voltage
        previous_effective_frequency = effective_frequency
        min_voltage = min(min_voltage, voltage)
        max_voltage = max(max_voltage, voltage)
        max_base_frequency = max(max_base_frequency, base_frequency)
        max_effective_frequency = max(
            max_effective_frequency,
            effective_frequency,
        )
        min_offset = min(min_offset, offset)
        max_offset = max(max_offset, offset)

    footer = curve.footer
    if reference_hex is not None:
        reference_compact = "".join(reference_hex.split())
        reference_summary = validate_vf_hex(reference_compact)
        reference_raw = bytes.fromhex(reference_compact)
        reference_curve = decode_vf_curve(reference_compact)
        if len(raw) != len(reference_raw):
            raise SafetyError(
                "Replacement VFCurve length does not match the selected "
                "config's reference curve."
            )
        if curve.unused_slots != reference_curve.unused_slots:
            raise SafetyError(
                "Replacement VFCurve unused-slot bytes do not match the "
                "selected config's reference curve."
            )
        if footer != reference_raw[VF_BUFFER_BYTES:]:
            raise SafetyError(
                "Replacement VFCurve footer does not match the selected "
                "config's reference curve."
            )
        if reference_summary["version"] != version:
            raise SafetyError("Replacement and reference VFCurve versions differ.")
        if reference_summary["point_count"] != point_count:
            raise SafetyError(
                "Replacement and reference VFCurve point counts differ."
            )
        if reference_summary["header_reserved"] != curve.header_reserved:
            raise SafetyError(
                "Replacement and reference VFCurve reserved/flags header differ."
            )
        for index in range(point_count):
            replacement_voltage = struct.unpack_from(
                "<f",
                raw,
                VF_HEADER_BYTES + index * VF_POINT_BYTES,
            )[0]
            reference_voltage = struct.unpack_from(
                "<f",
                reference_raw,
                VF_HEADER_BYTES + index * VF_POINT_BYTES,
            )[0]
            if abs(replacement_voltage - reference_voltage) > 0.01:
                raise SafetyError(
                    f"Replacement voltage grid differs at point {index}: "
                    f"{replacement_voltage} vs {reference_voltage} mV."
                )
            replacement_base = struct.unpack_from(
                "<f",
                raw,
                VF_HEADER_BYTES + index * VF_POINT_BYTES + 4,
            )[0]
            reference_base = struct.unpack_from(
                "<f",
                reference_raw,
                VF_HEADER_BYTES + index * VF_POINT_BYTES + 4,
            )[0]
            if replacement_base != reference_base:
                raise SafetyError(
                    f"Replacement base frequency differs at point {index}: "
                    f"{replacement_base} vs {reference_base} MHz."
                )

    return {
        "version": version,
        "point_count": point_count,
        "header_reserved": curve.header_reserved,
        "min_voltage_mv": min_voltage,
        "max_voltage_mv": max_voltage,
        "max_base_frequency_mhz": max_base_frequency,
        "max_effective_frequency_mhz": max_effective_frequency,
        # Backward-compatible summary key; now explicitly means effective.
        "max_frequency_mhz": max_effective_frequency,
        "min_offset_mhz": min_offset,
        "max_offset_mhz": max_offset,
        "footer_bytes": len(footer),
    }


def get_section(text: str, section: str) -> str:
    matches = list(
        re.finditer(
            SECTION_RE_TEMPLATE.format(section=re.escape(section)),
            text,
        )
    )
    if not matches:
        raise SafetyError(f"Missing required [{section}] section.")
    if len(matches) != 1:
        raise SafetyError(f"Expected one [{section}] section, found {len(matches)}.")
    return matches[0].group(2)


def replace_section(text: str, section: str, new_body: str) -> str:
    pattern = re.compile(
        SECTION_RE_TEMPLATE.format(section=re.escape(section)),
    )
    matches = list(pattern.finditer(text))
    if not matches:
        raise SafetyError(f"Missing required [{section}] section.")
    if len(matches) != 1:
        raise SafetyError(f"Expected one [{section}] section, found {len(matches)}.")
    match = matches[0]
    newline = "\r\n" if "\r\n" in match.group(0) else "\n"
    body = new_body.rstrip("\r\n") + newline
    return text[: match.start()] + match.group(1) + body + text[match.end() :]


def get_ini_value(block: str, key: str) -> str | None:
    matches = list(
        re.finditer(rf"(?m)^{re.escape(key)}=([^\r\n]*)(?:\r?)$", block)
    )
    if len(matches) > 1:
        raise SafetyError(f"Expected at most one {key} value, found {len(matches)}.")
    return matches[0].group(1).strip() if matches else None


def set_ini_value(block: str, key: str, value: str | int) -> str:
    replacement = f"{key}={value}"
    pattern = re.compile(rf"(?m)^{re.escape(key)}=[^\r\n]*(\r?)$")
    matches = list(pattern.finditer(block))
    if len(matches) > 1:
        raise SafetyError(f"Expected at most one {key} value, found {len(matches)}.")
    if matches:
        return pattern.sub(
            lambda match: replacement + match.group(1),
            block,
            count=1,
        )
    newline = "\r\n" if "\r\n" in block else "\n"
    format_match = re.search(r"(?m)^Format=[^\r\n]*(\r?\n|$)", block)
    if format_match:
        prefix = "" if format_match.group(1) else newline
        insert_at = format_match.end()
        return (
            block[:insert_at]
            + prefix
            + replacement
            + newline
            + block[insert_at:]
        )
    return block.rstrip("\r\n") + newline + replacement + newline


def patch_profile_contents(text: str) -> str:
    pattern = re.compile(r"(?m)^ProfileContents=\d+[ \t]*(\r?)$")
    if pattern.search(text):
        return pattern.sub(
            lambda match: "ProfileContents=3" + match.group(1),
            text,
            count=1,
        )
    header = re.search(r"(?m)^\[Settings\][ \t]*(\r?\n|$)", text)
    if not header:
        raise SafetyError("Top-level profile is missing [Settings].")
    newline = "\r\n" if header.group(1) == "\r\n" or "\r\n" in text else "\n"
    prefix = "" if header.group(1) else newline
    return (
        text[: header.end()]
        + prefix
        + "ProfileContents=3"
        + newline
        + text[header.end() :]
    )


def ascii_bytes(text: str) -> bytes:
    try:
        return text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SafetyError("Afterburner config output must be ASCII.") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: os.PathLike[str] | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_temp(target: Path, data: bytes) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{target.name}.kingai-",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            os.chmod(temp_path, stat.S_IMODE(target.stat().st_mode))
        if sha256_file(temp_path) != sha256_bytes(data):
            raise TransactionError(f"Temporary-file hash mismatch for {target}.")
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _write_verified_backup(
    target: Path,
    original: bytes,
    backup_dir: Path,
    stamp: str,
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    suffix = sha256_bytes(original)[:12]
    backup = backup_dir / f"{target.name}.{stamp}.{suffix}.bak"
    counter = 1
    while backup.exists():
        backup = backup_dir / f"{target.name}.{stamp}.{suffix}.{counter}.bak"
        counter += 1

    with backup.open("xb") as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    if sha256_file(backup) != sha256_bytes(original):
        backup.unlink(missing_ok=True)
        raise TransactionError(f"Backup hash mismatch for {target}.")
    return backup


def atomic_write_transaction(
    writes: Iterable[PlannedWrite],
    *,
    backup_dir: os.PathLike[str] | str,
    replacer: Callable[[str, str], None] = os.replace,
) -> list[WriteReceipt]:
    """Prepare, back up, atomically replace, verify, and roll back as one unit."""

    selected = list(writes)
    if not selected:
        return []

    seen: set[Path] = set()
    originals: dict[Path, bytes | None] = {}
    temporary: dict[Path, Path] = {}
    backups: dict[Path, Path | None] = {}
    planned_data: dict[Path, bytes] = {}
    applied: list[Path] = []
    backup_root = Path(backup_dir).expanduser()
    if backup_root.is_symlink():
        raise TransactionError(f"Refusing symlinked backup directory: {backup_root}")
    backup_root = backup_root.resolve(strict=False)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    try:
        for write in selected:
            raw_target = write.target.expanduser()
            if raw_target.is_symlink():
                raise TransactionError(
                    f"Refusing symlinked transaction target: {raw_target}"
                )
            target = raw_target.resolve(strict=False)
            if target in seen:
                raise TransactionError(f"Duplicate transaction target: {target}")
            seen.add(target)
            if not target.parent.is_dir():
                raise TransactionError(
                    f"Target directory does not exist: {target.parent}"
                )

            original = target.read_bytes() if target.exists() else None
            actual_before = sha256_bytes(original) if original is not None else None
            if actual_before != write.expected_before_sha256:
                raise TransactionError(
                    f"Target changed after planning: {target}; expected "
                    f"{write.expected_before_sha256 or 'missing'}, found "
                    f"{actual_before or 'missing'}."
                )
            originals[target] = original
            planned_data[target] = write.data
            if original == write.data:
                continue
            backups[target] = (
                _write_verified_backup(target, original, backup_root, stamp)
                if original is not None
                else None
            )
            temporary[target] = _write_temp(target, write.data)

        # Compare every target immediately before the first replacement. This
        # catches Afterburner or another process editing a file while backups
        # and temporary files were being prepared.
        for target, original in originals.items():
            current = target.read_bytes() if target.exists() else None
            if current != original:
                raise TransactionError(
                    f"Target changed during transaction preparation: {target}."
                )

        for write in selected:
            target = write.target.expanduser().resolve(strict=False)
            if target not in temporary:
                continue
            original = originals[target]
            current = target.read_bytes() if target.exists() else None
            if current != original:
                raise TransactionError(
                    f"Target changed before atomic replace: {target}."
                )
            replacer(str(temporary[target]), str(target))
            applied.append(target)
            expected = sha256_bytes(write.data)
            if not target.is_file() or sha256_file(target) != expected:
                raise TransactionError(f"Post-replace hash mismatch for {target}.")

        # A writer can race after its individual replace while later targets
        # are being processed. Verify the complete set before reporting
        # success.
        for target, data in planned_data.items():
            expected = sha256_bytes(data)
            if not target.is_file() or sha256_file(target) != expected:
                raise TransactionError(
                    f"Final transaction hash mismatch for {target}."
                )
    except Exception as exc:
        rollback_errors: list[str] = []
        for target in reversed(applied):
            try:
                original = originals[target]
                current = target.read_bytes() if target.exists() else None
                planned = planned_data[target]
                if current not in (original, planned):
                    raise TransactionError(
                        "external change detected during rollback; left untouched"
                    )
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    rollback_temp = _write_temp(target, original)
                    os.replace(rollback_temp, target)
                    if sha256_file(target) != sha256_bytes(original):
                        raise TransactionError("rollback hash mismatch")
            except Exception as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        for temp_path in temporary.values():
            temp_path.unlink(missing_ok=True)
        detail = f"; rollback failures: {', '.join(rollback_errors)}" if rollback_errors else ""
        raise TransactionError(f"Transaction failed and was rolled back: {exc}{detail}") from exc
    finally:
        for temp_path in temporary.values():
            temp_path.unlink(missing_ok=True)

    receipts: list[WriteReceipt] = []
    for write in selected:
        target = write.target.expanduser().resolve(strict=False)
        original = originals[target]
        receipts.append(
            WriteReceipt(
                target=target,
                backup=backups.get(target),
                before_sha256=sha256_bytes(original) if original is not None else None,
                after_sha256=sha256_file(target),
            )
        )
    return receipts
