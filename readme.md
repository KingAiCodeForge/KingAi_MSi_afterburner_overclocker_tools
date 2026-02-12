# MSI Afterburner Overclocker Tools

Command-line tools for reading and writing MSI Afterburner GPU profiles.  
Decode voltage-frequency (VF) curves, apply tiered overclock/undervolt profiles, and automate config management — all from the terminal.

**Compatible with NVIDIA GeForce GTX 10-series (Pascal) and newer:** GTX 1070, 1080, 1080 Ti, RTX 2060–2080 Ti, RTX 3060–3090 Ti, RTX 4060–4090, RTX 5070–5090.

> Works with any GPU that MSI Afterburner supports via per-point VF curve editing (GPU Boost 3.0+). This includes all NVIDIA cards from Pascal (2016) onward that expose voltage-frequency curves through Afterburner's config files.

---

## What These Tools Do

### `decode_vf_curve` — Read & Display VF Curves

Parses the binary VF curve data stored in MSI Afterburner's `.cfg` profile files and displays it in a human-readable table. Shows every voltage-frequency point, the per-point clock adjustment, and summary statistics.

```
$ python scripts/decode_vf_curve.py

[2026-02-12 18:30:01] Reading config: VEN_10DE&DEV_2482&...cfg
[2026-02-12 18:30:01] === Startup ===
[2026-02-12 18:30:01]   Core: 0 MHz | Mem: +0 MHz | Fan: 68% (manual)
[2026-02-12 18:30:01]   VF Curve (127 points, 12 bytes/point):
[2026-02-12 18:30:01]     [  0]    450.0 mV     250.0 MHz  (adj:    0.0)
[2026-02-12 18:30:01]     [ 72]    900.0 mV    1679.0 MHz  (adj: -205.0)  <-- undervolt clamp
[2026-02-12 18:30:01]     [126]   1237.5 mV    2050.0 MHz  (adj: -205.0)
[2026-02-12 18:30:01]   Range: 450.0–1237.5 mV | Peak: 2050.0 MHz
```

### `apply_profiles` — Write Tiered OC Profiles

Copies a validated VF curve (e.g. your Startup undervolt) to all 5 profile slots and sets per-profile memory OC + fan speed. Writes to a temp file first, then copies to Program Files with admin elevation.

```
$ python scripts/apply_profiles.py --dry-run

[2026-02-12 18:31:00] DRY RUN — no files will be modified
[2026-02-12 18:31:00] Profile1 → Core: 0 | Mem: +500 MHz | Fan: 50% | VF: Startup curve
[2026-02-12 18:31:00] Profile2 → Core: 0 | Mem: +600 MHz | Fan: 55% | VF: Startup curve
[2026-02-12 18:31:00] Profile3 → Core: 0 | Mem: +700 MHz | Fan: 60% | VF: Startup curve
[2026-02-12 18:31:00] Profile4 → Core: 0 | Mem: +800 MHz | Fan: 70% | VF: Startup curve
[2026-02-12 18:31:00] Profile5 → Core: 0 | Mem: +900 MHz | Fan: 80% | VF: Startup curve
```

---

## How It Works

### The VF Curve Format

MSI Afterburner stores GPU voltage-frequency curves as hex-encoded binary data in its per-GPU `.cfg` files. The format:

| Section | Size | Description |
|---------|------|-------------|
| **Header** | 8 bytes (16 hex chars) | `uint32 version` (always 2) + `uint32 point_count` (typically 127) |
| **Points** | 12 bytes each (24 hex chars) | 3 x `float32` per point (see below) |
| **Empty slots** | 12 bytes each | Zero-padded to 256 total slots |
| **Footer** | Variable | Fan curve data and metadata |

Each 12-byte VF point contains three IEEE 754 single-precision floats:

| Offset | Type | Field | Example |
|--------|------|-------|---------|
| 0–3 | float32 | **Clock adjustment** (MHz) | `-205.0` (matches CoreClkBoost / 1000) |
| 4–7 | float32 | **Voltage** (mV) | `900.0` (X-axis of the curve) |
| 8–11 | float32 | **Frequency** (MHz) | `1845.0` (Y-axis — the clock at that voltage) |

The 127 voltage points span **450 mV to 1237.5 mV** in **6.25 mV steps**.

### What the VF Curve Controls

NVIDIA GPUs since Pascal (GTX 10-series, 2016) use **GPU Boost 3.0/4.0** — an automatic clock management system that adjusts GPU frequency based on temperature, power draw, and voltage.

The VF curve tells the GPU: *"at this voltage, run at this frequency."*

