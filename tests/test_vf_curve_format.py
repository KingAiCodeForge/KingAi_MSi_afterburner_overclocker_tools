from __future__ import annotations

import struct
import sys
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vf_curve_format import (  # noqa: E402
    VF_BUFFER_BYTES,
    VF_HEADER_BYTES,
    VF_POINT_BYTES,
    VF_SLOT_COUNT,
    VF_VERSION_2,
    VFCurveFormatError,
    decode_vf_curve,
    encode_vf_curve,
)


def make_blob(
    points: tuple[tuple[float, float, float], ...],
    *,
    reserved: int = 0,
    unused_fill: int = 0,
    footer: bytes = b"synthetic-footer",
) -> str:
    payload = bytearray(struct.pack("<III", VF_VERSION_2, len(points), reserved))
    for point in points:
        payload.extend(struct.pack("<fff", *point))
    payload.extend(
        bytes([unused_fill])
        * ((VF_SLOT_COUNT - len(points)) * VF_POINT_BYTES)
    )
    payload.extend(footer)
    return payload.hex().upper()


class VFCurveFormatTests(unittest.TestCase):
    def test_decodes_12_byte_header_and_base_plus_offset(self) -> None:
        encoded = make_blob(
            (
                (450.0, 300.0, 0.0),
                (850.0, 1750.0, 50.0),
                (900.0, 1800.0, -15.0),
            ),
            reserved=0xA5A5A5A5,
        )

        curve = decode_vf_curve(encoded)

        self.assertEqual(curve.version, VF_VERSION_2)
        self.assertEqual(curve.point_count, 3)
        self.assertEqual(curve.header_reserved, 0xA5A5A5A5)
        self.assertEqual(curve.points[1].voltage_mv, 850.0)
        self.assertEqual(curve.points[1].base_frequency_mhz, 1750.0)
        self.assertEqual(curve.points[1].offset_mhz, 50.0)
        self.assertEqual(curve.points[1].effective_frequency_mhz, 1800.0)
        self.assertEqual(curve.points[2].effective_frequency_mhz, 1785.0)

    def test_round_trip_preserves_header_unused_slots_and_footer(self) -> None:
        encoded = make_blob(
            ((450.0, 300.0, 0.0), (850.0, 1750.0, 50.0)),
            reserved=7,
            unused_fill=0x5A,
            footer=b"\x00opaque\xfffooter",
        )
        curve = decode_vf_curve(encoded)

        self.assertEqual(encode_vf_curve(curve, curve.points), encoded)

    def test_offset_edit_changes_only_offset_float(self) -> None:
        encoded = make_blob(
            ((450.0, 300.0, 0.0), (850.0, 1750.0, 50.0)),
            reserved=9,
            footer=b"opaque-footer",
        )
        curve = decode_vf_curve(encoded)
        edited_points = (
            curve.points[0],
            replace(curve.points[1], offset_mhz=75.0),
        )

        edited = bytes.fromhex(encode_vf_curve(curve, edited_points))
        original = bytes.fromhex(encoded)
        offset_start = VF_HEADER_BYTES + VF_POINT_BYTES + 8

        self.assertEqual(edited[:offset_start], original[:offset_start])
        self.assertNotEqual(
            edited[offset_start : offset_start + 4],
            original[offset_start : offset_start + 4],
        )
        self.assertEqual(edited[offset_start + 4 :], original[offset_start + 4 :])
        self.assertEqual(struct.unpack_from("<f", edited, offset_start)[0], 75.0)
        self.assertEqual(len(edited), VF_BUFFER_BYTES + len(b"opaque-footer"))

    def test_rejects_old_header_alignment_unknown_version_and_truncation(self) -> None:
        old_layout = struct.pack("<IIfff", VF_VERSION_2, 1, 0.0, 450.0, 300.0)
        with self.assertRaises(VFCurveFormatError):
            decode_vf_curve(old_layout.hex())

        unknown_version = bytearray.fromhex(
            make_blob(((450.0, 300.0, 0.0),))
        )
        struct.pack_into("<I", unknown_version, 0, 0x00030000)
        with self.assertRaisesRegex(VFCurveFormatError, "Unsupported"):
            decode_vf_curve(unknown_version.hex())

        truncated = bytes.fromhex(
            make_blob(((450.0, 300.0, 0.0),))
        )[: VF_BUFFER_BYTES - 1]
        with self.assertRaisesRegex(VFCurveFormatError, "truncated"):
            decode_vf_curve(truncated.hex())


if __name__ == "__main__":
    unittest.main()
