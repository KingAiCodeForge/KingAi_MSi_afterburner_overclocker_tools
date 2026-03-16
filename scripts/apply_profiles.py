#!/usr/bin/env python3
"""
Apply tiered overclock profiles to MSI Afterburner config.

Copies the VF curve from Startup to all 5 profile slots and sets per-profile
memory clock offsets and fan speeds. Writes config to a temp location first,
then copies to Program Files via admin elevation.

Compatible with NVIDIA GTX 10-series (Pascal) and newer.

Usage:
    python apply_profiles.py --dry-run    # Preview without writing
    python apply_profiles.py              # Apply and copy
    python apply_profiles.py --config "path/to/custom.cfg"
"""

import argparse
import ctypes
import glob
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from datetime import datetime


# ── Logging tee ─────────────────────────────────────────────────────────────
class _Tee:
    """Write to both a file and the original stream."""
    def __init__(self, stream, log_file):
        self._stream = stream
        self._log = log_file

    def write(self, data):
        self._stream.write(data)
        self._log.write(data)

    def flush(self):
        self._stream.flush()
        self._log.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _init_log(script_name: str):
    """Set up file logging. Returns the log file path."""
    logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(logs_dir, f"{script_name}_{stamp}.log")
    log_file = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, log_file)
    sys.stderr = _Tee(sys.__stderr__, log_file)
    return log_path


# ── Constants ───────────────────────────────────────────────────────────────
AB_PROFILES_DIR = os.environ.get(
    "AB_PROFILES_DIR",
    os.path.join(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        "MSI Afterburner", "Profiles",
    ),
)

# Tiered profile definitions: (name, mem_offset_kHz, fan_pct)
# Memory values in kHz (Afterburner's internal unit). Divide by 1000 for MHz.
PROFILES = [
    ("Profile1", 500000,  50),   # +500 MHz mem, 50% fan
    ("Profile2", 600000,  55),   # +600 MHz mem, 55% fan
    ("Profile3", 700000,  60),   # +700 MHz mem, 60% fan
    ("Profile4", 800000,  70),   # +800 MHz mem, 70% fan
    ("Profile5", 900000,  80),   # +900 MHz mem, 80% fan
]


def ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def find_config(custom_path: str | None = None) -> str:
    if custom_path:
        if not os.path.isfile(custom_path):
            print(f"{ts()} ERROR: Config not found: {custom_path}", file=sys.stderr)
            sys.exit(1)
        return custom_path

    pattern = os.path.join(AB_PROFILES_DIR, "VEN_10DE*.cfg")
    matches = glob.glob(pattern)
    if not matches:
        print(f"{ts()} ERROR: No NVIDIA config found in {AB_PROFILES_DIR}", file=sys.stderr)
        sys.exit(1)

    best = max(matches, key=os.path.getsize)
    if len(matches) > 1:
        print(f"{ts()} Found {len(matches)} GPU configs, using largest: {os.path.basename(best)}")
    return best


def set_ini_value(block: str, key: str, value: str) -> str:
    """Set a key=value in an INI section block. Adds the key if not present."""
    pattern = re.compile(rf"^({re.escape(key)})=.*$", re.MULTILINE)
    if pattern.search(block):
        return pattern.sub(f"{key}={value}", block, count=1)
    else:
        # Add after Format= line if present, otherwise at the end
        format_match = re.search(r"^Format=.*$", block, re.MULTILINE)
        if format_match:
            insert_pos = format_match.end()
            return block[:insert_pos] + f"\n{key}={value}" + block[insert_pos:]
        return block.rstrip("\n") + f"\n{key}={value}\n"


def get_ini_value(block: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}=(.+)$", block, re.MULTILINE)
    return match.group(1).strip() if match else None