- **Stock curve**: Higher voltages map to higher clocks, up to the GPU's max boost
- **Undervolt**: Flatten the curve at a target voltage — the GPU hits your desired frequency at lower voltage, reducing heat and power
- **Overclock**: Shift the curve upward — higher frequencies at each voltage point

When you drag points in MSI Afterburner's Ctrl+F curve editor, it modifies exactly these float values in the config file. These tools let you do the same thing from scripts.

### GPU Boost Generations

| Generation | Architecture | Cards | Key Feature |
|-----------|-------------|-------|-------------|
| **GPU Boost 3.0** | Pascal (2016) | GTX 1070, 1080, 1080 Ti, Titan X/Xp | Per-point VF curve editing via Afterburner |
| **GPU Boost 4.0** | Turing (2018) | RTX 2060–2080 Ti, GTX 1650–1660 Ti | No hard temp limit, configurable thermal plateau, OC Scanner |
| **GPU Boost 4.0** | Ampere (2020) | RTX 3060–3090 Ti | Same VF format, higher power limits |
| **GPU Boost 4.0** | Ada Lovelace (2022) | RTX 4060–4090 | Same VF format, higher clocks |
| **GPU Boost 5.0** | Blackwell (2025) | RTX 5070–5090 | Same per-point VF curve format in Afterburner |

The VF curve binary format has remained consistent across all these generations. The tools work with any card that Afterburner recognizes and creates a `VEN_10DE*.cfg` file for.

### Config File Layout

The per-GPU config file (e.g. `VEN_10DE&DEV_2482&SUBSYS_40901458&REV_A1&BUS_1&DEV_0&FN_0.cfg`) uses INI-style sections:

```ini
[Defaults]          # Factory defaults (reference only)
[Settings]          # Capture flags
[Startup]           # Applied on Afterburner launch (your "daily driver")
[Profile1]          # Slot 1 — activated by clicking "1" in Afterburner
[Profile2]          # Slot 2
[Profile3]          # Slot 3
[Profile4]          # Slot 4
[Profile5]          # Slot 5
[PreSuspendedMode]  # Saved state before sleep/hibernate
```

Each profile section contains:

| Key | Unit | Description |
|-----|------|-------------|
| `CoreClkBoost` | kHz (divide by 1000 for MHz) | Global core clock offset |
| `MemClkBoost` | kHz (divide by 1000 for MHz) | Memory clock offset |
| `CoreVoltageBoost` | mV | Voltage offset (usually 0 with VF curves) |
| `PowerLimit` | % | Power limit (100 = stock TDP) |
| `ThermalLimit` | deg C | Temperature target |
| `FanMode` | 0 or 1 | 0 = auto, 1 = manual fixed speed |
| `FanSpeed` | % | Fan speed percentage (only when FanMode=1) |
| `VFCurve` | hex string | The full voltage-frequency curve (binary, hex-encoded) |

---

## Available Scripts

Every tool has **Python**, **PowerShell**, and **Batch** versions. All produce timestamped terminal output.

| Script | Python | PowerShell | Batch |
|--------|--------|------------|-------|
| Decode VF curves | `decode_vf_curve.py` | `decode_vf_curve.ps1` | `decode_vf_curve.bat` |
| Apply tiered profiles | `apply_profiles.py` | `apply_profiles.ps1` | `apply_profiles.bat` |

### Usage

```bash
# Python (requires Python 3.8+, no pip dependencies)
python scripts/decode_vf_curve.py
python scripts/decode_vf_curve.py --config "path/to/custom.cfg"
python scripts/apply_profiles.py --dry-run
python scripts/apply_profiles.py

# PowerShell
.\scripts\decode_vf_curve.ps1
.\scripts\apply_profiles.ps1

# Batch (wraps the PowerShell scripts)
scripts\decode_vf_curve.bat
scripts\apply_profiles.bat
```

