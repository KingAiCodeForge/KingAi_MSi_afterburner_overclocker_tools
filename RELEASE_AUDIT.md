# Offline tools v0.1.0 audit (2026-10-20)

This release qualifies offline file behavior with synthetic inputs. No installed
Afterburner profiles, GPU setting writes, live mappings, driver calls, physical
stability tests, or hardware compatibility tests were used. The live adapter is
inspected source with synthetic parser tests, not a hardware-qualified release.

## Source preservation

| Source | Exact commit | Status |
| --- | --- | --- |
| Original main | `694a79cacfdf4b90de5b67be36c72d2703f6072f` | Preserved as `preserve/20261020-main` |
| Scoped edit parent | `bcbe16f10cb292caa8906d260b499d60c0bee495` | Included as ancestor |
| Scoped edit plus regressions | `3e1cfa2c34c8989015d59d9a65f44f604a270d49` | Preserved as `preserve/20261020-msi-scoped-edit`; original chatgpt branch retained |
| Initial Python/PowerShell/batch variants | `78b1d2003d92aeaafa7988e95e566265ced138f3` | Historical source only; reachable through preserved main |
| Earlier path-detection/VF prototypes | `435519ae1b8b5323b47183acfd6650d9f558dbb8` | Historical source only; reachable through preserved main |

Historical batch launchers and active-by-default writers are unsupported. Recover
their exact original paths with `git show <commit>:scripts/<filename>` for study;
they are not copied into the supported release. Original public refs were also
captured in a verified local all-refs bundle before integration. No private
repository or ignored local research was imported.

## Per-script review ledger

Every current script/module was inspected. "Tested" below means the named
synthetic tests or CLI checks, not every possible code path.

| Path under scripts/ | Purpose and CLI | Change in this integration | Qualification and boundary |
| --- | --- | --- | --- |
| `_analyze_config.py` | Exact-file read-only summary; `--config --gpu-id`, optional sections/JSON | Inspected, unchanged | Outside-checkout fixture summary, missing input, help; duplicate-section/value helpers covered |
| `_compare_generations.py` | Compare explicit label/identity/config triples via repeated `--input` | Inspected, unchanged | Outside-checkout synthetic comparison and help; no scanning or guessed paths |
| `apply_profiles.py` | Plan five or selected slots; explicit acknowledgements and `--execute` required to write | Inspected, unchanged; uses corrected shared scoping helper | Existing plan hash preservation, numeric/VF rejection, verified temporary-fixture execution/backups/rollback; outside-checkout plan |
| `apply_profiles.ps1` | PowerShell 5.1 wrapper, paths anchored at `$PSScriptRoot` | Missing Python now exits 2 | PowerShell 5.1 parser and plan test; missing dependency on copied scripts in spaces/non-ASCII path |
| `create_profile.py` | Exact one-slot planner/writer with explicit execution acknowledgement | Inspected, unchanged; uses corrected shared scoping helper | Synthetic plan/execute/acknowledgement/invalid-input cases; outside-checkout plan and missing file |
| `decode_vf_curve.py` | Read-only human-readable curve decoder | Strict ASCII read; file/value errors return a diagnostic without traceback | Synthetic decode outside checkout and missing file; reader reports malformed curves; no writes |
| `decode_vf_curve.ps1` | PowerShell 5.1 read-only wrapper | Missing Python now exits 2 | PowerShell parser and copied-script dependency error checks |
| `encode_vf_curve.py` | Reference-bound offset/plateau/CSV generator; file output plan-only | Reject config/CSV input aliases including hard links even with overwrite; strict reads; clean I/O errors; execute requires output | Existing reference/VF/plan tests plus exact/normalized/hard-link alias regressions and outside-checkout plan |
| `export_csv_curve.py` | CSV file generator; plan-only unless executed | Limit sections to known slots; strict ASCII and clean I/O/value errors; defer output directory creation until a valid export | Existing export/plan tests and outside-checkout plan/missing file; exclusive output creation refuses collisions. Multiple CSV outputs are independent files, not one atomic transaction |
| `profile_safety.py` | Exact identity, numeric/VF/INI validation, verified backup/atomic replace/rollback | Integrate Settings-only edit; refuse ambiguous heading/key spellings; shared export alias check | Scope cases including CRLF/duplicates/unrelated sections; existing race/rollback/backup tests; hard-link alias regression. Canonical plain INI headings/keys required |
| `vf_curve_format.py` | Pure observed `0x20000` fixed-buffer codec | Inspected, unchanged | Synthetic decode/roundtrip/offset-byte preservation and malformed/unknown version rejection |
| `afterburner_shared_memory.py` | Pure MAHM/MACM parsers plus Windows read-only mapping adapter | Inspected, unchanged | Synthetic layout bounds, truncation, identity, source-scope, ANSI tests. Windows OS adapter/mutex path inspected but not executed against a live mapping |
| `shared_memory_cli.py` | Capabilities and passive capture; log output requires execute | Inspected, unchanged | Injected snapshot reader tests for JSON/JSONL/CSV, append validation, output plan and mapping failure. No live capture invoked; log overwrite remains an explicit caller choice |

No current script is left unreviewed. Historical variants are preserved but
excluded from support and qualification. Native testing was Windows x64,
Python 3.13 and Windows PowerShell 5.1. Other platforms/versions are qualified
only by the fresh GitHub Actions results attached to the integration; live
MSI/driver/GPU combinations remain untested.

## Reproduction

The runtime uses Python's standard library only; unzip the complete source
artifact and keep sibling files in `scripts/` together. No installation into
Afterburner, registry, or a package registry is needed. Invoke scripts with
absolute paths from any working directory; CLI help is available via `--help`.

```powershell
python -m venv --without-pip clean-env
.\clean-env\Scripts\python.exe -m unittest discover -s tests -v
.\clean-env\Scripts\python.exe -m compileall -q scripts tests
# Optional development lint, separate from the stdlib-only runtime:
python -m ruff check scripts tests
git diff --check
```

The local gate ran 70 unittest cases, all passing, including Windows wrapper
checks and hard-link tests. No fixture or source files were modified by plans.
Fresh artifact extraction/venv testing and remote CI receipts are recorded in
the GitHub release notes alongside exact source commit and SHA-256 checksums.

## Exposure and recovery review

Tracked source, synthetic fixture provenance, test files, workflows and docs
were reviewed. No personal profile, credential, local config, or capture is
part of the release. The repository had no custom local/global excludes.
`ignore/`, private tests, local configs/logs, environments and build outputs
stay excluded. Real `*.cfg`, `*.bak` and `KingAiBackups/` are now ignored;
only the explicitly labelled, user-owned `tests/fixtures/profiles/*.cfg`
examples are allowlisted. Existing tracked public synthetic fixtures remain
tracked; ignoring files is not treated as removal from history.

Backups are never deleted by this integration. Versioned release archives are
built from `git archive` of the reviewed merge commit, not from a working
directory, so ignored data and test-generated caches cannot enter artifacts.
Recovery and transaction limitations remain documented in `readme.md`.
