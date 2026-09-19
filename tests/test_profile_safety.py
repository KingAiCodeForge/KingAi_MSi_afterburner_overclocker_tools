from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).parent / "fixtures" / "profiles"
GPU_ID = "VEN_10DE&DEV_0000&SUBSYS_00000000&REV_00&BUS_0&DEV_0&FN_0"
sys.path.insert(0, str(SCRIPTS))

from profile_safety import (  # noqa: E402
    PlannedWrite,
    SafetyError,
    TransactionError,
    atomic_write_transaction,
    get_section,
    patch_profile_contents,
    resolve_config,
    set_ini_value,
    validate_bound,
    validate_vf_hex,
)
from apply_profiles import _project_effective_curve  # noqa: E402
from vf_curve_format import decode_vf_curve  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_vf_hex(
    points: tuple[tuple[float, float, float], ...] = (
        (450.0, 300.0, 0.0),
        (850.0, 1750.0, 50.0),
        (900.0, 1750.0, 50.0),
    ),
    *,
    header_reserved: int = 0,
    unused_fill: int = 0,
    footer: bytes = b"KingAiSyntheticFooter\x00",
) -> str:
    payload = struct.pack("<III", 0x20000, len(points), header_reserved)
    payload += b"".join(struct.pack("<fff", *point) for point in points)
    payload += bytes([unused_fill]) * ((256 - len(points)) * 12)
    payload += footer
    return payload.hex().upper()


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.profiles = Path(self.temp.name) / "profiles"
        shutil.copytree(FIXTURES, self.profiles)
        self.config = self.profiles / f"{GPU_ID}.cfg"
        fixture_text = self.config.read_text(encoding="ascii")
        self.config.write_text(
            fixture_text.replace("{{SYNTHETIC_VF_CURVE}}", make_vf_hex()),
            encoding="ascii",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_script(self, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )


class IdentityAndBoundsTests(FixtureCase):
    def test_exact_identity_resolves(self) -> None:
        selected = resolve_config(config_path=self.config, gpu_id=GPU_ID)
        self.assertEqual(selected, self.config.resolve())

    def test_identity_mismatch_refuses(self) -> None:
        with self.assertRaisesRegex(SafetyError, "identity mismatch"):
            resolve_config(
                config_path=self.config,
                gpu_id="VEN_10DE&DEV_0001&SUBSYS_00000002&REV_01&BUS_98&DEV_0&FN_0",
            )

    def test_no_ambiguous_directory_scan_is_available(self) -> None:
        second = self.profiles / (
            "VEN_10DE&DEV_2484&SUBSYS_00000001&REV_A1&BUS_2&DEV_0&FN_0.cfg"
        )
        shutil.copy2(self.config, second)
        result = self.run_script("apply_profiles.py", "--gpu-id", GPU_ID)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--config", result.stderr)

    def test_numeric_guardrails_reject_out_of_range_values(self) -> None:
        cases = (
            ("core_mhz", 501),
            ("memory_mhz", 2001),
            ("power_percent", 121),
            ("thermal_c", 91),
            ("fan_percent", 19),
            ("vf_voltage_mv", 1301),
            ("vf_frequency_mhz", 4001),
            ("vf_adjustment_mhz", 1001),
        )
        for name, value in cases:
            with self.subTest(name=name), self.assertRaises(SafetyError):
                validate_bound(name, value)

    def test_vf_validation_rejects_malformed_and_nonfinite_curves(self) -> None:
        with self.assertRaises(SafetyError):
            validate_vf_hex("xyz")
        nan_curve = make_vf_hex(((float("nan"), 850.0, 1800.0),))
        with self.assertRaises(SafetyError):
            validate_vf_hex(nan_curve)
        decreasing = make_vf_hex(((450.0, 1000.0, 0.0), (850.0, 800.0, 0.0)))
        with self.assertRaisesRegex(SafetyError, "frequency decreases"):
            validate_vf_hex(decreasing)
        truncated = make_vf_hex()[:200]
        with self.assertRaisesRegex(SafetyError, "truncated"):
            validate_vf_hex(truncated)
        with self.assertRaisesRegex(SafetyError, "footer does not match"):
            validate_vf_hex(
                make_vf_hex(footer=b"X" * len(b"KingAiSyntheticFooter\x00")),
                reference_hex=make_vf_hex(),
            )

    def test_reference_validation_preserves_opaque_unused_slots(self) -> None:
        reference = make_vf_hex(
            header_reserved=7,
            unused_fill=0x5A,
            footer=b"\x00opaque\xfffooter",
        )
        self.assertEqual(validate_vf_hex(reference)["header_reserved"], 7)

        replacement = bytearray.fromhex(reference)
        struct.pack_into("<f", replacement, 12 + 8, 25.0)
        validate_vf_hex(replacement.hex(), reference_hex=reference)

        first_unused = 12 + 3 * 12
        replacement[first_unused] ^= 0x01
        with self.assertRaisesRegex(SafetyError, "unused-slot bytes"):
            validate_vf_hex(replacement.hex(), reference_hex=reference)

    def test_tier_projection_changes_only_target_offset_floats(self) -> None:
        source = make_vf_hex(
            (
                (450.0, 300.0, 0.0),
                (850.0, 1750.0, 50.0),
                (900.0, 1750.0, 50.0),
            ),
            header_reserved=1,
            footer=b"source-footer",
        )
        target = make_vf_hex(
            (
                (450.0, 315.0, 0.0),
                (850.0, 1725.0, 0.0),
                (900.0, 1770.0, 0.0),
            ),
            header_reserved=9,
            unused_fill=0x5A,
            footer=b"\x00target\xfffooter",
        )

        replacement, _summary = _project_effective_curve(source, target)
        source_curve = decode_vf_curve(source)
        target_curve = decode_vf_curve(target)
        replacement_curve = decode_vf_curve(replacement)

        self.assertEqual(replacement_curve.header_reserved, target_curve.header_reserved)
        self.assertEqual(replacement_curve.unused_slots, target_curve.unused_slots)
        self.assertEqual(replacement_curve.footer, target_curve.footer)
        for source_point, target_point, replacement_point in zip(
            source_curve.points,
            target_curve.points,
            replacement_curve.points,
        ):
            self.assertEqual(replacement_point.voltage_mv, target_point.voltage_mv)
            self.assertEqual(
                replacement_point.base_frequency_mhz,
                target_point.base_frequency_mhz,
            )
            self.assertAlmostEqual(
                replacement_point.effective_frequency_mhz,
                source_point.effective_frequency_mhz,
            )

    def test_ini_edits_preserve_crlf_and_unrelated_content(self) -> None:
        block = "Format=2\r\nCoreClkBoost=0\r\nUnrelated=keep\r\n"
        changed = set_ini_value(block, "CoreClkBoost", 80000)
        self.assertEqual(
            changed,
            "Format=2\r\nCoreClkBoost=80000\r\nUnrelated=keep\r\n",
        )
        self.assertEqual(
            set_ini_value("Format=2\r\nUnrelated=keep\r\n", "FanSpeed", 60),
            "Format=2\r\nFanSpeed=60\r\nUnrelated=keep\r\n",
        )
        top = "[Settings]\r\nProfileContents=1\r\nMonitoring=keep\r\n"
        self.assertEqual(
            patch_profile_contents(top),
            "[Settings]\r\nProfileContents=3\r\nMonitoring=keep\r\n",
        )
        self.assertEqual(
            patch_profile_contents("[Settings]\r\nMonitoring=keep\r\n"),
            "[Settings]\r\nProfileContents=3\r\nMonitoring=keep\r\n",
        )

    def test_duplicate_profile_sections_are_refused(self) -> None:
        with self.assertRaisesRegex(SafetyError, "found 2"):
            get_section("[Profile1]\nA=1\n[Profile1]\nA=2\n", "Profile1")


class PlanExecuteTests(FixtureCase):
    def base_arguments(self) -> list[str]:
        return ["--config", str(self.config), "--gpu-id", GPU_ID]

    def test_apply_default_is_plan_and_changes_no_fixture_hash(self) -> None:
        before = {path.name: digest(path) for path in self.profiles.glob("*.cfg")}
        result = self.run_script("apply_profiles.py", *self.base_arguments())
        after = {path.name: digest(path) for path in self.profiles.glob("*.cfg")}
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PLAN ONLY", result.stdout)
        self.assertEqual(before, after)
        self.assertFalse((self.profiles / "KingAiBackups").exists())

    def test_apply_json_plan_is_uncontaminated(self) -> None:
        result = self.run_script(
            "apply_profiles.py",
            *self.base_arguments(),
            "--profile",
            "Profile1",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "plan")
        self.assertEqual(payload["gpu_id"], GPU_ID)

    def test_apply_execute_refuses_implicit_example_values(self) -> None:
        before = digest(self.config)
        result = self.run_script(
            "apply_profiles.py",
            *self.base_arguments(),
            "--execute",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("explicit tuning inputs", result.stderr)
        self.assertEqual(before, digest(self.config))

    def test_create_default_is_plan_and_changes_no_fixture_hash(self) -> None:
        before = digest(self.config)
        result = self.run_script(
            "create_profile.py",
            *self.base_arguments(),
            "--profile",
            "1",
            "--mem",
            "700",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PLAN ONLY", result.stdout)
        self.assertEqual(before, digest(self.config))

    def test_create_execute_requires_afterburner_closed_acknowledgement(self) -> None:
        before = digest(self.config)
        result = self.run_script(
            "create_profile.py",
            *self.base_arguments(),
            "--profile",
            "1",
            "--mem",
            "0",
            "--execute",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--acknowledge-afterburner-closed", result.stderr)
        self.assertEqual(before, digest(self.config))

    def test_execute_creates_verified_backups_and_exact_output(self) -> None:
        before_config = self.config.read_bytes()
        before_top = (self.profiles / "Profile1.cfg").read_bytes()
        backup_dir = self.profiles / "backups"
        result = self.run_script(
            "create_profile.py",
            *self.base_arguments(),
            "--profile",
            "1",
            "--core",
            "80",
            "--mem",
            "700",
            "--power",
            "100",
            "--thermal",
            "83",
            "--fan",
            "60",
            "--copy-startup-vf",
            "--backup-dir",
            str(backup_dir),
            "--acknowledge-afterburner-closed",
            "--execute",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.config.read_text(encoding="ascii")
        self.assertIn("CoreClkBoost=80000", text)
        self.assertIn("MemClkBoost=700000", text)
        self.assertIn("FanSpeed=60", text)
        self.assertIn("ProfileContents=3", (self.profiles / "Profile1.cfg").read_text())

        backups = list(backup_dir.glob("*.bak"))
        self.assertEqual(len(backups), 2)
        backup_payloads = {item.read_bytes() for item in backups}
        self.assertEqual(backup_payloads, {before_config, before_top})

    def test_tier_execute_updates_all_exact_profile_slots(self) -> None:
        backup_dir = self.profiles / "tier-backups"
        result = self.run_script(
            "apply_profiles.py",
            *self.base_arguments(),
            "--memory",
            "0",
            "0",
            "0",
            "0",
            "0",
            "--fan",
            "50",
            "55",
            "60",
            "65",
            "70",
            "--power",
            "100",
            "--thermal",
            "83",
            "--acknowledge-source-profile",
            "--acknowledge-afterburner-closed",
            "--backup-dir",
            str(backup_dir),
            "--execute",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.config.read_text(encoding="ascii")
        for number, (memory, fan) in enumerate(
            zip((0, 0, 0, 0, 0), (50, 55, 60, 65, 70)),
            start=1,
        ):
            section = text.split(f"[Profile{number}]", 1)[1].split("[", 1)[0]
            self.assertIn(f"MemClkBoost={memory * 1000}", section)
            self.assertIn(f"FanSpeed={fan}", section)
            top = (self.profiles / f"Profile{number}.cfg").read_text(encoding="ascii")
            self.assertIn("ProfileContents=3", top)
        self.assertEqual(len(list(backup_dir.glob("*.bak"))), 6)

    def test_every_invalid_profile_setting_refuses_before_write(self) -> None:
        cases = (
            ("--core", "501"),
            ("--mem", "2001"),
            ("--power", "121"),
            ("--thermal", "91"),
            ("--fan", "19"),
        )
        for option, value in cases:
            with self.subTest(option=option):
                before = digest(self.config)
                result = self.run_script(
                    "create_profile.py",
                    *self.base_arguments(),
                    "--profile",
                    "1",
                    option,
                    value,
                    "--acknowledge-afterburner-closed",
                    "--execute",
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(before, digest(self.config))

    def test_invalid_vf_refuses_before_write(self) -> None:
        before = digest(self.config)
        result = self.run_script(
            "create_profile.py",
            *self.base_arguments(),
            "--profile",
            "1",
            "--vf-curve",
            "0200000001000000",
            "--acknowledge-afterburner-closed",
            "--execute",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(before, digest(self.config))

    def test_curve_file_outputs_are_plan_only_without_execute(self) -> None:
        hex_output = Path(self.temp.name) / "planned.hex"
        encode = self.run_script(
            "encode_vf_curve.py",
            *self.base_arguments(),
            "--undervolt",
            "850",
            "1800",
            "--output",
            str(hex_output),
        )
        self.assertEqual(encode.returncode, 0, encode.stderr)
        self.assertIn("PLAN ONLY", encode.stdout)
        self.assertFalse(hex_output.exists())

        csv_output = Path(self.temp.name) / "planned-csv"
        export = self.run_script(
            "export_csv_curve.py",
            *self.base_arguments(),
            "--section",
            "Startup",
            "--output-dir",
            str(csv_output),
        )
        self.assertEqual(export.returncode, 0, export.stderr)
        self.assertIn("PLAN ONLY", export.stdout)
        self.assertFalse(csv_output.exists())

    def test_curve_output_refuses_existing_file_without_overwrite(self) -> None:
        hex_output = Path(self.temp.name) / "existing.hex"
        hex_output.write_text("preserve-me", encoding="ascii")
        result = self.run_script(
            "encode_vf_curve.py",
            *self.base_arguments(),
            "--undervolt",
            "850",
            "1800",
            "--output",
            str(hex_output),
            "--execute",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(hex_output.read_text(encoding="ascii"), "preserve-me")

    def test_undervolt_target_must_match_reference_voltage_grid(self) -> None:
        result = self.run_script(
            "encode_vf_curve.py",
            *self.base_arguments(),
            "--undervolt",
            "851",
            "1800",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("exactly match", result.stderr)


class TransactionRollbackTests(FixtureCase):
    def test_expected_before_hash_refuses_concurrent_change(self) -> None:
        target = self.profiles / "Profile1.cfg"
        expected = digest(target)
        target.write_text("[Settings]\nExternalEdit=preserve\n", encoding="ascii")
        external = target.read_bytes()
        with self.assertRaisesRegex(TransactionError, "changed after planning"):
            atomic_write_transaction(
                [
                    PlannedWrite(
                        target,
                        b"[Settings]\nProfileContents=3\n",
                        "concurrent edit test",
                        expected,
                    )
                ],
                backup_dir=self.profiles / "backups",
            )
        self.assertEqual(target.read_bytes(), external)
        self.assertFalse((self.profiles / "backups").exists())

    def test_injected_second_replace_failure_restores_first_target(self) -> None:
        first = self.config
        second = self.profiles / "Profile1.cfg"
        original_first = first.read_bytes()
        original_second = second.read_bytes()
        calls = 0

        def fail_second_replace(source: str, target: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected replace failure")
            os.replace(source, target)

        with self.assertRaises(TransactionError):
            atomic_write_transaction(
                [
                    PlannedWrite(
                        first,
                        b"[Changed]\nValue=1\n",
                        "first",
                        hashlib.sha256(original_first).hexdigest(),
                    ),
                    PlannedWrite(
                        second,
                        b"[Changed]\nValue=2\n",
                        "second",
                        hashlib.sha256(original_second).hexdigest(),
                    ),
                ],
                backup_dir=self.profiles / "backups",
                replacer=fail_second_replace,
            )

        self.assertEqual(first.read_bytes(), original_first)
        self.assertEqual(second.read_bytes(), original_second)
        self.assertFalse(list(self.profiles.glob("*.kingai-*.tmp")))

    def test_final_hash_check_never_reports_success_after_late_drift(self) -> None:
        first = self.config
        second = self.profiles / "Profile1.cfg"
        original_first = first.read_bytes()
        original_second = second.read_bytes()
        calls = 0

        def drift_after_second_replace(source: str, target: str) -> None:
            nonlocal calls
            calls += 1
            os.replace(source, target)
            if calls == 2:
                first.write_bytes(b"[External]\nLateEdit=preserve\n")

        with self.assertRaisesRegex(TransactionError, "Final transaction hash mismatch"):
            atomic_write_transaction(
                [
                    PlannedWrite(
                        first,
                        b"[Changed]\nValue=1\n",
                        "first",
                        hashlib.sha256(original_first).hexdigest(),
                    ),
                    PlannedWrite(
                        second,
                        b"[Changed]\nValue=2\n",
                        "second",
                        hashlib.sha256(original_second).hexdigest(),
                    ),
                ],
                backup_dir=self.profiles / "backups",
                replacer=drift_after_second_replace,
            )

        self.assertEqual(first.read_bytes(), b"[External]\nLateEdit=preserve\n")
        self.assertEqual(second.read_bytes(), original_second)


if __name__ == "__main__":
    unittest.main()