def parse_sections(text: str) -> list[tuple[str, str, str]]:
    """
    Parse config into list of (section_header_line, section_name, section_body).
    Preserves exact original formatting.
    """
    parts = []
    current_name = None
    current_header = None
    lines = []

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current_name is not None:
                parts.append((current_header, current_name, "".join(lines)))
            current_name = stripped[1:-1]
            current_header = line
            lines = []
        else:
            lines.append(line)

    if current_name is not None:
        parts.append((current_header, current_name, "".join(lines)))

    return parts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply tiered OC profiles to MSI Afterburner config."
    )
    parser.add_argument("--config", "-c", help="Path to a specific .cfg file.")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Preview changes without writing any files.")
    args = parser.parse_args()

    log_path = _init_log("apply_profiles")

    cfg_path = find_config(args.config)
    print(f"{ts()} Log file: {log_path}")
    print(f"{ts()} Config: {os.path.basename(cfg_path)}")
    print(f"{ts()} Full path: {cfg_path}")
    print()

    if args.dry_run:
        print(f"{ts()} DRY RUN — no files will be modified")
        print()

    # Read config
    with open(cfg_path, "r", encoding="ascii", errors="replace") as f:
        text = f.read()

    # Parse into sections
    sections = parse_sections(text)
    section_map = {name: body for _, name, body in sections}

    # Get VF curve from Startup
    startup_body = section_map.get("Startup", "")
    vf_match = re.search(r"VFCurve=([0-9A-Fa-f]+)", startup_body)
    if not vf_match:
        print(f"{ts()} ERROR: No VFCurve found in [Startup] section.", file=sys.stderr)
        print(f"{ts()} Set up your undervolt in Afterburner first, then run this.", file=sys.stderr)
        sys.exit(1)

    vf_hex = vf_match.group(1)
    print(f"{ts()} Source VF curve from [Startup]: {len(vf_hex)} hex chars")

    # Get Startup's CoreClkBoost for reference
    startup_core = get_ini_value(startup_body, "CoreClkBoost") or "0"
    print(f"{ts()} Startup CoreClkBoost: {int(startup_core) / 1000:.0f} MHz")
    print()

    # Build modified config
    new_sections = []
    for header, name, body in sections:
        # Find matching profile definition
        profile_def = next((p for p in PROFILES if p[0] == name), None)

        if profile_def:
            pname, mem_khz, fan_pct = profile_def
            body = set_ini_value(body, "CoreClkBoost", startup_core)
            body = set_ini_value(body, "MemClkBoost", str(mem_khz))
            body = set_ini_value(body, "FanMode", "1")
            body = set_ini_value(body, "FanSpeed", str(fan_pct))
            body = set_ini_value(body, "VFCurve", vf_hex)
            # Preserve existing power/thermal or set safe defaults
            if not get_ini_value(body, "PowerLimit"):
                body = set_ini_value(body, "PowerLimit", "100")
            if not get_ini_value(body, "ThermalLimit"):
                body = set_ini_value(body, "ThermalLimit", "83")

            print(f"{ts()} {pname} -> Core: {int(startup_core)/1000:.0f} MHz | "
                  f"Mem: +{mem_khz/1000:.0f} MHz | Fan: {fan_pct}% | VF: Startup curve")

        new_sections.append(header + body)

    print()

    if args.dry_run:
        print(f"{ts()} DRY RUN complete. No files modified.")
        return

    # Reassemble config content
    content = "".join(new_sections)

    # Write to temp location (writable without admin)
    temp_dir = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Temp")
    temp_path = os.path.join(temp_dir, "AB_profile_modified.cfg")

    # Write as ASCII with no BOM — critical for Afterburner compatibility
    with open(temp_path, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
        f.write(content)

    # Verify no BOM
    with open(temp_path, "rb") as f:
        first3 = f.read(3)
    if first3 == b"\xef\xbb\xbf":
        print(f"{ts()} WARNING: BOM detected in output, stripping...")
        with open(temp_path, "rb") as f:
            data = f.read()
        with open(temp_path, "wb") as f:
            f.write(data[3:])

    file_size = os.path.getsize(temp_path)
    print(f"{ts()} Written to: {temp_path} ({file_size} bytes, ASCII, no BOM)")

    # Backup original
    backup_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(temp_dir, f"AB_profile_backup_{backup_suffix}.cfg")
    shutil.copy2(cfg_path, backup_path)
    print(f"{ts()} Backup saved: {backup_path}")

    # Copy to Afterburner profiles dir (needs admin)
    print(f"{ts()} Copying to Afterburner profile dir (admin required)...")
    try:
        # Try direct copy first (works if running as admin)
        shutil.copy2(temp_path, cfg_path)
        print(f"{ts()} SUCCESS: Config updated directly.")
    except PermissionError:
        # Elevate via PowerShell
        ps_cmd = f"Copy-Item '{temp_path}' '{cfg_path}' -Force; Start-Sleep 1"
        result = subprocess.run(
            ["powershell", "-Command",
             f"Start-Process powershell -Verb RunAs -ArgumentList '-Command',\"{ps_cmd}\" -Wait"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            # Verify the copy worked
            live_size = os.path.getsize(cfg_path)
            with open(cfg_path, "rb") as f:
                first_byte = f.read(1)
            if first_byte == b"[":
                print(f"{ts()} SUCCESS: Config updated ({live_size} bytes, clean encoding).")
            else:
                print(f"{ts()} WARNING: File copied but encoding may be wrong. Check Afterburner.")
        else:
            print(f"{ts()} ERROR: Elevated copy failed. Copy manually:")
            print(f'{ts()}   copy "{temp_path}" "{cfg_path}"')

    # ── Fix top-level ProfileN.cfg files ───────────────────────────────────
    # Afterburner checks these first. If they say ProfileContents=1, the
    # profile is treated as empty and OC settings in the per-GPU config are
    # ignored. They MUST say ProfileContents=3 for OC data to be loaded.
    # Uses read-modify-write to preserve ~27KB of monitoring/OSD config.
    print(f"{ts()} Updating top-level ProfileN.cfg files...")
    profiles_dir = os.path.dirname(cfg_path)
    for pname, _, _ in PROFILES:
        top_cfg = os.path.join(profiles_dir, f"{pname}.cfg")
        if os.path.isfile(top_cfg):
            with open(top_cfg, "r", encoding="ascii", errors="replace") as f:
                existing = f.read()
            if "ProfileContents=3" in existing:
                print(f"{ts()}   {pname}.cfg -> already OK")
                continue
            # Read-modify-write: patch only the ProfileContents line
            if re.search(r"^ProfileContents=\d+", existing, re.MULTILINE):
                patched = re.sub(r"^ProfileContents=\d+", "ProfileContents=3", existing, count=1, flags=re.MULTILINE)
            else:
                patched = existing.replace("[Settings]\r\n", "[Settings]\r\nProfileContents=3\r\n", 1)
                if patched == existing:
                    patched = existing.replace("[Settings]\n", "[Settings]\nProfileContents=3\n", 1)
        else:
            patched = "[Settings]\r\nProfileContents=3\r\n"

        temp_top = os.path.join(temp_dir, f"{pname}.cfg")
        with open(temp_top, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
            f.write(patched)
        try:
            shutil.copy2(temp_top, top_cfg)
            print(f"{ts()}   {pname}.cfg -> ProfileContents=3 (direct)")
        except PermissionError:
            ps_cmd = f"Copy-Item '{temp_top}' '{top_cfg}' -Force"
            subprocess.run(
                ["powershell", "-Command",
                 f"Start-Process powershell -Verb RunAs -ArgumentList '-Command',\"{ps_cmd}\" -Wait"],
                capture_output=True, text=True, timeout=15
            )
            print(f"{ts()}   {pname}.cfg -> ProfileContents=3 (elevated)")

    print()
    print(f"{ts()} Profile layout:")
    for pname, mem_khz, fan_pct in PROFILES:
        print(f"{ts()}   {pname}: Core {int(startup_core)/1000:.0f} (VF curve) | Mem +{mem_khz/1000:.0f} MHz | Fan {fan_pct}%")
    print()
    print(f"{ts()} Restart MSI Afterburner to apply changes.")


if __name__ == "__main__":
    main()
