"""Clean-room codec for the observed MSI Afterburner VFCurve profile blob.

This module only translates profile bytes.  It does not open Afterburner,
access a GPU, or write a profile.

Observed version-2 layout:

* 12-byte little-endian header: version, point count, reserved/flags
* 256 12-byte slots: voltage_mV, base_frequency_MHz, offset_MHz (float32)
* opaque trailing bytes, preserved verbatim by the encoder

The effective frequency represented by an active point is base + offset.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Iterable


VF_VERSION_2 = 0x00020000
VF_HEADER_BYTES = 12
VF_POINT_BYTES = 12
VF_SLOT_COUNT = 256
VF_BUFFER_BYTES = VF_HEADER_BYTES + VF_POINT_BYTES * VF_SLOT_COUNT


class VFCurveFormatError(ValueError):
    """Raised when a curve cannot be decoded without guessing."""


@dataclass(frozen=True)
class VFPoint:
    """One decoded point from the fixed slot buffer."""

    index: int
    voltage_mv: float
    base_frequency_mhz: float
    offset_mhz: float

    @property
    def effective_frequency_mhz(self) -> float:
        return self.base_frequency_mhz + self.offset_mhz

    def as_dict(self) -> dict[str, int | float]:
        """Return stable, explicit labels for CLI and CSV consumers."""

        return {
            "index": self.index,
            "voltage_mv": self.voltage_mv,
            "base_frequency_mhz": self.base_frequency_mhz,
            "offset_mhz": self.offset_mhz,
            "effective_frequency_mhz": self.effective_frequency_mhz,
        }


@dataclass(frozen=True)
class VFCurve:
    """Decoded curve plus the opaque fields required for exact re-encoding."""

    version: int
    point_count: int
    header_reserved: int
    points: tuple[VFPoint, ...]
    unused_slots: bytes
    footer: bytes


def _compact_hex(hex_value: str) -> str:
    compact = "".join(hex_value.split())
    if not compact or len(compact) % 2 or not re.fullmatch(
        r"[0-9A-Fa-f]+", compact
    ):
        raise VFCurveFormatError(
            "VFCurve must be a non-empty, even-length hexadecimal string."
        )
    return compact


def decode_vf_curve(hex_value: str) -> VFCurve:
    """Decode one complete fixed-slot VFCurve value."""

    compact = _compact_hex(hex_value)
    raw = bytes.fromhex(compact)
    if len(raw) < VF_HEADER_BYTES:
        raise VFCurveFormatError(
            f"VFCurve is shorter than its {VF_HEADER_BYTES}-byte header."
        )

    version, point_count, header_reserved = struct.unpack_from("<III", raw)
    if version != VF_VERSION_2:
        raise VFCurveFormatError(
            f"Unsupported VFCurve version 0x{version:08X}; "
            f"expected 0x{VF_VERSION_2:08X}."
        )
    if point_count < 1 or point_count > VF_SLOT_COUNT:
        raise VFCurveFormatError(
            f"VFCurve point count must be 1..{VF_SLOT_COUNT}, got {point_count}."
        )
    if len(raw) < VF_BUFFER_BYTES:
        raise VFCurveFormatError(
            f"VFCurve is truncated: expected the full {VF_SLOT_COUNT}-slot "
            f"buffer ({VF_BUFFER_BYTES} bytes), got {len(raw)}."
        )

    points = []
    for index in range(point_count):
        voltage_mv, base_frequency_mhz, offset_mhz = struct.unpack_from(
            "<fff",
            raw,
            VF_HEADER_BYTES + index * VF_POINT_BYTES,
        )
        points.append(
            VFPoint(
                index=index,
                voltage_mv=voltage_mv,
                base_frequency_mhz=base_frequency_mhz,
                offset_mhz=offset_mhz,
            )
        )

    active_end = VF_HEADER_BYTES + point_count * VF_POINT_BYTES
    return VFCurve(
        version=version,
        point_count=point_count,
        header_reserved=header_reserved,
        points=tuple(points),
        unused_slots=raw[active_end:VF_BUFFER_BYTES],
        footer=raw[VF_BUFFER_BYTES:],
    )


def encode_vf_curve(
    reference: VFCurve,
    points: Iterable[VFPoint],
) -> str:
    """Encode points while preserving header, unused slots, and footer exactly."""

    selected = tuple(points)
    if len(selected) != reference.point_count:
        raise VFCurveFormatError(
            f"Expected {reference.point_count} points, got {len(selected)}."
        )
    for expected_index, point in enumerate(selected):
        if point.index != expected_index:
            raise VFCurveFormatError(
                f"Point index mismatch at slot {expected_index}: got {point.index}."
            )

    payload = bytearray(
        struct.pack(
            "<III",
            reference.version,
            reference.point_count,
            reference.header_reserved,
        )
    )
    for point in selected:
        payload.extend(
            struct.pack(
                "<fff",
                point.voltage_mv,
                point.base_frequency_mhz,
                point.offset_mhz,
            )
        )

    expected_unused_bytes = (
        VF_SLOT_COUNT - reference.point_count
    ) * VF_POINT_BYTES
    if len(reference.unused_slots) != expected_unused_bytes:
        raise VFCurveFormatError(
            "Reference curve has an invalid unused-slot buffer length."
        )
    payload.extend(reference.unused_slots)
    payload.extend(reference.footer)
    return payload.hex().upper()
