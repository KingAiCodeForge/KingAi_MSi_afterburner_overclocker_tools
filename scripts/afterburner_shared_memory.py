"""Read-only parsing for MSI Afterburner's documented shared-memory layouts.

The installed MSI Afterburner SDK documents two named mappings:

* ``MAHMSharedMemory`` contains hardware-monitoring samples.
* ``MACMSharedMemory`` contains the current control state and supported ranges.

This module opens either mapping with ``FILE_MAP_READ`` only.  It deliberately
contains no command notification, mutex write, profile application, or other
hardware-control path.
"""

from __future__ import annotations

import ctypes
import math
import os
import struct
from ctypes import wintypes
from typing import Any, Callable


MAHM_MAPPING_NAME = "MAHMSharedMemory"
MACM_MAPPING_NAME = "MACMSharedMemory"
MACM_MUTEX_NAME = r"Global\Access_MACMSharedMemory"
MACM_MUTEX_TIMEOUT_MS = 5_000

MAHM_SIGNATURE = 0x4D41484D
MACM_SIGNATURE = 0x4D41434D
DEAD_SIGNATURE = 0x0000DEAD

MAX_PATH_CHARS = 260
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 65_536

_MAHM_HEADER = struct.Struct("<8I")
_MAHM_ENTRY = struct.Struct("<260s260s260s260s260sfffIII")
_MAHM_GPU_ENTRY = struct.Struct("<260s260s260s260s260sI")
_MACM_HEADER = struct.Struct("<9I")

_MAHM_SOURCE_NAMES = {
    0x00000000: "gpu_temperature",
    0x00000001: "pcb_temperature",
    0x00000002: "memory_temperature",
    0x00000003: "vrm_temperature",
    0x00000010: "fan_speed",
    0x00000011: "fan_tachometer",
    0x00000012: "fan_speed_2",
    0x00000013: "fan_tachometer_2",
    0x00000014: "fan_speed_3",
    0x00000015: "fan_tachometer_3",
    0x00000020: "core_clock",
    0x00000021: "shader_clock",
    0x00000022: "memory_clock",
    0x00000030: "gpu_usage",
    0x00000031: "memory_usage",
    0x00000032: "framebuffer_usage",
    0x00000033: "video_engine_usage",
    0x00000034: "bus_usage",
    0x00000035: "process_memory_usage",
    0x00000040: "gpu_voltage",
    0x00000041: "aux_voltage",
    0x00000042: "memory_voltage",
    0x00000043: "aux2_voltage",
    0x00000050: "framerate",
    0x00000051: "frametime",
    0x00000052: "framerate_min",
    0x00000053: "framerate_average",
    0x00000054: "framerate_max",
    0x00000055: "framerate_1_percent_low",
    0x00000056: "framerate_0_1_percent_low",
    0x00000060: "gpu_relative_power",
    0x00000061: "gpu_absolute_power",
    0x00000070: "gpu_temperature_limit",
    0x00000071: "gpu_power_limit",
    0x00000072: "gpu_voltage_limit",
    0x00000074: "gpu_utilization_limit",
    0x00000075: "gpu_sli_sync_limit",
    0x00000080: "cpu_temperature",
    0x00000090: "cpu_usage",
    0x00000091: "ram_usage",
    0x00000092: "pagefile_usage",
    0x00000093: "process_ram_usage",
    0x000000A0: "cpu_clock",
    0x000000B0: "gpu_temperature_2",
    0x000000B1: "pcb_temperature_2",
    0x000000B2: "memory_temperature_2",
    0x000000B3: "vrm_temperature_2",
    0x000000C0: "gpu_temperature_3",
    0x000000C1: "pcb_temperature_3",
    0x000000C2: "memory_temperature_3",
    0x000000C3: "vrm_temperature_3",
    0x000000D0: "gpu_temperature_4",
    0x000000D1: "pcb_temperature_4",
    0x000000D2: "memory_temperature_4",
    0x000000D3: "vrm_temperature_4",
    0x000000E0: "gpu_temperature_5",
    0x000000E1: "pcb_temperature_5",
    0x000000E2: "memory_temperature_5",
    0x000000E3: "vrm_temperature_5",
    0x000000F0: "plugin_gpu",
    0x000000F1: "plugin_cpu",
    0x000000F2: "plugin_motherboard",
    0x000000F3: "plugin_ram",
    0x000000F4: "plugin_storage",
    0x000000F5: "plugin_network",
    0x000000F6: "plugin_psu",
    0x000000F7: "plugin_ups",
    0x000000FF: "plugin_misc",
    0x00000100: "cpu_power",
}

