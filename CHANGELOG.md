# Changelog

## Unreleased

This safety release corrects defects in the currently published tools; it is
not a claim that any clock or voltage setting is stable on a physical GPU.

### Fixed

- Replaced active-by-default profile writes with plan-only behavior and an
  explicit execution acknowledgement.
- Removed largest-config and partial-identity guessing. Profile operations now
  require an exact config path, complete GPU identity, and exact profile slot.
- Added bounds checks, expected-hash checks, verified backups, atomic per-file
  replacement, read-back verification, and rollback of earlier replacements.
- Corrected the observed V/F layout to a 12-byte header, 256 fixed-size slots,
  and preserved footer. Effective frequency is decoded as base plus offset.
- Replaced whole-blob V/F transplantation with offset-only projection onto
  each target profile's existing voltage/base grid.
- Corrected the advertised PowerShell wrappers so they parse in Windows
  PowerShell 5.1; removed the obsolete batch wrappers.
- Added a passive read-only MAHM/MACM logger with clean JSON, JSONL, and CSV
  output, strict append validation, bounds-checked mapping parsing, and the
  documented MACM snapshot mutex.
- Stopped treating MAHM CPU/system instance numbers as GPU indices.

### Validation and remaining boundary

- Local synthetic gates pass: 50 unit tests, 50 pytest cases plus 13 subtests,
  Ruff, Python compilation, PowerShell 5.1 parsing, and `git diff --check`.
- At the 2026-09-13 audit, the included GitHub Actions workflow had not run
  remotely because this batch was still uncommitted.
- No physical profile write, overclock/undervolt stability run, or destructive
  GPU test was used to validate this batch. The V/F writer supports only the
  observed `0x20000` layout and fails closed on other formats.
