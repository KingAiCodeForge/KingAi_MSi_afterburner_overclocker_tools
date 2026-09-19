from __future__ import annotations

import contextlib
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from afterburner_shared_memory import (  # noqa: E402
    DEAD_SIGNATURE,
    MACM_SIGNATURE,
    MAHM_SIGNATURE,
    MappingUnavailableError,
    SnapshotFormatError,
    filter_snapshot,
    parse_macm_snapshot,
    parse_mahm_snapshot,
)
from shared_memory_cli import main as shared_memory_main  # noqa: E402


GPU_ID = "VEN_10DE&DEV_0000&SUBSYS_00000000&REV_00&BUS_0&DEV_0&FN_0"
MAHM_HEADER = struct.Struct("<8I")
MAHM_ENTRY = struct.Struct("<260s260s260s260s260sfffIII")
MAHM_GPU_ENTRY = struct.Struct("<260s260s260s260s260sI")
MACM_HEADER = struct.Struct("<9I")


def c_string(value: str) -> bytes:
    encoded = value.encode("ascii")
    if len(encoded) >= 260:
        raise ValueError("synthetic string is too long")
    return encoded + b"\0" * (260 - len(encoded))


def c_bytes(value: bytes) -> bytes:
    if len(value) >= 260:
        raise ValueError("synthetic byte string is too long")
    return value + b"\0" * (260 - len(value))


def make_mahm_snapshot(*, include_cpu_instance: bool = False) -> bytes:
    header_size = MAHM_HEADER.size + 8
    entry_size = MAHM_ENTRY.size + 8
    gpu_entry_size = MAHM_GPU_ENTRY.size + 8
    entry_count = 3 if include_cpu_instance else 2
    gpu_count = 1
    total = header_size + entry_count * entry_size + gpu_count * gpu_entry_size
    payload = bytearray(total)
    MAHM_HEADER.pack_into(
        payload,
        0,
        MAHM_SIGNATURE,
        0x00020000,
        header_size,
        entry_count,
        entry_size,
        1_800_000_000,
        gpu_count,
        gpu_entry_size,
    )
    MAHM_ENTRY.pack_into(
        payload,
        header_size,
        c_string("Core clock"),
        c_string("MHz"),
        c_string("Core clock"),
        c_string("MHz"),
        c_string("%.0f"),
        2010.0,
        0.0,
        3000.0,
        0x1,
        0,
        0x20,
    )
    MAHM_ENTRY.pack_into(
        payload,
        header_size + entry_size,
        c_string("Frametime"),
        c_string("ms"),
        c_string("Frametime"),
        c_string("ms"),
        c_string("%.1f"),
        3.402823466e38,
        0.0,
        100.0,
        0,
        0xFFFFFFFF,
        0x51,
    )
    if include_cpu_instance:
        MAHM_ENTRY.pack_into(
            payload,
            header_size + 2 * entry_size,
            c_string("CPU16 temperature"),
            c_string("C"),
            c_string("CPU16 temperature"),
            c_string("C"),
            c_string("%.0f"),
            58.0,
            0.0,
            100.0,
            0,
            15,
            0x80,
        )
    gpu_offset = header_size + entry_count * entry_size
    MAHM_GPU_ENTRY.pack_into(
        payload,
        gpu_offset,
        c_string(GPU_ID),
        c_string("Synthetic"),
        c_string("Synthetic GPU"),
        c_string("0.0 test driver"),
        c_string("00.00.00.00.00"),
        8 * 1024 * 1024,
    )
    return bytes(payload)


def _pack_control(
    payload: bytearray,
    base: int,
    offset: int,
    values: tuple[int, int, int, int],
    *,
    signed: bool = False,
) -> None:
    struct.pack_into("<4i" if signed else "<4I", payload, base + offset, *values)