_MAHM_LOCATION_FLAGS = {
    0x00000001: "osd",
    0x00000002: "lcd",
    0x00000004: "tray",
}

# The SDK's MAHM sample treats these as system sources before interpreting
# dwGpu.  For per-core CPU sources, dwGpu is the CPU/logical-core instance
# index, not an index into the GPU-entry array.
_MAHM_SYSTEM_SOURCE_IDS = {
    0x00000050,  # framerate
    0x00000051,  # frametime
    0x00000052,  # framerate minimum
    0x00000053,  # framerate average
    0x00000054,  # framerate maximum
    0x00000055,  # framerate 1% low
    0x00000056,  # framerate 0.1% low
    0x00000080,  # CPU temperature
    0x00000090,  # CPU usage
    0x00000091,  # RAM usage
    0x00000092,  # pagefile usage
    0x00000093,  # process RAM usage
    0x000000A0,  # CPU clock
    0x000000F1,  # CPU plugin
    0x000000F2,  # motherboard plugin
    0x000000F3,  # RAM plugin
    0x000000F4,  # storage plugin
    0x000000F5,  # network plugin
    0x000000F6,  # PSU plugin
    0x000000F7,  # UPS plugin
    0x000000FF,  # miscellaneous plugin
    0x00000100,  # CPU power
}

_MAHM_GPU_SOURCE_IDS = {
    *_MAHM_SOURCE_NAMES.keys(),
} - _MAHM_SYSTEM_SOURCE_IDS

_MACM_HEADER_FLAGS = {
    0x00000001: "linked",
    0x00000002: "synchronized",
    0x00000004: "thermal_linked",
}

_MACM_CAPABILITY_FLAGS = {
    0x00000001: "core_clock",
    0x00000002: "shader_clock",
    0x00000004: "memory_clock",
    0x00000008: "fan_speed",
    0x00000010: "core_voltage",
    0x00000020: "memory_voltage",
    0x00000040: "aux_voltage",
    0x00000080: "core_voltage_boost",
    0x00000100: "memory_voltage_boost",
    0x00000200: "aux_voltage_boost",
    0x00000400: "power_limit",
    0x00000800: "core_clock_boost",
    0x00001000: "memory_clock_boost",
    0x00002000: "thermal_limit",
    0x00004000: "thermal_prioritize",
    0x00008000: "aux2_voltage",
    0x00010000: "aux2_voltage_boost",
    0x00020000: "vf_curve",
    0x00040000: "vf_curve_enabled",
    0x80000000: "synchronized_with_master",
}

_MACM_COMMANDS = {
    0: "idle",
    0x00AB0000: "init",
    0x00AB0001: "flush",
    0x00AB0002: "flush_without_applying",
    0x00AB0003: "refresh_vf_curve",
}

