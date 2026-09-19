from __future__ import annotations

import os
import hashlib
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GPU_ID = "VEN_10DE&DEV_0000&SUBSYS_00000000&REV_00&BUS_0&DEV_0&FN_0"


def make_vf_hex() -> str:
    points = (
        (450.0, 300.0, 0.0),
        (850.0, 1750.0, 50.0),
        (900.0, 1800.0, 50.0),
    )
    payload = struct.pack("<III", 0x20000, len(points), 0)
    payload += b"".join(struct.pack("<fff", *point) for point in points)
    payload += b"\x00" * ((256 - len(points)) * 12)
    payload += b"KingAiSyntheticFooter\x00"
    return payload.hex().upper()


@unittest.skipUnless(os.name == "nt", "Windows PowerShell parser test")
class PowerShellSyntaxTests(unittest.TestCase):
    def test_advertised_scripts_parse_in_windows_powershell_51(self) -> None:
        paths = [
            ROOT / "scripts" / "apply_profiles.ps1",
            ROOT / "scripts" / "decode_vf_curve.ps1",
        ]
        for path in paths:
            escaped = str(path).replace("'", "''")
            command = (
                "$tokens=$null; $errors=$null; "
                f"[System.Management.Automation.Language.Parser]::ParseFile('{escaped}',"
                "[ref]$tokens,[ref]$errors) | Out-Null; "
                "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, f"{path}\n{result.stderr}")

    def test_powershell_writer_wrapper_is_plan_only_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profiles = Path(temporary) / "profiles"
            shutil.copytree(Path(__file__).parent / "fixtures" / "profiles", profiles)
            config = profiles / f"{GPU_ID}.cfg"
            config.write_text(
                config.read_text(encoding="ascii").replace(
                    "{{SYNTHETIC_VF_CURVE}}",
                    make_vf_hex(),
                ),
                encoding="ascii",
            )
            before = hashlib.sha256(config.read_bytes()).hexdigest()
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "apply_profiles.ps1"),
                    "-ConfigPath",
                    str(config),
                    "-GpuId",
                    GPU_ID,
                    "-Profile",
                    "Profile1",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            after = hashlib.sha256(config.read_bytes()).hexdigest()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PLAN ONLY", result.stdout)
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