def make_macm_snapshot() -> bytes:
    header_size = MACM_HEADER.size + 8
    gpu_entry_size = 3760 + 8
    total = header_size + gpu_entry_size
    payload = bytearray(total)
    flags = (
        0x00000001
        | 0x00000004
        | 0x00000008
        | 0x00000400
        | 0x00000800
        | 0x00001000
        | 0x00002000
        | 0x00004000
        | 0x00020000
        | 0x00040000
    )
    MACM_HEADER.pack_into(
        payload,
        0,
        MACM_SIGNATURE,
        0x00020003,
        header_size,
        1,
        gpu_entry_size,
        0,
        0x00000002,
        1_800_000_001,
        0,
    )
    entry = header_size
    struct.pack_into("<I", payload, entry, flags)
    _pack_control(payload, entry, 4, (1_500_000, 0, 3_000_000, 1_425_000))
    _pack_control(payload, entry, 36, (7_000_000, 0, 9_000_000, 7_000_000))
    struct.pack_into("<6I", payload, entry + 52, 60, 0, 20, 100, 40, 1)
    _pack_control(payload, entry, 172, (100, 50, 120, 100), signed=True)
    _pack_control(payload, entry, 188, (61_000, -500_000, 500_000, 0), signed=True)
    _pack_control(payload, entry, 204, (200_000, -2_000_000, 2_000_000, 0), signed=True)
    _pack_control(payload, entry, 220, (83, 60, 90, 83), signed=True)
    struct.pack_into("<2I", payload, entry + 236, 1, 0)

    vf = entry + 276
    struct.pack_into("<3I", payload, vf, 1, 0, 2)
    points = vf + 12
    struct.pack_into("<IIi", payload, points, 850_000, 1_800_000, 61_000)
    struct.pack_into("<IIi", payload, points + 12, 900_000, 1_830_000, 61_000)
    lock = points + 256 * 12
    struct.pack_into("<2I", payload, lock, 0, 1)
    power = lock + 8
    struct.pack_into("<4I", payload, power, 100, 100, 1_800_000, 1_739_000)
    thermal_count = power + 4 * 16
    struct.pack_into("<I", payload, thermal_count, 1)
    struct.pack_into(
        "<4I",
        payload,
        thermal_count + 4,
        83,
        83,
        1_800_000,
        1_739_000,
    )
    payload[entry + 3500 : entry + 3760] = c_string(GPU_ID)
    return bytes(payload)