_MACM_CONTROL_LAYOUT = (
    ("core_clock_khz", 4, False, "kHz", 0x00000001),
    ("shader_clock_khz", 20, False, "kHz", 0x00000002),
    ("memory_clock_khz", 36, False, "kHz", 0x00000004),
    ("core_voltage_mv", 76, False, "mV", 0x00000010),
    ("memory_voltage_mv", 92, False, "mV", 0x00000020),
    ("aux_voltage_mv", 108, False, "mV", 0x00000040),
    ("core_voltage_boost_mv", 124, True, "mV", 0x00000080),
    ("memory_voltage_boost_mv", 140, True, "mV", 0x00000100),
    ("aux_voltage_boost_mv", 156, True, "mV", 0x00000200),
    ("power_limit_percent", 172, True, "%", 0x00000400),
    ("core_clock_boost_khz", 188, True, "kHz", 0x00000800),
    ("memory_clock_boost_khz", 204, True, "kHz", 0x00001000),
    ("thermal_limit_c", 220, True, "C", 0x00002000),
    ("aux2_voltage_mv", 244, False, "mV", 0x00008000),
    ("aux2_voltage_boost_mv", 260, True, "mV", 0x00010000),
)


class SharedMemoryError(RuntimeError):
    """Base error for read-only shared-memory access."""


class MappingUnavailableError(SharedMemoryError):
    """The requested named mapping is not available for read access."""


class SnapshotFormatError(SharedMemoryError):
    """A shared-memory snapshot is structurally invalid or unsupported."""


def _labels(value: int, labels: dict[int, str]) -> list[str]:
    return [label for bit, label in labels.items() if value & bit]


def _version(value: int) -> dict[str, int]:
    return {
        "raw": value,
        "major": value >> 16,
        "minor": value & 0xFFFF,
    }


def _decode_ansi(raw: bytes) -> str:
    terminated = raw.split(b"\0", 1)[0]
    if not terminated:
        return ""
    try:
        return terminated.decode("utf-8")
    except UnicodeDecodeError:
        if os.name == "nt":
            try:
                return terminated.decode("mbcs", errors="strict")
            except UnicodeDecodeError:
                # A system configured to use UTF-8 as its ANSI code page can
                # still receive legacy single-byte strings from Afterburner.
                pass
        return terminated.decode("cp1252", errors="replace")


def _finite_or_none(value: float) -> float | None:
    if not math.isfinite(value) or value >= 3.402823466e38:
        return None
    return value


def _checked_total(parts: tuple[int, ...], *, kind: str) -> int:
    total = sum(parts)
    if any(part < 0 for part in parts) or total > MAX_SNAPSHOT_BYTES:
        raise SnapshotFormatError(
            f"{kind} declares an invalid or oversized {total}-byte snapshot"
        )
    return total


def _validate_version(raw_version: int, *, kind: str) -> tuple[int, int]:
    major = raw_version >> 16
    minor = raw_version & 0xFFFF
    if major != 2:
        raise SnapshotFormatError(
            f"{kind} version {major}.{minor} is unsupported; only major version 2 "
            "has a documented compatible layout"
        )
    return major, minor


def _mahm_layout(data: bytes) -> tuple[tuple[int, ...], int]:
    if len(data) < _MAHM_HEADER.size:
        raise SnapshotFormatError("MAHM snapshot is shorter than its v2 header")
    header = _MAHM_HEADER.unpack_from(data)
    (
        signature,
        version,
        header_size,
        entry_count,
        entry_size,
        _poll_time,
        gpu_count,
        gpu_entry_size,
    ) = header
    if signature == DEAD_SIGNATURE:
        raise SnapshotFormatError("MAHM mapping is marked dead")
    if signature != MAHM_SIGNATURE:
        raise SnapshotFormatError(
            f"MAHM signature mismatch: expected 0x{MAHM_SIGNATURE:08X}, "
            f"got 0x{signature:08X}"
        )
    _validate_version(version, kind="MAHM")
    if header_size < _MAHM_HEADER.size:
        raise SnapshotFormatError("MAHM header size is smaller than the v2 header")
    if entry_count > MAX_RECORDS or gpu_count > MAX_RECORDS:
        raise SnapshotFormatError("MAHM declares too many entries")
    if entry_count and entry_size < _MAHM_ENTRY.size:
        raise SnapshotFormatError("MAHM monitoring entry size is too small")
    if gpu_count and gpu_entry_size < _MAHM_GPU_ENTRY.size:
        raise SnapshotFormatError("MAHM GPU entry size is too small")
    total = _checked_total(
        (
            header_size,
            entry_count * entry_size,
            gpu_count * gpu_entry_size,
        ),
        kind="MAHM",
    )
    return header, total


