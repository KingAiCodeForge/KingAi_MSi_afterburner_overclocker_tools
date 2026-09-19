# KingAi MSI Afterburner Profile Tools

Command-line tools for inspecting and carefully editing MSI Afterburner
profile files, plus a passive live-state logger.

See [CHANGELOG.md](CHANGELOG.md) for the defects corrected by the current
unreleased safety batch.

The profile tools are offline: they do **not** talk to a GPU, Afterburner's
shared memory, NVAPI, or a driver. The separate logger opens Afterburner's
SDK-documented named mappings with read-only access; it has no control command,
writer, notification, injection, or process-hook path. No profile writer
changes a file unless the user supplies `--execute` (Python) or `-Execute`
(PowerShell).

## Safety model

The write path now enforces all of the following:

- plan-only behavior by default;
- an exact full GPU identity and exact config path;
- exact `Profile1` through `Profile5` targets;
- no directory scan, largest-file selection, or multi-GPU guessing;
- software guardrails for every supported clock, power, thermal, fan, and VF
  input;
- complete VF header, point-count, finite-number, range, and voltage-order
  validation;
- bounded effective-frequency dips, full 256-slot buffer, and
  reference-bound voltage/base/unused-slot/footer validation;
- same-directory temporary files and atomic `os.replace` operations;
- SHA-256 verification of temporary files, backups, and final targets;
- rollback of already replaced files if any later replacement fails;
- no automatic UAC prompt or hidden elevation.

The guardrails reject clearly unintended inputs. They do **not** prove that a
setting is stable or safe for a particular card. Stability still requires
incremental workload testing, temperature monitoring, and error checking.

## Requirements

- Python 3.10 or newer
- Windows PowerShell 5.1 or newer for the `.ps1` wrappers
- an MSI Afterburner profile already saved for the intended GPU and slot

No third-party Python package is required.

## Identify the exact target

The GPU identity is the complete filename stem, not just `VEN_10DE&DEV_0000`.
For example:

```text
VEN_10DE&DEV_0000&SUBSYS_00000000&REV_00&BUS_0&DEV_0&FN_0
```

The matching config must be:

```text
VEN_10DE&DEV_0000&SUBSYS_00000000&REV_00&BUS_0&DEV_0&FN_0.cfg
```

The CLI refuses a mismatch. It never chooses one of several GPU configs.

## Read a VF curve

Python:

```powershell
$gpu = "VEN_10DE&DEV_0000&SUBSYS_00000000&REV_00&BUS_0&DEV_0&FN_0"
$cfg = "C:\Program Files (x86)\MSI Afterburner\Profiles\$gpu.cfg"
python .\scripts\decode_vf_curve.py --config $cfg --gpu-id $gpu
```

Windows PowerShell 5.1 wrapper:

```powershell
.\scripts\decode_vf_curve.ps1 -ConfigPath $cfg -GpuId $gpu
.\scripts\decode_vf_curve.ps1 -ConfigPath $cfg -GpuId $gpu -ShowAll
```

This is read-only and does not create a log unless the caller redirects its
output.

## Log live state read-only

The passive shared-memory CLI can capture machine-readable monitoring data or
inspect current control/VF state without changing it:

```powershell
python .\scripts\shared_memory_cli.py capabilities
python .\scripts\shared_memory_cli.py monitor --gpu-id $gpu --format json
python .\scripts\shared_memory_cli.py control-state --gpu-id $gpu --format json
```

Stdout is the default. Its own `--output` option is plan-only unless
`--execute` is also present; in that command, `--execute` authorizes only the
log-file write, never a GPU or Afterburner control write. See
[SHARED_MEMORY_CLI.md](SHARED_MEMORY_CLI.md) for filtering, JSONL/CSV capture,
units, and error behavior.

## Plan and apply profile tiers

With no tuning arguments, the command prints this non-executable example:

| Slot | Memory offset | Manual fan |
| --- | ---: | ---: |
| Profile1 | 0 MHz | 50% |
| Profile2 | 0 MHz | 55% |
| Profile3 | 0 MHz | 60% |
| Profile4 | 0 MHz | 65% |
| Profile5 | 0 MHz | 70% |

The plan projects the validated source effective-frequency curve onto each
target's existing voltage grid by changing only its offset floats. It preserves
the target header, voltage/base fields, unused slots, and footer. Unless
`--core` is supplied, it also copies the source core offset. These source values
may themselves be unstable.
Execution therefore requires explicit memory, fan, power, and thermal inputs
plus `--acknowledge-source-profile`. Exit MSI Afterburner first and supply
`--acknowledge-afterburner-closed`; the acknowledgement is a deliberate
safety gate, not automatic process detection.

Plan only:

```powershell
python .\scripts\apply_profiles.py --config $cfg --gpu-id $gpu
```

An illustrative execute command with no memory overclock is shown below. The
numbers are not a card-specific recommendation; replace them with values
independently validated for the exact GPU:

```powershell
python .\scripts\apply_profiles.py `
  --config $cfg --gpu-id $gpu `
  --memory 0 0 0 0 0 `
  --fan 50 55 60 65 70 `
  --power 100 --thermal 83 `
  --acknowledge-source-profile `
  --acknowledge-afterburner-closed `
  --execute
```

PowerShell equivalents:

```powershell
.\scripts\apply_profiles.ps1 -ConfigPath $cfg -GpuId $gpu
.\scripts\apply_profiles.ps1 `
  -ConfigPath $cfg -GpuId $gpu `
  -Memory 0,0,0,0,0 `
  -Fan 50,55,60,65,70 `
  -Power 100 -Thermal 83 `
  -AcknowledgeSourceProfile `
  -AcknowledgeAfterburnerClosed `
  -Execute
```

Use repeated `--profile ProfileN` arguments to target fewer slots. Use
`--memory P1 P2 P3 P4 P5`, `--fan P1 P2 P3 P4 P5`, `--core`, `--power`, and
`--thermal` to customize the plan. Implicit example values cannot be executed.

## Plan and edit one exact slot

```powershell
python .\scripts\create_profile.py `
  --config $cfg `
  --gpu-id $gpu `
  --profile 1 `
  --core 80 `
  --mem 700 `
  --power 100 `
  --thermal 83 `
  --fan 60 `
  --copy-startup-vf
```

That command only prints a plan. Exit MSI Afterburner, add
`--acknowledge-afterburner-closed`, and then add `--execute` to perform the
verified transaction.

## Guardrails

These are input-validation limits, not recommended overclocks:

| Input | Accepted range |
| --- | ---: |
| Core offset | -500 to +500 MHz |
| Memory offset | -2000 to +2000 MHz |
| Power limit | 50 to 120% |
| Thermal target | 60 to 90 C |
| Manual fan | 20 to 100% |
| VF point voltage | 400 to 1300 mV |
| VF point frequency | 100 to 4000 MHz |
| VF point adjustment | -1000 to +1000 MHz |
| VF point count | 1 to 256 |

Actual firmware and driver limits can be narrower. Passing validation does not
mean a value is stable.

## Curve generation and CSV export

Both file-output tools are also plan-only by default:

```powershell
python .\scripts\encode_vf_curve.py `
  --config $cfg --gpu-id $gpu `
  --undervolt 850 1800 `
  --preview `
  --output .\curve.hex

python .\scripts\export_csv_curve.py `
  --config $cfg --gpu-id $gpu `
  --section Startup `
  --output-dir .\curve_exports
```

Add `--execute` to create the requested output. Generating a curve or CSV does
not install or apply it. Replacing an existing hex output additionally
requires `--overwrite`.

## Backups and recovery

Before changing an existing target, the transaction writes and verifies a
timestamped SHA-256-labelled `.bak`. The default directory is
`KingAiBackups` next to the selected config; `--backup-dir` can select another
location.

If a transaction fails, files already replaced in that transaction are
restored and verified. If a later manual recovery is needed:

1. Exit MSI Afterburner.
2. Identify the backup whose hash and timestamp match the intended session.
3. Preserve the current files separately.
4. Restore the per-GPU config and corresponding top-level `ProfileN.cfg`
   backups while elevated.
5. Verify hashes, then start Afterburner without automatic profile application.

Do not assume deleting a config is sufficient recovery, and do not restore a
backup belonging to a different GPU identity.

## VF format status

The parser handles the observed 12-byte little-endian
version/count/reserved header followed by 256 12-byte slots containing
voltage, base-frequency, and offset `float32` values. Effective frequency is
base plus offset. The current validator requires the observed `0x20000`
version, strictly increasing active-point voltage, bounded effective-frequency
dips, the complete 256-slot buffer, and a bounded footer. A replacement must
match the selected config's point count, voltage grid, base-frequency fields,
reserved header, unused-slot bytes, total length, and footer; only offset
floats may change. Undervolt targets must match an existing reference voltage
point.

This is an observed profile-data layout, not a public MSI compatibility
contract. Curve versions, point layouts, and trailing data can vary.
Unsupported or implausible input is rejected instead of being silently
rewritten.

The repository does not claim blanket compatibility with every Pascal,
Turing, Ampere, Ada, or Blackwell card. A new GPU/Afterburner combination
needs read-only decoding and fixture validation before write support is
claimed.

## Tests

The public tests use synthetic profile fixtures only:

```powershell
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
# Optional equivalent when pytest is installed. pytest.ini limits collection
# to the public synthetic tests directory.
python -m pytest -q
```

They cover:

- exact GPU/profile identity and refusal to guess;
- plan-only hash preservation;
- all numeric guardrails and malformed VF curves;
- exact VF header/base/voltage/unused/footer preservation and offset-only
  projection;
- explicit execution against temporary fixtures;
- backup hash/content verification;
- injected atomic-replace failure and rollback;
- bounds-checked synthetic shared-memory layouts and clean JSON/JSONL/CSV;
- Windows PowerShell 5.1 parser validation.

The test suite never reads or writes the installed MSI Afterburner directory.
The included GitHub Actions workflow is configured to run the Python suite on
multiple versions and the PowerShell parser checks on Windows. Until that
workflow is committed and pushed, those checks have only been run locally.

## Known limitations

- MSI Afterburner must be exited before an execute operation. The CLI requires
  an explicit acknowledgement because it cannot enforce process shutdown or
  lock every Afterburner version.
- Each target hash is checked against the plan before replacement and checked
  again immediately before its atomic replace. A detected external edit aborts
  the transaction instead of being overwritten.
- A replacement of one file is atomic, but Windows cannot make replacement of
  the per-GPU file and several `ProfileN.cfg` files one filesystem-wide atomic
  operation. Backups are prepared first and normal failures roll back; a power
  loss or process termination between replacements can still require manual
  recovery.
- The tests prove file-handling behavior against synthetic fixtures. They do
  not certify a physical GPU, overclock, undervolt, driver, or Afterburner
  release.
- The current VF validator accepts the observed `0x20000` format only.

## Project layout

```text
scripts/
  profile_safety.py       shared validation and transaction layer
  vf_curve_format.py      observed profile V/F codec
  decode_vf_curve.py      read-only decoder
  decode_vf_curve.ps1     PowerShell 5.1 read-only wrapper
  apply_profiles.py       tiered profile planner/writer
  apply_profiles.ps1      PowerShell 5.1 plan/execute wrapper
  create_profile.py       one-slot planner/writer
  encode_vf_curve.py      guarded curve generator
  export_csv_curve.py     guarded CSV exporter
  afterburner_shared_memory.py  read-only SDK mapping parser
  shared_memory_cli.py    passive live-state logger
tests/
  fixtures/               synthetic configs only
  test_profile_safety.py
  test_powershell_syntax.py
  test_vf_curve_format.py
  test_shared_memory.py
```

## License

MIT. See [LICENSE](LICENSE).
