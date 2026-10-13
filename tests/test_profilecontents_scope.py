"""Synthetic INI section-safety regressions (no GPU or user profiles).

Run from repository root:
    python -m pytest -q tests/test_profilecontents_scope.py tests/test_profile_safety.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from profile_safety import SafetyError, patch_profile_contents  # noqa: E402


def test_existing_settings_key_changes_only_target():
    source = "[Settings]\nProfileContents=1\nMonitoring=keep\n"
    assert patch_profile_contents(source) == source.replace(
        "ProfileContents=1", "ProfileContents=3"
    )


def test_missing_key_inserted_after_settings_header():
    assert patch_profile_contents("[Settings]\r\nMonitoring=keep\r\n") == (
        "[Settings]\r\nProfileContents=3\r\nMonitoring=keep\r\n"
    )


def test_unrelated_section_key_does_not_suppress_settings_insertion():
    text = "[Other]\nProfileContents=7\n[Settings]\nMonitoring=keep\n"
    assert patch_profile_contents(text) == (
        "[Other]\nProfileContents=7\n[Settings]\n"
        "ProfileContents=3\nMonitoring=keep\n"
    )


def test_unrelated_profilecontents_remains_unchanged():
    text = ("[Other]\r\nProfileContents=7\r\n"
            "[Settings]\r\nProfileContents=1\r\n")
    assert patch_profile_contents(text) == (
        "[Other]\r\nProfileContents=7\r\n"
        "[Settings]\r\nProfileContents=3\r\n"
    )


def test_duplicate_settings_keys_fail_closed():
    with pytest.raises(SafetyError, match="at most one"):
        patch_profile_contents(
            "[Settings]\nProfileContents=1\nProfileContents=2\n"
        )


def test_duplicate_settings_sections_fail_closed():
    with pytest.raises(SafetyError, match="Expected one"):
        patch_profile_contents("[Settings]\nA=1\n[Settings]\nB=2\n")


def test_missing_settings_section_fails_closed():
    with pytest.raises(SafetyError, match="Expected one"):
        patch_profile_contents("[Other]\nProfileContents=4\n")


def test_crlf_and_separator_lines_preserved():
    text = "[Settings]\r\nA=2\r\n\r\n[Other]\r\nB=1\r\n"
    assert patch_profile_contents(text) == (
        "[Settings]\r\nProfileContents=3\r\nA=2\r\n\r\n[Other]\r\nB=1\r\n"
    )


def test_header_without_trailing_newline_still_supported():
    assert patch_profile_contents("[Settings]") == "[Settings]\nProfileContents=3\n"


def test_only_one_intended_key_updated():
    text = (
        "[Other]\nProfileContents=2\n[Settings]\nProfileContents=2\n"
        "[Profile1]\nProfileContents=9\n"
    )
    result = patch_profile_contents(text)
    assert result.count("ProfileContents=3") == 1
    assert result.count("ProfileContents=2") == 1
    assert result.count("ProfileContents=9") == 1


def test_malformed_space_before_equals_rejected():
    with pytest.raises(SafetyError, match="Malformed"):
        patch_profile_contents("[Settings]\nProfileContents =3\n")