def parse_mahm_snapshot(data: bytes) -> dict[str, Any]:
    """Parse a copied MAHM byte snapshot without accessing live memory."""

    header, total = _mahm_layout(data)
    if len(data) < total:
        raise SnapshotFormatError(
            f"MAHM snapshot is truncated: needs {total} bytes, has {len(data)}"
        )
    (
        _signature,
        version,
        header_size,
        entry_count,
        entry_size,
        poll_time,
        gpu_count,
        gpu_entry_size,
    ) = header

    sources: list[dict[str, Any]] = []
    for index in range(entry_count):
        offset = header_size + index * entry_size
        (
            source_name,
            source_units,
            localized_name,
            localized_units,
            recommended_format,
            value,
            minimum,
            maximum,
            flags,
            gpu_index,
            source_id,
        ) = _MAHM_ENTRY.unpack_from(data, offset)

        if source_id in _MAHM_SYSTEM_SOURCE_IDS:
            scope = "system"
            parsed_gpu_index = None
            instance_index = None if gpu_index == 0xFFFFFFFF else gpu_index
        elif source_id not in _MAHM_GPU_SOURCE_IDS:
            scope = "global" if gpu_index == 0xFFFFFFFF else "unresolved"
            parsed_gpu_index = None
            instance_index = None if gpu_index == 0xFFFFFFFF else gpu_index
        elif gpu_index < gpu_count:
            scope = "gpu"
            parsed_gpu_index = gpu_index
            instance_index = None
        else:
            raise SnapshotFormatError(
                f"MAHM source {index} refers to missing GPU index {gpu_index}"
            )

        clean_value = _finite_or_none(value)
        sources.append(
            {
                "index": index,
                "source_id": source_id,
                "source_key": _MAHM_SOURCE_NAMES.get(
                    source_id, f"unknown_0x{source_id:08x}"
                ),
                "scope": scope,
                "gpu_index": parsed_gpu_index,
                "instance_index": instance_index,
                "name": _decode_ansi(source_name),
                "localized_name": _decode_ansi(localized_name),
                "unit": _decode_ansi(source_units),
                "localized_unit": _decode_ansi(localized_units),
                "recommended_format": _decode_ansi(recommended_format),
                "value": clean_value,
                "available": clean_value is not None,
                "minimum": _finite_or_none(minimum),
                "maximum": _finite_or_none(maximum),
                "flags": flags,
                "locations": _labels(flags, _MAHM_LOCATION_FLAGS),
            }
        )

    gpu_base = header_size + entry_count * entry_size
    gpus: list[dict[str, Any]] = []
    for index in range(gpu_count):
        offset = gpu_base + index * gpu_entry_size
        gpu_id, family, device, driver, bios, memory_kib = (
            _MAHM_GPU_ENTRY.unpack_from(data, offset)
        )
        gpus.append(
            {
                "index": index,
                "gpu_id": _decode_ansi(gpu_id),
                "family": _decode_ansi(family),
                "device": _decode_ansi(device),
                "driver": _decode_ansi(driver),
                "bios": _decode_ansi(bios),
                "memory_kib": memory_kib,
            }
        )

    return {
        "schema": "kingai.afterburner.mahm.v1",
        "mapping": MAHM_MAPPING_NAME,
        "access": "read_only",
        "version": _version(version),
        "poll_time_unix": poll_time,
        "layout": {
            "header_size": header_size,
            "entry_size": entry_size,
            "gpu_entry_size": gpu_entry_size,
        },
        "gpus": gpus,
        "sources": sources,
    }