class MahMParserTests(unittest.TestCase):
    def test_variable_sized_layout_and_unavailable_value(self) -> None:
        snapshot = parse_mahm_snapshot(make_mahm_snapshot())
        self.assertEqual(snapshot["version"]["major"], 2)
        self.assertEqual(snapshot["layout"]["header_size"], MAHM_HEADER.size + 8)
        self.assertEqual(snapshot["gpus"][0]["gpu_id"], GPU_ID)
        self.assertEqual(snapshot["sources"][0]["source_key"], "core_clock")
        self.assertEqual(snapshot["sources"][0]["value"], 2010.0)
        self.assertTrue(snapshot["sources"][0]["available"])
        self.assertEqual(snapshot["sources"][0]["locations"], ["osd"])
        self.assertIsNone(snapshot["sources"][1]["value"])
        self.assertFalse(snapshot["sources"][1]["available"])
        self.assertIsNone(snapshot["sources"][1]["gpu_index"])
        self.assertEqual(snapshot["sources"][0]["scope"], "gpu")
        self.assertEqual(snapshot["sources"][1]["scope"], "system")

    def test_legacy_ansi_unit_decodes_without_replacement_character(self) -> None:
        raw = bytearray(make_mahm_snapshot())
        units_offset = MAHM_HEADER.size + 8 + 260
        raw[units_offset : units_offset + 260] = c_bytes(b"\xB0C")
        snapshot = parse_mahm_snapshot(bytes(raw))
        self.assertEqual(snapshot["sources"][0]["unit"], "°C")

    def test_system_instance_index_is_not_mistaken_for_gpu_index(self) -> None:
        snapshot = parse_mahm_snapshot(
            make_mahm_snapshot(include_cpu_instance=True)
        )
        source = snapshot["sources"][2]
        self.assertEqual(source["scope"], "system")
        self.assertIsNone(source["gpu_index"])
        self.assertEqual(source["instance_index"], 15)

        filtered = filter_snapshot(snapshot, gpu_id=GPU_ID)
        self.assertEqual(len(filtered["sources"]), 3)

    def test_unknown_source_is_not_guessed_to_belong_to_a_gpu(self) -> None:
        raw = bytearray(make_mahm_snapshot())
        source_id_offset = MAHM_HEADER.size + 8 + MAHM_ENTRY.size - 4
        struct.pack_into("<I", raw, source_id_offset, 0x12345678)
        snapshot = parse_mahm_snapshot(bytes(raw))
        source = snapshot["sources"][0]
        self.assertEqual(source["scope"], "unresolved")
        self.assertIsNone(source["gpu_index"])
        self.assertEqual(source["instance_index"], 0)

    def test_exact_gpu_and_source_filter_retains_global_source(self) -> None:
        snapshot = parse_mahm_snapshot(make_mahm_snapshot())
        filtered = filter_snapshot(snapshot, gpu_id=GPU_ID)
        self.assertEqual(len(filtered["gpus"]), 1)
        self.assertEqual(len(filtered["sources"]), 2)
        source_only = filter_snapshot(snapshot, source_ids={0x20})
        self.assertEqual(
            [source["source_key"] for source in source_only["sources"]],
            ["core_clock"],
        )
        with self.assertRaisesRegex(SnapshotFormatError, "matched 0"):
            filter_snapshot(snapshot, gpu_id="VEN_10DE&DEV_FFFF")

    def test_rejects_dead_truncated_and_bad_gpu_reference(self) -> None:
        dead = bytearray(make_mahm_snapshot())
        struct.pack_into("<I", dead, 0, DEAD_SIGNATURE)
        with self.assertRaisesRegex(SnapshotFormatError, "marked dead"):
            parse_mahm_snapshot(bytes(dead))

        with self.assertRaisesRegex(SnapshotFormatError, "truncated"):
            parse_mahm_snapshot(make_mahm_snapshot()[:-1])

        invalid = bytearray(make_mahm_snapshot())
        gpu_index_offset = MAHM_HEADER.size + 8 + 1300 + 16
        struct.pack_into("<I", invalid, gpu_index_offset, 2)
        with self.assertRaisesRegex(SnapshotFormatError, "missing GPU index"):
            parse_mahm_snapshot(bytes(invalid))


class MacMParserTests(unittest.TestCase):
    def test_controls_ranges_identity_and_vf_curve(self) -> None:
        snapshot = parse_macm_snapshot(make_macm_snapshot())
        self.assertEqual(snapshot["version"]["minor"], 3)
        self.assertEqual(snapshot["flag_labels"], ["synchronized"])
        self.assertEqual(snapshot["command"]["name"], "idle")
        gpu = snapshot["gpus"][0]
        self.assertEqual(gpu["gpu_id"], GPU_ID)
        self.assertTrue(gpu["is_master"])
        self.assertEqual(gpu["controls"]["core_clock_boost_khz"]["current"], 61_000)
        self.assertEqual(gpu["controls"]["memory_clock_boost_khz"]["current"], 200_000)
        self.assertEqual(gpu["controls"]["fan_speed_percent"]["current"], 60)
        self.assertFalse(gpu["controls"]["fan_speed_percent"]["automatic_current"])
        self.assertEqual(gpu["vf_curve"]["point_count"], 2)
        self.assertEqual(gpu["vf_curve"]["points"][0]["voltage_uv"], 850_000)
        self.assertEqual(
            gpu["vf_curve"]["points"][0]["frequency_offset_khz"],
            61_000,
        )

    def test_rejects_truncation_bad_master_and_excess_vf_points(self) -> None:
        with self.assertRaisesRegex(SnapshotFormatError, "truncated"):
            parse_macm_snapshot(make_macm_snapshot()[:-1])

        bad_master = bytearray(make_macm_snapshot())
        struct.pack_into("<I", bad_master, 20, 1)
        with self.assertRaisesRegex(SnapshotFormatError, "master GPU index"):
            parse_macm_snapshot(bytes(bad_master))

        too_many_points = bytearray(make_macm_snapshot())
        vf_count_offset = MACM_HEADER.size + 8 + 276 + 8
        struct.pack_into("<I", too_many_points, vf_count_offset, 257)
        with self.assertRaisesRegex(SnapshotFormatError, "maximum is 256"):
            parse_macm_snapshot(bytes(too_many_points))


class SharedMemoryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshots = {
            "mahm": parse_mahm_snapshot(make_mahm_snapshot()),
            "macm": parse_macm_snapshot(make_macm_snapshot()),
        }

    def run_cli(
        self,
        arguments: list[str],
        *,
        reader=None,
        clock=lambda: 1_800_000_100.0,
    ) -> tuple[int, str, str]:
        selected_reader = reader or (lambda kind: self.snapshots[kind])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = shared_memory_main(
                arguments,
                snapshot_reader=selected_reader,
                sleeper=lambda _interval: None,
                clock=clock,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_capabilities_is_clean_json_and_read_only(self) -> None:
        code, stdout, stderr = self.run_cli(["capabilities"])
        payload = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["access"], "read_only")
        self.assertIn("shared_memory_write", payload["prohibited_actions"])

    def test_monitor_json_jsonl_and_csv_are_machine_readable(self) -> None:
        code, stdout, stderr = self.run_cli(["monitor", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["snapshot"]["sources"][0]["value"], 2010.0)

        code, stdout, stderr = self.run_cli(
            ["monitor", "--samples", "2", "--source-id", "0x20"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        lines = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([line["sample_index"] for line in lines], [0, 1])
        self.assertEqual(len(lines[0]["snapshot"]["sources"]), 1)

        code, stdout, stderr = self.run_cli(
            ["monitor", "--format", "csv", "--source-id", "32"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("sample_index,captured_at_utc,poll_time_unix", stdout)
        self.assertIn(",core_clock,Core clock,", stdout)

    def test_json_stdout_escapes_non_ascii_for_powershell_compatibility(self) -> None:
        snapshot = json.loads(json.dumps(self.snapshots["mahm"]))
        snapshot["sources"][0]["unit"] = "°C"
        code, stdout, stderr = self.run_cli(
            ["monitor", "--format", "json"],
            reader=lambda _kind: snapshot,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("\\u00b0C", stdout)
        self.assertEqual(json.loads(stdout)["snapshot"]["sources"][0]["unit"], "°C")

    def test_control_state_json_exposes_exact_selected_gpu(self) -> None:
        code, stdout, stderr = self.run_cli(
            ["control-state", "--gpu-id", GPU_ID]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["snapshot"]["gpus"][0]["gpu_id"], GPU_ID)
        self.assertEqual(
            payload["snapshot"]["gpus"][0]["controls"]["memory_clock_boost_khz"][
                "current"
            ],
            200_000,
        )

    def test_output_is_plan_only_without_execute_or_mapping_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "telemetry.jsonl"

            def forbidden_reader(_kind: str):
                raise AssertionError("plan-only mode opened a mapping")

            code, stdout, stderr = self.run_cli(
                [
                    "monitor",
                    "--format",
                    "jsonl",
                    "--output",
                    str(output),
                ],
                reader=forbidden_reader,
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(json.loads(stdout)["mode"], "plan")
            self.assertFalse(output.exists())

    def test_execute_writes_log_but_never_mutates_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "telemetry.jsonl"
            before = json.dumps(self.snapshots, sort_keys=True)
            code, stdout, stderr = self.run_cli(
                [
                    "monitor",
                    "--format",
                    "jsonl",
                    "--samples",
                    "2",
                    "--output",
                    str(output),
                    "--execute",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(json.loads(stdout)["samples_written"], 2)
            self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 2)
            self.assertEqual(json.dumps(self.snapshots, sort_keys=True), before)

    def test_existing_or_incompatible_append_target_is_refused_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "telemetry.jsonl"
            output.write_text("preserve me\n", encoding="utf-8")

            def forbidden_reader(_kind: str):
                raise AssertionError("invalid file request opened a mapping")

            code, stdout, stderr = self.run_cli(
                [
                    "monitor",
                    "--format",
                    "jsonl",
                    "--output",
                    str(output),
                    "--execute",
                ],
                reader=forbidden_reader,
            )
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("output already exists", stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "preserve me\n")

            code, stdout, stderr = self.run_cli(
                [
                    "monitor",
                    "--format",
                    "jsonl",
                    "--output",
                    str(output),
                    "--append",
                    "--execute",
                ],
                reader=forbidden_reader,
            )
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("record 1 is not valid JSON", stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "preserve me\n")

    def test_append_refuses_incomplete_or_corrupt_tail_before_read(self) -> None:
        record = {
            "schema": "kingai.afterburner.shared_memory_sample.v1",
            "sample_index": 0,
            "captured_at_utc": "2027-01-15T08:01:40Z",
            "snapshot": self.snapshots["mahm"],
        }

        def forbidden_reader(_kind: str):
            raise AssertionError("invalid append target opened a mapping")

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "telemetry.jsonl"
            incomplete = json.dumps(record, sort_keys=True)
            output.write_text(incomplete, encoding="utf-8")
            code, stdout, stderr = self.run_cli(
                [
                    "monitor",
                    "--format",
                    "jsonl",
                    "--output",
                    str(output),
                    "--append",
                    "--execute",
                ],
                reader=forbidden_reader,
            )
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("does not end with a newline", stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), incomplete)

            corrupt = incomplete + "\nnot-json\n"
            output.write_text(corrupt, encoding="utf-8")
            code, stdout, stderr = self.run_cli(
                [
                    "monitor",
                    "--format",
                    "jsonl",
                    "--output",
                    str(output),
                    "--append",
                    "--execute",
                ],
                reader=forbidden_reader,
            )
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("record 2 is not valid JSON", stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), corrupt)

    def test_append_validates_every_csv_row_before_read(self) -> None:
        def forbidden_reader(_kind: str):
            raise AssertionError("invalid append target opened a mapping")

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "telemetry.csv"
            output.write_text(
                ",".join(
                    (
                        "sample_index",
                        "captured_at_utc",
                        "poll_time_unix",
                        "scope",
                        "gpu_index",
                        "gpu_id",
                        "instance_index",
                        "source_index",
                        "source_id",
                        "source_key",
                        "name",
                        "localized_name",
                        "value",
                        "unit",
                        "minimum",
                        "maximum",
                        "available",
                        "flags",
                        "locations",
                    )
                )
                + "\nshort,row\n",
                encoding="utf-8",
            )
            before = output.read_text(encoding="utf-8")
            code, stdout, stderr = self.run_cli(
                [
                    "monitor",
                    "--format",
                    "csv",
                    "--output",
                    str(output),
                    "--append",
                    "--execute",
                ],
                reader=forbidden_reader,
            )
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("CSV row 2", stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), before)

    def test_valid_jsonl_append_adds_a_separate_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "telemetry.jsonl"
            arguments = [
                "monitor",
                "--format",
                "jsonl",
                "--output",
                str(output),
                "--execute",
            ]
            code, _stdout, stderr = self.run_cli(arguments)
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            code, _stdout, stderr = self.run_cli(
                arguments[:-1] + ["--append", "--execute"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(all(json.loads(line) for line in lines))

    def test_argument_errors_are_one_json_object(self) -> None:
        def forbidden_reader(_kind: str):
            raise AssertionError("invalid arguments opened a mapping")

        code, stdout, stderr = self.run_cli(
            ["monitor", "--samples", "0"],
            reader=forbidden_reader,
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        error = json.loads(stderr)["error"]
        self.assertEqual(error["code"], "invalid_request")
        self.assertIn("samples must be between", error["message"])

    def test_unavailable_mapping_has_stable_json_error_and_no_output(self) -> None:
        def missing(_kind: str):
            raise MappingUnavailableError("synthetic mapping is absent")

        code, stdout, stderr = self.run_cli(["monitor"], reader=missing)
        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"]["code"], "mapping_unavailable")


if __name__ == "__main__":
    unittest.main()
