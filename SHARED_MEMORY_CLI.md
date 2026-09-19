# Read-only shared-memory logger

`scripts/shared_memory_cli.py` reads the shared-memory interfaces documented by
the MSI Afterburner SDK:

- `MAHMSharedMemory` for hardware-monitoring samples;
- `MACMSharedMemory` for current control state, supported ranges, and VF-curve
  state.

The Windows mapping is opened with `FILE_MAP_READ`. MACM copies also hold the
SDK-documented `Global\Access_MACMSharedMemory` mutex for a bounded interval so
a settings update cannot tear one snapshot. This implementation has no
shared-memory writer, command notification, profile application, process
injection, or GPU-control function.

## Requirements

- Windows and Python 3.10 or newer;
- MSI Afterburner running with its shared-memory interface available;
- read access to the existing named mapping.

No third-party Python package is required. If a mapping is absent or access is
denied, the CLI exits with a structured error instead of creating a mapping or
starting Afterburner.

## Machine-facing capabilities

This command is offline and does not open either mapping:

```powershell
python .\scripts\shared_memory_cli.py capabilities
```

It prints one uncontaminated JSON object describing commands, formats, and
prohibited actions.

## Capture monitoring data

One MAHM snapshot as JSON:

```powershell
python .\scripts\shared_memory_cli.py monitor --format json
```

Sixty snapshots, one per second, as JSON Lines:

```powershell
python .\scripts\shared_memory_cli.py monitor `
  --samples 60 `
  --interval 1 `
  --format jsonl
```

CSV is available for MAHM sources:

```powershell
python .\scripts\shared_memory_cli.py monitor `
  --samples 60 `
  --format csv
```

Use a repeated `--source-id` to retain specific documented sources. For
example, `0x20` is core clock and `0x22` is memory clock:

```powershell
python .\scripts\shared_memory_cli.py monitor `
  --source-id 0x20 `
  --source-id 0x22
```

Use `--gpu-id` to require one exact full Afterburner identity. Global and
system sources are retained with the selected GPU. Each MAHM source carries a
`scope`: `gpu`, `system`, `global`, or `unresolved`. Per-core system sources
put their CPU/logical-core number in `instance_index`, never `gpu_index`:

```powershell
$gpu = "VEN_10DE&DEV_0000&SUBSYS_00000000&REV_00&BUS_0&DEV_0&FN_0"
python .\scripts\shared_memory_cli.py monitor --gpu-id $gpu
```

## Inspect control state

`control-state` is read-only despite its name. It reports MACM capability
flags, current/default/minimum/maximum values, master-GPU state, and the
available VF-curve points:

```powershell
python .\scripts\shared_memory_cli.py control-state --format json
python .\scripts\shared_memory_cli.py control-state `
  --gpu-id $gpu `
  --samples 60 `
  --format jsonl
```

Clock and VF frequencies retain the SDK's `kHz` unit. VF voltage retains
microvolts (`uV`); regular voltage controls retain millivolts (`mV`). No
conversion is hidden in the serialized data.

## Log-file safety

Stdout is the default, so normal use writes no files. Shell redirection remains
under the shell's control. The CLI's own `--output` option is plan-only until
`--execute` is present:

```powershell
python .\scripts\shared_memory_cli.py monitor `
  --samples 60 `
  --format jsonl `
  --output .\logs\gpu-telemetry.jsonl
```

That prints a plan and does not open a mapping or create a file. To create the
file:

```powershell
python .\scripts\shared_memory_cli.py monitor `
  --samples 60 `
  --format jsonl `
  --output .\logs\gpu-telemetry.jsonl `
  --execute
```

An existing file is refused unless `--append` or `--overwrite` is also
explicit. Appending is supported for JSONL and CSV, not single-object JSON.
Before appending, the CLI requires a terminal newline and validates every
existing JSONL record or CSV row against the selected format; it will not join
a new record onto a partial or corrupt tail.
The repository ignores `logs/`; shared-memory captures can contain exact GPU
identity, driver, BIOS, and machine telemetry and should not be committed as
public fixtures.

## Output and errors

- JSON is limited to one snapshot.
- JSONL emits one self-contained sample object per line.
- CSV emits one MAHM source row per sample.
- CSV includes `scope`, `gpu_index`, `gpu_id`, and `instance_index` so system
  instances cannot be mistaken for GPU records.
- JSON escapes non-ASCII characters, keeping stdout machine-readable through
  Windows PowerShell 5.1 regardless of its active console code page.
- Successful stdout contains records only, with no log banners.
- Exit `2` means an invalid CLI/file request.
- Exit `3` means the named mapping is unavailable.
- Exit `4` means the copied snapshot is malformed or unsupported.
- Exit `130` means the user interrupted capture.

Errors are one JSON object on stderr. The parser accepts documented major
version 2 layouts, honors the header-provided record sizes, bounds every
offset against the copied mapping, and refuses unsupported or inconsistent
layouts.

## Test boundary

`tests/test_shared_memory.py` creates byte fixtures in memory. It does not read
the installed Afterburner directory, open a live mapping, launch a process,
attach a debugger, inject code, or exercise a GPU setter.

## Validation status

The parser has also completed one read-only live validation against a running
Afterburner installation. That confirms that the observed mapping layouts can
be copied and decoded on that tested combination; it does not certify every
Afterburner release, GPU generation, driver, or mapping extension.

A captured clock, voltage, temperature, limiter, or control value is only a
snapshot. It does not prove that an overclock or undervolt is stable, safe, or
actually exercised under load. Treat stability as a separate workload and
error-monitoring result, and keep live captures out of public fixtures because
they can contain exact device and machine identity.