def _macm_minimum_entry_size(minor: int) -> int:
    if minor >= 3:
        return 3760
    if minor == 2:
        return 276
    if minor == 1:
        return 244
    return 220


def _macm_layout(data: bytes) -> tuple[tuple[int, ...], int]:
    if len(data) < _MACM_HEADER.size:
        raise SnapshotFormatError("MACM snapshot is shorter than its v2 header")
    header = _MACM_HEADER.unpack_from(data)
    (
        signature,
        version,
        header_size,
        gpu_count,
        gpu_entry_size,
        master_gpu,
        _flags,
        _state_time,
        _command,
    ) = header
    if signature == DEAD_SIGNATURE:
        raise SnapshotFormatError("MACM mapping is marked dead")
    if signature != MACM_SIGNATURE:
        raise SnapshotFormatError(
            f"MACM signature mismatch: expected 0x{MACM_SIGNATURE:08X}, "
            f"got 0x{signature:08X}"
        )
    _major, minor = _validate_version(version, kind="MACM")
    if header_size < _MACM_HEADER.size:
        raise SnapshotFormatError("MACM header size is smaller than the v2 header")
    if gpu_count > MAX_RECORDS:
        raise SnapshotFormatError("MACM declares too many GPU entries")
    if gpu_count and gpu_entry_size < _macm_minimum_entry_size(minor):
        raise SnapshotFormatError(
            f"MACM v2.{minor} GPU entry size {gpu_entry_size} is too small"
        )
    if gpu_count and master_gpu >= gpu_count:
        raise SnapshotFormatError(
            f"MACM master GPU index {master_gpu} is outside {gpu_count} entries"
        )
    total = _checked_total(
        (header_size, gpu_count * gpu_entry_size),
        kind="MACM",
    )
    return header, total


def _read_control(
    data: bytes,
    entry_offset: int,
    field_offset: int,
    *,
    signed: bool,
    unit: str,
) -> dict[str, Any]:
    code = "<4i" if signed else "<4I"
    current, minimum, maximum, default = struct.unpack_from(
        code, data, entry_offset + field_offset
    )
    return {
        "unit": unit,
        "current": current,
        "minimum": minimum,
        "maximum": maximum,
        "default": default,
    }


def _parse_vf_curve(data: bytes, entry_offset: int) -> dict[str, Any]:
    vf_offset = entry_offset + 276
    version, flags, point_count = struct.unpack_from("<3I", data, vf_offset)
    if point_count > 256:
        raise SnapshotFormatError(
            f"MACM VF curve declares {point_count} points; maximum is 256"
        )
    points: list[dict[str, int]] = []
    point_base = vf_offset + 12
    for index in range(point_count):
        voltage, frequency, frequency_offset = struct.unpack_from(
            "<IIi", data, point_base + index * 12
        )
        points.append(
            {
                "index": index,
                "voltage_uv": voltage,
                "frequency_khz": frequency,
                "frequency_offset_khz": frequency_offset,
            }
        )

    lock_offset = point_base + 256 * 12
    lock_index, power_count = struct.unpack_from("<2I", data, lock_offset)
    if lock_index > point_count:
        raise SnapshotFormatError(
            f"MACM VF lock index {lock_index} exceeds {point_count} points"
        )
    if power_count > 4:
        raise SnapshotFormatError(
            f"MACM VF curve declares {power_count} power tuples; maximum is 4"
        )
    power_tuples: list[dict[str, int]] = []
    power_base = lock_offset + 8
    for index in range(power_count):
        power_current, power_default, frequency_current, frequency_default = (
            struct.unpack_from("<4I", data, power_base + index * 16)
        )
        power_tuples.append(
            {
                "index": index,
                "power_current_percent": power_current,
                "power_default_percent": power_default,
                "frequency_current_khz": frequency_current,
                "frequency_default_khz": frequency_default,
            }
        )

    thermal_count_offset = power_base + 4 * 16
    (thermal_count,) = struct.unpack_from("<I", data, thermal_count_offset)
    if thermal_count > 4:
        raise SnapshotFormatError(
            f"MACM VF curve declares {thermal_count} thermal tuples; maximum is 4"
        )
    thermal_tuples: list[dict[str, int]] = []
    thermal_base = thermal_count_offset + 4
    for index in range(thermal_count):
        temperature_current, temperature_default, frequency_current, frequency_default = (
            struct.unpack_from("<4I", data, thermal_base + index * 16)
        )
        thermal_tuples.append(
            {
                "index": index,
                "temperature_current_c": temperature_current,
                "temperature_default_c": temperature_default,
                "frequency_current_khz": frequency_current,
                "frequency_default_khz": frequency_default,
            }
        )

    return {
        "version": version,
        "flags": flags,
        "point_count": point_count,
        "lock_index_1_based": lock_index,
        "points": points,
        "power_tuples": power_tuples,
        "thermal_tuples": thermal_tuples,
    }


