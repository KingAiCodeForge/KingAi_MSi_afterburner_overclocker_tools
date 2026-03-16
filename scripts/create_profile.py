#!/usr/bin/env python3
"""
Create or update MSI Afterburner profile(s) in a per-GPU config file.

Sets core offset, memory offset, power limit, thermal limit, fan speed,
and optionally a custom VF curve for one or more profile slots.

Handles:
  - Per-GPU .cfg file (creates [ProfileN] sections or updates existing)
  - Top-level ProfileN.cfg files (ensures ProfileContents=3)
  - Admin elevation for writing to Program Files
  - ASCII encoding with CRLF, BOM detection/stripping

Compatible with NVIDIA GTX 10-series (Pascal) and newer.

Usage:
    # Set Profile 3: undervolt curve, +600 mem, 70% fan
    python create_profile.py --profile 3 --mem 600 --fan 70 --vf-curve curve.hex

    # Set Profile 1 with flat core offset (no VF curve)
    python create_profile.py --profile 1 --core 150 --mem 500 --fan 50 --power 110

    # Set all profiles at once with tiered mem OC
    python create_profile.py --profile 1 --mem 500 --fan 50
    python create_profile.py --profile 2 --mem 600 --fan 55
    python create_profile.py --profile 3 --mem 700 --fan 60

    # Dry run (preview without writing)
    python create_profile.py --profile 3 --mem 600 --fan 70 --dry-run
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime


# ── Logging tee ─────────────────────────────────────────────────────────────
class _Tee:
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
        format_match = re.search(r"^Format=.*$", block, re.MULTILINE)
        if format_match:
            insert_pos = format_match.end()
            return block[:insert_pos] + f"\n{key}={value}" + block[insert_pos:]
        return block.rstrip("\n") + f"\n{key}={value}\n"


def get_ini_value(block: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}=(.+)$", block, re.MULTILINE)
    return match.group(1).strip() if match else None


def parse_sections(text: str) -> list[tuple[str, str, str]]:
    """Parse config into list of (header_line, section_name, section_body)."""
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


def write_to_ab(content: str, cfg_path: str, dry_run: bool = False) -> bool:
    """Write config content to the Afterburner profiles directory.

    Handles:
    - Temp file write (always writable)
    - BOM detection and stripping
    - Backup of original
    - Direct copy or admin elevation fallback

    Returns True on success.
    """
    if dry_run:
        return True

    temp_dir = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Temp")
    temp_path = os.path.join(temp_dir, "AB_profile_modified.cfg")

    # Write as ASCII, CRLF, no BOM
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
    print(f"{ts()} Written to temp: {temp_path} ({file_size} bytes, ASCII, no BOM)")

    # Backup original
    backup_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(temp_dir, f"AB_profile_backup_{backup_suffix}.cfg")
    shutil.copy2(cfg_path, backup_path)
    print(f"{ts()} Backup saved: {backup_path}")

    # Copy to Afterburner dir
    print(f"{ts()} Copying to Afterburner profile dir...")
    try:
        shutil.copy2(temp_path, cfg_path)
        print(f"{ts()} SUCCESS: Config updated directly.")
        return True
    except PermissionError:
        ps_cmd = f"Copy-Item '{temp_path}' '{cfg_path}' -Force; Start-Sleep 1"
        result = subprocess.run(
            ["powershell", "-Command",
             f"Start-Process powershell -Verb RunAs -ArgumentList '-Command',\"{ps_cmd}\" -Wait"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            live_size = os.path.getsize(cfg_path)
            with open(cfg_path, "rb") as f:
                first_byte = f.read(1)
            if first_byte == b"[":
                print(f"{ts()} SUCCESS: Config updated via elevation ({live_size} bytes).")
                return True
            else:
                print(f"{ts()} WARNING: File copied but encoding may be wrong.", file=sys.stderr)
                return False
        else:
            print(f"{ts()} ERROR: Elevated copy failed. Manual copy:", file=sys.stderr)
            print(f'{ts()}   copy "{temp_path}" "{cfg_path}"', file=sys.stderr)
            return False


def fix_profile_contents(profiles_dir: str, profile_num: int, dry_run: bool = False) -> None:
    """Ensure top-level ProfileN.cfg has ProfileContents=3.
    
    Uses read-modify-write to preserve all monitoring/OSD config (~27KB).
    Only touches the ProfileContents= line in the [Settings] section.
    """
    top_cfg = os.path.join(profiles_dir, f"Profile{profile_num}.cfg")

    if os.path.isfile(top_cfg):
        with open(top_cfg, "r", encoding="ascii", errors="replace") as f:
            existing = f.read()
        if "ProfileContents=3" in existing:
            print(f"{ts()}   Profile{profile_num}.cfg -> already OK (ProfileContents=3)")
            return
        # Read-modify-write: patch the existing ProfileContents value
        import re as _re
        if _re.search(r"^ProfileContents=\d+", existing, _re.MULTILINE):
            patched = _re.sub(r"^ProfileContents=\d+", "ProfileContents=3", existing, count=1, flags=_re.MULTILINE)
        else:
            # ProfileContents missing — add after [Settings]
            patched = existing.replace("[Settings]\r\n", "[Settings]\r\nProfileContents=3\r\n", 1)
            if patched == existing:
                patched = existing.replace("[Settings]\n", "[Settings]\nProfileContents=3\n", 1)
    else:
        # File doesn't exist — create minimal version
        patched = "[Settings]\r\nProfileContents=3\r\n"

    if dry_run:
        print(f"{ts()}   Profile{profile_num}.cfg -> WOULD set ProfileContents=3")
        return

    temp_dir = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Temp")
    temp_top = os.path.join(temp_dir, f"Profile{profile_num}.cfg")
    with open(temp_top, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
        f.write(patched)

    try:
        shutil.copy2(temp_top, top_cfg)
        print(f"{ts()}   Profile{profile_num}.cfg -> ProfileContents=3 (direct)")
    except PermissionError:
        ps_cmd = f"Copy-Item '{temp_top}' '{top_cfg}' -Force"
        subprocess.run(
            ["powershell", "-Command",
             f"Start-Process powershell -Verb RunAs -ArgumentList '-Command',\"{ps_cmd}\" -Wait"],
            capture_output=True, text=True, timeout=15
        )
        print(f"{ts()}   Profile{profile_num}.cfg -> ProfileContents=3 (elevated)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create or update MSI Afterburner profiles in a per-GPU .cfg file."
    )
    parser.add_argument(
        "--profile", "-P", type=int, required=True, choices=[1, 2, 3, 4, 5],
        help="Profile slot to write (1-5)."
    )
    parser.add_argument("--config", "-c", help="Path to per-GPU .cfg file. Auto-detects if omitted.")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Preview without writing.")

    # OC settings
    parser.add_argument("--core", type=int, default=None,
                        help="Core clock offset in MHz (e.g. 150 or -100).")
    parser.add_argument("--mem", type=int, default=None,
                        help="Memory clock offset in MHz (e.g. 500).")
    parser.add_argument("--power", type=int, default=None,
                        help="Power limit percentage (e.g. 100, 110).")
    parser.add_argument("--thermal", type=int, default=None,
                        help="Thermal limit in °C (e.g. 83).")
    parser.add_argument("--fan", type=int, default=None,
                        help="Fan speed %% (0=auto, 1-100=manual).")
    parser.add_argument("--vf-curve", default=None,
                        help="VFCurve hex blob string, OR path to a .hex file containing one.")
    parser.add_argument("--copy-startup-vf", action="store_true",
                        help="Copy VFCurve from [Startup] section (same as apply_profiles.py).")

    args = parser.parse_args()

    # Validate: at least one setting must be provided
    settings_given = any(x is not None for x in [args.core, args.mem, args.power, args.thermal, args.fan])
    if not settings_given and not args.vf_curve and not args.copy_startup_vf:
        parser.error("Provide at least one OC setting (--core, --mem, --power, --thermal, --fan, "
                     "--vf-curve, or --copy-startup-vf).")

    log_path = _init_log("create_profile")

    cfg_path = find_config(args.config)
    profiles_dir = os.path.dirname(cfg_path)
    profile_name = f"Profile{args.profile}"

    print(f"{ts()} Log file: {log_path}")
    print(f"{ts()} Config: {os.path.basename(cfg_path)}")
    print(f"{ts()} Target: [{profile_name}]")
    if args.dry_run:
        print(f"{ts()} DRY RUN — no files will be modified")
    print()

    # Read config
    with open(cfg_path, "r", encoding="ascii", errors="replace") as f:
        text = f.read()

    sections = parse_sections(text)
    section_map = {name: body for _, name, body in sections}

    # Resolve VF curve
    vf_hex = None
    if args.copy_startup_vf:
        startup_body = section_map.get("Startup", "")
        vf_match = re.search(r"VFCurve=([0-9A-Fa-f]+)", startup_body)
        if not vf_match:
            print(f"{ts()} ERROR: No VFCurve in [Startup]. Set your curve in Afterburner first.",
                  file=sys.stderr)
            sys.exit(1)
        vf_hex = vf_match.group(1)
        print(f"{ts()} Copied VFCurve from [Startup]: {len(vf_hex)} hex chars")
    elif args.vf_curve:
        # Could be a hex string or a file path
        if os.path.isfile(args.vf_curve):
            with open(args.vf_curve, "r", encoding="ascii") as f:
                vf_hex = f.read().strip()
            print(f"{ts()} Loaded VFCurve from file: {args.vf_curve} ({len(vf_hex)} hex chars)")
        else:
            vf_hex = args.vf_curve.strip()
            if not all(c in "0123456789abcdefABCDEF" for c in vf_hex):
                print(f"{ts()} ERROR: --vf-curve is not a valid hex string or file path.",
                      file=sys.stderr)
                sys.exit(1)
            print(f"{ts()} Using VFCurve from argument: {len(vf_hex)} hex chars")

    # Build the profile section
    if profile_name in section_map:
        body = section_map[profile_name]
        print(f"{ts()} Updating existing [{profile_name}] section")
    else:
        body = "\nFormat=2\n"
        print(f"{ts()} Creating new [{profile_name}] section")

    # Apply settings
    changes = []

    if args.core is not None:
        core_khz = str(args.core * 1000)
        body = set_ini_value(body, "CoreClkBoost", core_khz)
        changes.append(f"Core: {args.core:+d} MHz")

    if args.mem is not None:
        mem_khz = str(args.mem * 1000)
        body = set_ini_value(body, "MemClkBoost", mem_khz)
        changes.append(f"Mem: +{args.mem} MHz")

    if args.power is not None:
        body = set_ini_value(body, "PowerLimit", str(args.power))
        changes.append(f"Power: {args.power}%")

    if args.thermal is not None:
        body = set_ini_value(body, "ThermalLimit", str(args.thermal))
        changes.append(f"Thermal: {args.thermal}°C")

    if args.fan is not None:
        if args.fan == 0:
            body = set_ini_value(body, "FanMode", "0")
            changes.append("Fan: auto")
        else:
            body = set_ini_value(body, "FanMode", "1")
            body = set_ini_value(body, "FanSpeed", str(args.fan))
            changes.append(f"Fan: {args.fan}%")

    if vf_hex:
        body = set_ini_value(body, "VFCurve", vf_hex)
        changes.append(f"VFCurve: {len(vf_hex)} hex chars")

    # Ensure defaults for unset critical fields
    if not get_ini_value(body, "PowerLimit"):
        body = set_ini_value(body, "PowerLimit", "100")
    if not get_ini_value(body, "ThermalLimit"):
        body = set_ini_value(body, "ThermalLimit", "83")
    if not get_ini_value(body, "CoreClkBoost"):
        body = set_ini_value(body, "CoreClkBoost", "0")
    if not get_ini_value(body, "MemClkBoost"):
        body = set_ini_value(body, "MemClkBoost", "0")

    print(f"{ts()} Settings: {' | '.join(changes)}")
    print()

    # Rebuild config
    found = False
    new_sections = []
    for header, name, sec_body in sections:
        if name == profile_name:
            new_sections.append(header + body)
            found = True
        else:
            new_sections.append(header + sec_body)

    if not found:
        # Append new section at end
        new_sections.append(f"[{profile_name}]\r\n" + body)

    content = "".join(new_sections)

    if args.dry_run:
        print(f"{ts()} DRY RUN — would write {len(content)} bytes to {os.path.basename(cfg_path)}")
        print(f"{ts()} DRY RUN — would set Profile{args.profile}.cfg ProfileContents=3")
        print()
        print(f"{ts()} DRY RUN complete. No files modified.")
        return

    # Write config
    success = write_to_ab(content, cfg_path, dry_run=args.dry_run)

    if success:
        # Fix top-level ProfileN.cfg
        print(f"{ts()} Updating top-level profile metadata...")
        fix_profile_contents(profiles_dir, args.profile, dry_run=args.dry_run)

    print()
    print(f"{ts()} [{profile_name}] summary: {' | '.join(changes)}")
    print(f"{ts()} Restart MSI Afterburner to apply changes.")


if __name__ == "__main__":
    main()
