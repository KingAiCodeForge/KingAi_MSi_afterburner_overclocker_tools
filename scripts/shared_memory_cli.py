#!/usr/bin/env python3
"""Read-only CLI for MSI Afterburner MAHM and MACM shared memory."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

from afterburner_shared_memory import (
    MAHM_MAPPING_NAME,
    MappingUnavailableError,
    SharedMemoryError,
    SnapshotFormatError,
    filter_snapshot,
    read_live_snapshot,
)


CSV_FIELDS = (
    "sample_index",
    "captured_at_utc",
    "poll_time_unix",
    "scope",
    "gpu_index",
    "gpu_id",
    "instance_index",
    "source_index",
    "source_id",
    "source_key",
    "name",
    "localized_name",
    "value",
    "unit",
    "minimum",
    "maximum",
    "available",
    "flags",
    "locations",
)


class CliError(RuntimeError):
    """A command-line request is invalid."""


class JsonArgumentParser(argparse.ArgumentParser):
    """Raise usage errors so the machine-facing entry point can serialize them."""

    def error(self, message: str) -> None:
        raise CliError(message)


def _source_id(value: str) -> int:
    try:
        parsed = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected a decimal or 0x-prefixed integer, got {value!r}"
        ) from exc
    if not 0 <= parsed <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("source ID must fit an unsigned 32-bit value")
    return parsed


def _positive_samples(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("samples must be an integer") from exc
    if not 1 <= parsed <= 1_000_000:
        raise argparse.ArgumentTypeError("samples must be between 1 and 1,000,000")
    return parsed


def _interval(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be a number") from exc
    if not 0.1 <= parsed <= 86_400:
        raise argparse.ArgumentTypeError(
            "interval must be between 0.1 and 86,400 seconds"
        )
    return parsed


def _add_common_arguments(
    parser: argparse.ArgumentParser,
    *,
    formats: tuple[str, ...],
) -> None:
    parser.add_argument(
        "--format",
        choices=formats,
        help="output encoding (default: json for one sample, jsonl otherwise)",
    )
    parser.add_argument(
        "--samples",
        type=_positive_samples,
        default=1,
        help="number of snapshots to capture (default: 1)",
    )
    parser.add_argument(
        "--interval",
        type=_interval,
        default=1.0,
        help="seconds between snapshots (default: 1.0)",
    )
    parser.add_argument(
        "--gpu-id",
        help="require and retain one exact full Afterburner GPU identity",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write records to this file; requires --execute",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="allow --output to create or replace a log file (never writes MAHM/MACM)",
    )
    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing --output file",
    )
    output_mode.add_argument(
        "--append",
        action="store_true",
        help="append JSONL/CSV records to an existing --output file",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description=(
            "Read MSI Afterburner's documented MAHM/MACM mappings with "
            "FILE_MAP_READ only. This command cannot apply GPU settings."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "capabilities",
        help="describe the stable machine-facing command contract",
    )

    monitor = subparsers.add_parser(
        "monitor",
        help="read MAHM telemetry without changing Afterburner",
    )
    _add_common_arguments(monitor, formats=("json", "jsonl", "csv"))
    monitor.add_argument(
        "--source-id",
        type=_source_id,
        action="append",
        default=[],
        help="retain this numeric MAHM source ID; may be repeated",
    )

    control = subparsers.add_parser(
        "control-state",
        help="read MACM settings, ranges, flags, and VF state without applying them",
    )
    _add_common_arguments(control, formats=("json", "jsonl"))

    return parser


def capabilities() -> dict[str, Any]:
    return {
        "schema": "kingai.afterburner.shared_memory_capabilities.v1",
        "access": "read_only",
        "commands": {
            "monitor": {
                "mapping": "MAHMSharedMemory",
                "formats": ["json", "jsonl", "csv"],
            },
            "control-state": {
                "mapping": "MACMSharedMemory",
                "formats": ["json", "jsonl"],
            },
        },
        "file_output": {
            "default": "stdout",
            "requires_execute": True,
            "existing_file_requires": ["--append", "--overwrite"],
        },
        "prohibited_actions": [
            "shared_memory_write",
            "command_notification",
            "profile_apply",
            "gpu_control",
        ],
    }


def _captured_at(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _records(
    *,
    kind: str,
    samples: int,
    interval: float,
    gpu_id: str | None,
    source_ids: set[int] | None,
    snapshot_reader: Callable[[str], dict[str, Any]],
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
) -> Iterator[dict[str, Any]]:
    for index in range(samples):
        snapshot = snapshot_reader(kind)
        snapshot = filter_snapshot(
            snapshot,
            gpu_id=gpu_id,
            source_ids=source_ids,
        )
        yield {
            "schema": "kingai.afterburner.shared_memory_sample.v1",
            "sample_index": index,
            "captured_at_utc": _captured_at(clock()),
            "snapshot": snapshot,
        }
        if index + 1 < samples:
            sleeper(interval)


def _csv_rows(record: dict[str, Any]) -> Iterator[dict[str, Any]]:
    snapshot = record["snapshot"]
    if snapshot.get("mapping") != MAHM_MAPPING_NAME:
        raise CliError("CSV output is only supported for MAHM monitoring")
    gpu_ids = {
        gpu["index"]: gpu.get("gpu_id", "")
        for gpu in snapshot.get("gpus", [])
    }
    for source in snapshot.get("sources", []):
        gpu_index = source.get("gpu_index")
        yield {
            "sample_index": record["sample_index"],
            "captured_at_utc": record["captured_at_utc"],
            "poll_time_unix": snapshot.get("poll_time_unix"),
            "scope": source.get("scope"),
            "gpu_index": "" if gpu_index is None else gpu_index,
            "gpu_id": "" if gpu_index is None else gpu_ids.get(gpu_index, ""),
            "instance_index": (
                ""
                if source.get("instance_index") is None
                else source.get("instance_index")
            ),
            "source_index": source.get("index"),
            "source_id": source.get("source_id"),
            "source_key": source.get("source_key"),
            "name": source.get("name"),
            "localized_name": source.get("localized_name"),
            "value": source.get("value"),
            "unit": source.get("unit"),
            "minimum": source.get("minimum"),
            "maximum": source.get("maximum"),
            "available": source.get("available"),
            "flags": source.get("flags"),
            "locations": "|".join(source.get("locations", [])),
        }


def _json_line(value: Any) -> str:
    return json.dumps(
        value,
        # ASCII escapes survive Windows PowerShell 5.1's external-process
        # decoding regardless of the active ANSI/OEM code page.
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_output_options(args: argparse.Namespace, output_format: str) -> None:
    if args.execute and args.output is None:
        raise CliError("--execute is only valid together with --output")
    if (args.overwrite or args.append) and args.output is None:
        raise CliError("--overwrite and --append require --output")
    if (args.overwrite or args.append) and not args.execute:
        raise CliError("--overwrite and --append require --execute")
    if args.append and output_format == "json":
        raise CliError("--append requires --format jsonl or csv")
    if output_format == "json" and args.samples != 1:
        raise CliError("multiple samples require --format jsonl or csv")
    if args.output is not None and args.output.name in {"", ".", ".."}:
        raise CliError("--output must name a file")


def _open_output(
    path: Path,
    *,
    overwrite: bool,
    append: bool,
) -> TextIO:
    parent = path.expanduser().resolve().parent
    if not parent.is_dir():
        raise CliError(f"output parent directory does not exist: {parent}")
    mode = "a" if append else "w" if overwrite else "x"
    try:
        return path.open(mode, encoding="utf-8", newline="")
    except FileExistsError as exc:
        raise CliError(
            f"output already exists: {path}; use --append or --overwrite explicitly"
        ) from exc
    except OSError as exc:
        raise CliError(f"could not open output {path}: {exc}") from exc


def _preflight_output(
    path: Path,
    *,
    output_format: str,
    mapping_name: str,
    overwrite: bool,
    append: bool,
) -> None:
    parent = path.expanduser().resolve().parent
    if not parent.is_dir():
        raise CliError(f"output parent directory does not exist: {parent}")
    if path.exists() and not path.is_file():
        raise CliError(f"output is not a regular file: {path}")
    if path.exists() and not (overwrite or append):
        raise CliError(
            f"output already exists: {path}; use --append or --overwrite explicitly"
        )
    if not append or not path.exists() or path.stat().st_size == 0:
        return
    try:
        with path.open("rb") as binary:
            binary.seek(-1, 2)
            if binary.read(1) != b"\n":
                raise CliError(
                    "existing append target does not end with a newline"
                )
        with path.open("r", encoding="utf-8", newline="") as existing:
            if output_format == "csv":
                reader = csv.reader(existing)
                try:
                    header = next(reader)
                except StopIteration as exc:
                    raise CliError("existing CSV log is empty") from exc
                if tuple(header) != CSV_FIELDS:
                    raise CliError(
                        "existing CSV header does not match this logger's schema"
                    )
                for row_number, row in enumerate(reader, start=2):
                    if len(row) != len(CSV_FIELDS):
                        raise CliError(
                            f"existing CSV row {row_number} has {len(row)} "
                            f"fields; expected {len(CSV_FIELDS)}"
                        )
                return

            for line_number, line in enumerate(existing, start=1):
                if len(line) > 1_048_576:
                    raise CliError(
                        f"existing JSONL record {line_number} exceeds 1 MiB"
                    )
                if not line.endswith("\n"):
                    raise CliError(
                        f"existing JSONL record {line_number} is incomplete"
                    )
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CliError(
                        f"existing JSONL record {line_number} is not valid JSON"
                    ) from exc
                if (
                    not isinstance(record, dict)
                    or record.get("schema")
                    != "kingai.afterburner.shared_memory_sample.v1"
                ):
                    raise CliError(
                        f"existing JSONL record {line_number} uses an "
                        "incompatible schema"
                    )
                snapshot = record.get("snapshot")
                if (
                    not isinstance(snapshot, dict)
                    or snapshot.get("mapping") != mapping_name
                ):
                    raise CliError(
                        f"existing JSONL record {line_number} is not a "
                        f"{mapping_name} capture"
                    )
    except (UnicodeError, csv.Error) as exc:
        raise CliError(f"could not parse append target {path}: {exc}") from exc
    except OSError as exc:
        raise CliError(f"could not validate append target {path}: {exc}") from exc


def _write_records(
    records: Iterator[dict[str, Any]],
    *,
    output_format: str,
    stream: TextIO,
    append_to_nonempty: bool,
) -> int:
    count = 0
    if output_format == "csv":
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
        if not append_to_nonempty:
            writer.writeheader()
        for record in records:
            for row in _csv_rows(record):
                writer.writerow(row)
            stream.flush()
            count += 1
        return count

    for record in records:
        stream.write(_json_line(record))
        stream.write("\n")
        stream.flush()
        count += 1
    return count


def _run_capture(
    args: argparse.Namespace,
    *,
    snapshot_reader: Callable[[str], dict[str, Any]],
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
) -> int:
    kind = "mahm" if args.command == "monitor" else "macm"
    output_format = args.format or ("json" if args.samples == 1 else "jsonl")
    _validate_output_options(args, output_format)

    if args.output is not None and not args.execute:
        plan = {
            "schema": "kingai.afterburner.shared_memory_file_plan.v1",
            "mode": "plan",
            "mapping": "MAHMSharedMemory" if kind == "mahm" else "MACMSharedMemory",
            "output": str(args.output),
            "format": output_format,
            "samples": args.samples,
            "message": "No mapping was opened and no file was written; add --execute.",
        }
        sys.stdout.write(_json_line(plan) + "\n")
        return 0

    source_ids = set(args.source_id) if args.command == "monitor" else None
    if args.output is not None:
        _preflight_output(
            args.output.expanduser(),
            output_format=output_format,
            mapping_name=(
                "MAHMSharedMemory" if kind == "mahm" else "MACMSharedMemory"
            ),
            overwrite=args.overwrite,
            append=args.append,
        )
    records = _records(
        kind=kind,
        samples=args.samples,
        interval=args.interval,
        gpu_id=args.gpu_id,
        source_ids=source_ids,
        snapshot_reader=snapshot_reader,
        sleeper=sleeper,
        clock=clock,
    )

    if args.output is None:
        _write_records(
            records,
            output_format=output_format,
            stream=sys.stdout,
            append_to_nonempty=False,
        )
        return 0

    # Acquire one valid sample before creating a destination. A missing or
    # malformed live mapping therefore cannot leave an empty log behind.
    try:
        first = next(records)
    except StopIteration as exc:  # samples is validated, so this is defensive
        raise CliError("no samples were requested") from exc

    output_path = args.output.expanduser()
    append_to_nonempty = args.append and output_path.exists() and output_path.stat().st_size
    stream = _open_output(
        output_path,
        overwrite=args.overwrite,
        append=args.append,
    )
    try:
        count = _write_records(
            chain((first,), records),
            output_format=output_format,
            stream=stream,
            append_to_nonempty=bool(append_to_nonempty),
        )
    finally:
        stream.close()

    summary = {
        "schema": "kingai.afterburner.shared_memory_file_result.v1",
        "mode": "executed",
        "output": str(output_path),
        "format": output_format,
        "samples_written": count,
    }
    sys.stdout.write(_json_line(summary) + "\n")
    return 0


def main(
    argv: list[str] | None = None,
    *,
    snapshot_reader: Callable[[str], dict[str, Any]] = read_live_snapshot,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "capabilities":
            sys.stdout.write(_json_line(capabilities()) + "\n")
            return 0
        return _run_capture(
            args,
            snapshot_reader=snapshot_reader,
            sleeper=sleeper,
            clock=clock,
        )
    except KeyboardInterrupt:
        sys.stderr.write(
            _json_line(
                {
                    "error": {
                        "code": "interrupted",
                        "message": "capture interrupted",
                    }
                }
            )
            + "\n"
        )
        return 130
    except MappingUnavailableError as error:
        code = "mapping_unavailable"
        exit_code = 3
        message = str(error)
    except SnapshotFormatError as error:
        code = "invalid_snapshot"
        exit_code = 4
        message = str(error)
    except (CliError, SharedMemoryError, OSError) as error:
        code = "invalid_request"
        exit_code = 2
        message = str(error)
    sys.stderr.write(
        _json_line(
            {
                "error": {
                    "code": code,
                    "message": message,
                }
            }
        )
        + "\n"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