def parse_macm_snapshot(data: bytes) -> dict[str, Any]:
    """Parse a copied MACM byte snapshot without accessing live memory."""

    header, total = _macm_layout(data)
    if len(data) < total:
        raise SnapshotFormatError(
            f"MACM snapshot is truncated: needs {total} bytes, has {len(data)}"
        )
    (
        _signature,
        version,
        header_size,
        gpu_count,
        gpu_entry_size,
        master_gpu,
        flags,
        state_time,
        command,
    ) = header
    minor = version & 0xFFFF

    gpus: list[dict[str, Any]] = []
    for index in range(gpu_count):
        entry_offset = header_size + index * gpu_entry_size
        (gpu_flags,) = struct.unpack_from("<I", data, entry_offset)
        controls: dict[str, dict[str, Any]] = {}
        for name, offset, signed, unit, capability in _MACM_CONTROL_LAYOUT:
            if gpu_flags & capability and offset + 16 <= gpu_entry_size:
                controls[name] = _read_control(
                    data,
                    entry_offset,
                    offset,
                    signed=signed,
                    unit=unit,
                )

        if gpu_flags & 0x00000008:
            (
                fan_current,
                fan_flags_current,
                fan_minimum,
                fan_maximum,
                fan_default,
                fan_flags_default,
            ) = struct.unpack_from("<6I", data, entry_offset + 52)
            controls["fan_speed_percent"] = {
                "unit": "%",
                "current": fan_current,
                "minimum": fan_minimum,
                "maximum": fan_maximum,
                "default": fan_default,
                "automatic_current": bool(fan_flags_current & 1),
                "automatic_default": bool(fan_flags_default & 1),
                "flags_current": fan_flags_current,
                "flags_default": fan_flags_default,
            }

        thermal_priority = None
        if gpu_flags & 0x00004000 and gpu_entry_size >= 244:
            current, default = struct.unpack_from("<2I", data, entry_offset + 236)
            thermal_priority = {
                "current": bool(current),
                "default": bool(default),
                "raw_current": current,
                "raw_default": default,
            }

        vf_curve = None
        gpu_id = None
        if minor >= 3 and gpu_entry_size >= 3760:
            gpu_id = _decode_ansi(data[entry_offset + 3500 : entry_offset + 3760])
            if gpu_flags & 0x00020000:
                vf_curve = _parse_vf_curve(data, entry_offset)

        gpus.append(
            {
                "index": index,
                "gpu_id": gpu_id,
                "is_master": index == master_gpu,
                "flags": gpu_flags,
                "capabilities": _labels(gpu_flags, _MACM_CAPABILITY_FLAGS),
                "controls": controls,
                "thermal_prioritize": thermal_priority,
                "vf_curve": vf_curve,
            }
        )

    return {
        "schema": "kingai.afterburner.macm.v1",
        "mapping": MACM_MAPPING_NAME,
        "access": "read_only",
        "version": _version(version),
        "state_time_unix": state_time,
        "master_gpu_index": master_gpu if gpu_count else None,
        "flags": flags,
        "flag_labels": _labels(flags, _MACM_HEADER_FLAGS),
        "command": {
            "raw": command,
            "name": _MACM_COMMANDS.get(command, f"unknown_0x{command:08x}"),
        },
        "layout": {
            "header_size": header_size,
            "gpu_entry_size": gpu_entry_size,
        },
        "gpus": gpus,
    }


