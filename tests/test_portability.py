"""Offline release gates; all writable inputs are disposable synthetic copies."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_profile_safety import FIXTURES, GPU_ID, SCRIPTS, make_vf_hex
from profile_safety import SafetyError, patch_profile_contents


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "offline spaces \u00e9"
        self.root.mkdir()
        self.scripts = self.root / "tools"
        shutil.copytree(SCRIPTS, self.scripts, ignore=shutil.ignore_patterns("__pycache__"))
        self.profiles = self.root / "profiles"
        shutil.copytree(FIXTURES, self.profiles)
        self.config = self.profiles / f"{GPU_ID}.cfg"
        self.config.write_text(self.config.read_text(encoding="ascii").replace(
            "{{SYNTHETIC_VF_CURVE}}", make_vf_hex()), encoding="ascii")

    def run_cli(self, name, *args):
        return subprocess.run([sys.executable, str(self.scripts / name), *map(str, args)],
                              cwd=self.root, capture_output=True, text=True, check=False)

    def snapshot(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.profiles.iterdir() if p.is_file()}

    def test_every_python_cli_help_runs_outside_checkout(self):
        names = ("_analyze_config.py", "_compare_generations.py", "apply_profiles.py",
                 "create_profile.py", "decode_vf_curve.py", "encode_vf_curve.py",
                 "export_csv_curve.py", "shared_memory_cli.py")
        for name in names:
            with self.subTest(name=name):
                result = self.run_cli(name, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_plan_and_read_commands_preserve_every_input(self):
        before = self.snapshot()
        common = ("--config", self.config, "--gpu-id", GPU_ID)
        cases = (("apply_profiles.py", *common, "--json"),
                 ("create_profile.py", *common, "--profile", "1", "--core", "0", "--json"),
                 ("decode_vf_curve.py", *common),
                 ("_analyze_config.py", *common, "--json"),
                 ("_compare_generations.py", "--input", "synthetic", GPU_ID, self.config, "--json"),
                 ("encode_vf_curve.py", *common, "--offset", "0", "--output", self.root / "planned.hex"),
                 ("export_csv_curve.py", *common, "--output-dir", self.root / "uncreated"))
        for name, *args in cases:
            with self.subTest(name=name):
                result = self.run_cli(name, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "planned.hex").exists())
        self.assertFalse((self.root / "uncreated").exists())

    def test_encoder_refuses_input_alias_even_with_overwrite(self):
        before = self.snapshot()
        for output in (self.config, self.config.parent / ".." / "profiles" / self.config.name):
            result = self.run_cli("encode_vf_curve.py", "--config", self.config,
                                  "--gpu-id", GPU_ID, "--offset", "0", "--output", output,
                                  "--overwrite", "--execute")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("aliases an input", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_encoder_refuses_hardlink_alias(self):
        alias = self.root / "alias.hex"
        try:
            os.link(self.config, alias)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        before = self.config.read_bytes()
        result = self.run_cli("encode_vf_curve.py", "--config", self.config,
                              "--gpu-id", GPU_ID, "--offset", "0", "--output", alias,
                              "--overwrite", "--execute")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.config.read_bytes(), before)

    def test_encoder_refuses_csv_input_alias(self):
        source = self.root / "input.csv"
        original = "voltage_mV,offset_MHz\n850,50\n"
        source.write_text(original, encoding="utf-8")
        result = self.run_cli("encode_vf_curve.py", "--config", self.config,
                              "--gpu-id", GPU_ID, "--from-csv", source,
                              "--output", source, "--overwrite", "--execute")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_non_ascii_config_fails_without_traceback_or_export_directory(self):
        self.config.write_bytes(b"[Startup]\nVFCurve=\xff\n")
        output = self.root / "uncreated"
        common = ("--config", self.config, "--gpu-id", GPU_ID)
        for name, extra in (("decode_vf_curve.py", []), ("encode_vf_curve.py", ["--offset", "0"]),
                            ("export_csv_curve.py", ["--execute", "--output-dir", output])):
            with self.subTest(name=name):
                result = self.run_cli(name, *common, *extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(output.exists())

    def test_missing_input_fails_without_traceback_or_output(self):
        common = ("--config", self.root / f"{GPU_ID}.cfg", "--gpu-id", GPU_ID)
        for name, extra in (("apply_profiles.py", []), ("create_profile.py", ["--profile", "1", "--core", "0"]),
                            ("decode_vf_curve.py", []), ("encode_vf_curve.py", ["--offset", "0"]),
                            ("export_csv_curve.py", []), ("_analyze_config.py", [])):
            with self.subTest(name=name):
                result = self.run_cli(name, *common, *extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(list(self.profiles.iterdir())), 6)

    def test_ambiguous_settings_spellings_fail_closed(self):
        for body in ("ProfileContents =3\n", "ProfileContents =3\r\n", " profilecontents=3\n",
                     "profilecontents=3\n", "ProfileContents=1\nprofilecontents=2\n"):
            with self.subTest(body=body), self.assertRaises(SafetyError):
                patch_profile_contents("[Settings]\n" + body)
        for header in ("[settings]\n", "[ Settings ]\n", " [Settings]\n", "[Other] ; comment\n"):
            with self.subTest(header=header), self.assertRaises(SafetyError):
                patch_profile_contents("[Settings]\n" + header + "ProfileContents=1\n")

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell wrappers")
    def test_wrappers_report_missing_python_without_touching_profiles(self):
        before = self.snapshot()
        for name in ("apply_profiles.ps1", "decode_vf_curve.ps1"):
            with self.subTest(name=name):
                result = subprocess.run([
                    "powershell.exe", "-NoProfile", "-File", str(self.scripts / name),
                    "-ConfigPath", str(self.config), "-GpuId", GPU_ID,
                    "-PythonCommand", "kingai-synthetic-missing-python-command"],
                    cwd=self.root, text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