All scripts auto-detect your GPU config by scanning `C:\Program Files (x86)\MSI Afterburner\Profiles\` for `VEN_10DE*.cfg` files.

---

## Installation

1. **Clone the repo**:
   ```bash
   git clone https://github.com/KingAiCodeForge/KingAi_MSi_afterburner_overclocker_tools.git
   cd KingAi_MSi_afterburner_overclocker_tools
   ```

2. **Requirements**:
   - Windows 10/11
   - MSI Afterburner installed (tested with v4.6.4+)
   - Python 3.8+ (for `.py` scripts) — stdlib only, no pip installs needed
   - PowerShell 5.1+ (built into Windows)

3. **Run any script** from the `scripts/` folder.

---

## Understanding Your VF Curve Output

A typical decoded undervolt curve looks like this:

```
Point  Voltage     Frequency   What's happening
-----  -------     ---------   ----------------
[  0]  450.0 mV    250.0 MHz   Idle — minimum voltage and clock
[ 40]  700.0 mV   1105.0 MHz   Mid-range — clock scaling up with voltage
[ 71]  893.8 mV   1990.0 MHz   Just below your undervolt target
[ 72]  900.0 mV   1679.0 MHz   ← UNDERVOLT CLAMP: freq drops here
[ 73]  906.2 mV   2035.0 MHz   Above the clamp (GPU never reaches these)
[126] 1237.5 mV   2050.0 MHz   Maximum voltage point (never reached)
```

**The "clamp" at point 72** is where an undervolt works. By setting a **lower frequency at a specific voltage**, you tell GPU Boost: *"don't go above 900 mV."* The GPU sees that increasing voltage would **decrease** performance, so it stays at or below your chosen voltage.

Points above the clamp are technically present in the curve but the GPU has no incentive to use them — boosting to a higher voltage would mean running at a *lower* clock. GPU Boost always picks the highest-performing voltage/frequency combination.

---

## Disclaimer

**This software modifies MSI Afterburner configuration files that directly control your GPU's voltage, clock speeds, and fan behavior.**

### What Can Go Wrong

- **Unstable overclocks** cause driver crashes, application freezes, and blue screens (BSOD). Your system will recover after a reboot, but unsaved work is lost.
- **Excessive voltage or frequency** can degrade GPU silicon over extended periods. Stock voltage limits in Afterburner are set conservatively by NVIDIA, but sustained operation at maximum allowed voltage under heavy load accelerates electromigration.
- **Aggressive memory overclocks** can cause **silent data corruption** — texture glitches, flickering, or incorrect compute results — before producing an obvious crash. Always test with memory-specific benchmarks (OCCT VRAM test, HWiNFO memory error counters) not just GPU stress tests.
- **Disabling or reducing fan speed** while overclocking removes your thermal safety margin. If the GPU hits its thermal limit, it will throttle aggressively or shut down to protect itself, but sustained high temperatures reduce component lifespan.

### Safety Nets

- **Afterburner's hardware limits still apply.** These tools cannot set voltages, clocks, or power limits beyond what Afterburner and your GPU's VBIOS allow. The VF curve values are clamped by the driver.
- **Nothing is permanent.** GPU settings reset every reboot. The `.cfg` file just tells Afterburner what to apply on startup. Delete it and Afterburner regenerates stock defaults.
- **Recovery is simple.** Close Afterburner, delete the `VEN_10DE*.cfg` file from the Profiles folder, restart Afterburner. Done.

### The Silicon Lottery

Every GPU die is unique. Two identical-model cards from the same production batch can have different stable voltage and frequency limits. This is a fundamental property of semiconductor manufacturing (process variation). **What runs stable on one card may crash on another.** Start conservative, test with stress tools, and increase incrementally.

### Warranty

Overclocking may void your GPU manufacturer's warranty. NVIDIA's reference warranty excludes damage from overclocking. Check your specific card manufacturer's (ASUS, MSI, EVGA, Gigabyte, etc.) warranty terms — some are more lenient than others.

**You use these tools entirely at your own risk. The authors accept no responsibility for any hardware damage, data loss, system instability, or other consequences resulting from the use of this software.**

---

## How to Recover from a Bad Config

If Afterburner won't load correctly or your GPU is unstable:

1. **Close MSI Afterburner** completely (check system tray — right-click, Exit)
2. Navigate to `C:\Program Files (x86)\MSI Afterburner\Profiles\`
3. **Delete** the `VEN_10DE&DEV_XXXX&....cfg` file for your GPU
4. Restart MSI Afterburner — it creates a fresh config with stock defaults

Your GPU resets to stock clocks every power cycle. The config file only controls what Afterburner applies when it launches. No irreversible changes are made to hardware.

---

## Project Structure

```
KingAi_MSi_afterburner_overclocker_tools/
├── scripts/
│   ├── decode_vf_curve.py       # Python — decode & display VF curves
│   ├── decode_vf_curve.ps1      # PowerShell version
│   ├── decode_vf_curve.bat      # Batch wrapper
│   ├── apply_profiles.py        # Python — apply tiered OC profiles
│   ├── apply_profiles.ps1       # PowerShell version
│   └── apply_profiles.bat       # Batch wrapper
├── ignore/                      # Local test outputs (gitignored)
│   ├── plan.md
│   ├── outputs/
│   └── tests/
├── .gitignore
├── LICENSE
└── README.md
```

---

## Contributing

PRs welcome. If you have a different NVIDIA GPU and can verify the VF curve format, open an issue with your `decode_vf_curve.py` output so we can confirm compatibility across GPU generations.

## License

MIT — see [LICENSE](LICENSE).