def _mapping_layout(kind: str) -> tuple[str, int, Callable[[bytes], tuple[Any, int]]]:
    normalized = kind.lower()
    if normalized == "mahm":
        return MAHM_MAPPING_NAME, _MAHM_HEADER.size, _mahm_layout
    if normalized == "macm":
        return MACM_MAPPING_NAME, _MACM_HEADER.size, _macm_layout
    raise ValueError(f"unknown mapping kind: {kind}")


def _acquire_macm_snapshot_mutex(kernel32: Any) -> Any:
    """Acquire the SDK-documented MACM snapshot mutex with a finite wait."""

    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_mutex(None, False, MACM_MUTEX_NAME)
    if not handle:
        error = ctypes.get_last_error()
        raise MappingUnavailableError(
            f"could not open the MACM snapshot mutex (Windows error {error})"
        )

    wait_result = wait_for_single_object(handle, MACM_MUTEX_TIMEOUT_MS)
    wait_object_0 = 0x00000000
    wait_abandoned = 0x00000080
    wait_timeout = 0x00000102
    wait_failed = 0xFFFFFFFF
    if wait_result in (wait_object_0, wait_abandoned):
        return handle

    close_handle(handle)
    if wait_result == wait_timeout:
        raise MappingUnavailableError(
            f"timed out after {MACM_MUTEX_TIMEOUT_MS} ms waiting for the "
            "MACM snapshot mutex"
        )
    if wait_result == wait_failed:
        error = ctypes.get_last_error()
        raise MappingUnavailableError(
            f"waiting for the MACM snapshot mutex failed (Windows error {error})"
        )
    raise MappingUnavailableError(
        f"waiting for the MACM snapshot mutex returned 0x{wait_result:08X}"
    )


def _release_macm_snapshot_mutex(kernel32: Any, handle: Any) -> None:
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = (wintypes.HANDLE,)
    release_mutex.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    released = release_mutex(handle)
    error = ctypes.get_last_error() if not released else 0
    close_handle(handle)
    if not released:
        raise MappingUnavailableError(
            f"releasing the MACM snapshot mutex failed (Windows error {error})"
        )


def _mapped_region_size(kernel32: Any, address: int) -> int:
    class MemoryBasicInformation(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    query = kernel32.VirtualQuery
    query.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(MemoryBasicInformation),
        ctypes.c_size_t,
    )
    query.restype = ctypes.c_size_t
    information = MemoryBasicInformation()
    if not query(
        ctypes.c_void_p(address),
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        raise MappingUnavailableError(
            f"VirtualQuery failed for mapped view (Windows error {error})"
        )
    offset = address - int(information.BaseAddress)
    if offset < 0 or offset >= information.RegionSize:
        raise MappingUnavailableError("mapped view has an invalid memory region")
    return int(information.RegionSize) - offset


def read_named_mapping(kind: str) -> bytes:
    """Copy one existing named mapping through a read-only Windows view."""

    if os.name != "nt":
        raise MappingUnavailableError(
            "live MSI Afterburner named mappings are only available on Windows"
        )

    normalized_kind = kind.lower()
    name, fixed_header_size, layout_reader = _mapping_layout(normalized_kind)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_mapping = kernel32.OpenFileMappingW
    open_mapping.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    open_mapping.restype = wintypes.HANDLE
    map_view = kernel32.MapViewOfFile
    map_view.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_size_t,
    )
    map_view.restype = ctypes.c_void_p
    unmap_view = kernel32.UnmapViewOfFile
    unmap_view.argtypes = (ctypes.c_void_p,)
    unmap_view.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    file_map_read = 0x0004
    handle = open_mapping(file_map_read, False, name)
    if not handle:
        error = ctypes.get_last_error()
        raise MappingUnavailableError(
            f"{name} is unavailable for read access (Windows error {error})"
        )

    address = map_view(handle, file_map_read, 0, 0, 0)
    if not address:
        error = ctypes.get_last_error()
        close_handle(handle)
        raise MappingUnavailableError(
            f"{name} could not be mapped read-only (Windows error {error})"
        )

    macm_mutex = None
    try:
        if normalized_kind == "macm":
            macm_mutex = _acquire_macm_snapshot_mutex(kernel32)
        region_size = _mapped_region_size(kernel32, int(address))
        if region_size < fixed_header_size:
            raise SnapshotFormatError(
                f"{name} mapped region is smaller than its fixed header"
            )
        for _attempt in range(3):
            header_before = ctypes.string_at(address, fixed_header_size)
            _header, total = layout_reader(header_before)
            if total > region_size:
                raise SnapshotFormatError(
                    f"{name} declares {total} bytes but its mapped region has "
                    f"{region_size}"
                )
            snapshot = ctypes.string_at(address, total)
            header_after = ctypes.string_at(address, fixed_header_size)
            if header_before == header_after:
                return snapshot
        raise SnapshotFormatError(
            f"{name} layout changed repeatedly while the read-only snapshot "
            "was being copied"
        )
    finally:
        try:
            if macm_mutex:
                _release_macm_snapshot_mutex(kernel32, macm_mutex)
        finally:
            unmap_view(address)
            close_handle(handle)


def read_live_snapshot(kind: str) -> dict[str, Any]:
    """Read and parse one live mapping snapshot without requesting write access."""

    data = read_named_mapping(kind)
    if kind.lower() == "mahm":
        return parse_mahm_snapshot(data)
    if kind.lower() == "macm":
        return parse_macm_snapshot(data)
    raise ValueError(f"unknown mapping kind: {kind}")


def filter_snapshot(
    snapshot: dict[str, Any],
    *,
    gpu_id: str | None = None,
    source_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Return a filtered copy while retaining exact identity semantics."""

    if not gpu_id and not source_ids:
        return snapshot

    result = dict(snapshot)
    gpus = list(snapshot.get("gpus", []))
    selected_indices: set[int] | None = None
    if gpu_id:
        matches = [gpu for gpu in gpus if gpu.get("gpu_id") == gpu_id]
        if len(matches) != 1:
            raise SnapshotFormatError(
                f"exact GPU identity matched {len(matches)} entries: {gpu_id}"
            )
        selected_indices = {int(matches[0]["index"])}
        result["gpus"] = matches

    if snapshot.get("mapping") == MAHM_MAPPING_NAME:
        sources = list(snapshot.get("sources", []))
        if selected_indices is not None:
            sources = [
                source
                for source in sources
                if source.get("gpu_index") is None
                or source.get("gpu_index") in selected_indices
            ]
        if source_ids:
            sources = [
                source
                for source in sources
                if int(source.get("source_id", -1)) in source_ids
            ]
        result["sources"] = sources
    elif source_ids:
        raise SnapshotFormatError("--source-id is only valid for MAHM monitoring")

    return result


__all__ = [
    "DEAD_SIGNATURE",
    "MACM_MAPPING_NAME",
    "MACM_SIGNATURE",
    "MAHM_MAPPING_NAME",
    "MAHM_SIGNATURE",
    "MappingUnavailableError",
    "SharedMemoryError",
    "SnapshotFormatError",
    "filter_snapshot",
    "parse_macm_snapshot",
    "parse_mahm_snapshot",
    "read_live_snapshot",
    "read_named_mapping",
]
