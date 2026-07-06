#!/usr/bin/env python3
"""Unit tests for the macOS/Linux daemon's multi config-dir active-plan support.

Covers read_config_dirs, read_token_for, PlanSelector, and poll_active_payload.

Run: python -m pytest daemon/tests/test_macos_multidir.py -x -q
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import daemon.claude_usage_daemon as mod
from daemon.claude_usage_daemon import PlanSelector, read_config_dirs, read_token_for


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# read_config_dirs
# ---------------------------------------------------------------------------

def test_config_dirs_defaults_to_claude_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "CONFIG_FILE", tmp_path / "config")  # absent
    assert read_config_dirs() == [mod.DEFAULT_CONFIG_DIR]


def test_config_dirs_defaults_when_key_absent(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("clock = auto\nchime = on\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    assert read_config_dirs() == [mod.DEFAULT_CONFIG_DIR]


def test_config_dirs_parses_comma_list_and_expands_tilde(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("config_dirs = ~/.claude, ~/.claude-work  # two plans\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    assert read_config_dirs() == [Path.home() / ".claude", Path.home() / ".claude-work"]


# ---------------------------------------------------------------------------
# read_token_for
# ---------------------------------------------------------------------------

def test_token_for_reads_dir_credentials_file(tmp_path):
    (tmp_path / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"TOK_X"}}')
    assert read_token_for(tmp_path) == "TOK_X"


def test_token_for_missing_file_non_default_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert read_token_for(tmp_path) is None  # no file, not the default dir


def test_token_for_default_dir_falls_back_to_keychain_on_macos(tmp_path, monkeypatch):
    # An empty dir standing in as the default: no file present -> Keychain.
    monkeypatch.setattr(mod, "DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    with patch.object(mod, "_read_token_keychain", return_value="TOK_KEYCHAIN"):
        assert read_token_for(tmp_path) == "TOK_KEYCHAIN"


def test_token_for_file_wins_over_keychain(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    (tmp_path / ".credentials.json").write_text('{"accessToken":"TOK_FILE"}')
    with patch.object(mod, "_read_token_keychain", return_value="TOK_KEYCHAIN"):
        assert read_token_for(tmp_path) == "TOK_FILE"


# ---------------------------------------------------------------------------
# PlanSelector — the "active = recent API activity" rule
# ---------------------------------------------------------------------------

A, B = Path("/a"), Path("/b")


def test_selector_startup_picks_highest_util():
    sel = PlanSelector()
    assert sel.choose({A: 10, B: 30}) == B  # no history yet -> highest %


def test_selector_switches_on_rise():
    sel = PlanSelector()
    sel.choose({A: 10, B: 30})           # startup -> B
    assert sel.choose({A: 20, B: 30}) == A  # A rose 10->20 -> A active


def test_selector_sticky_when_no_movement():
    sel = PlanSelector()
    sel.choose({A: 10, B: 30})
    sel.choose({A: 20, B: 30})           # A active
    assert sel.choose({A: 20, B: 30}) == A  # nothing moved -> still A (not higher B)


def test_selector_reset_to_zero_is_not_activity():
    sel = PlanSelector()
    sel.choose({A: 10, B: 30})
    sel.choose({A: 20, B: 30})           # A active
    sel.choose({A: 20, B: 45})           # B rose -> B active
    assert sel.choose({A: 20, B: 0}) == B   # B window reset (drop) isn't a rise -> stays B


def test_selector_larger_rise_wins_same_cycle():
    sel = PlanSelector()
    sel.choose({A: 10, B: 10})           # seed
    assert sel.choose({A: 12, B: 40}) == B  # both rose same cycle -> higher % breaks tie


# ---------------------------------------------------------------------------
# poll_active_payload — integration over the helpers
# ---------------------------------------------------------------------------

def test_poll_active_payload_picks_active_and_skips_tokenless(monkeypatch):
    dirs = [A, B]
    monkeypatch.setattr(mod, "read_config_dirs", lambda: dirs)
    monkeypatch.setattr(mod, "read_token_for", lambda d: {A: "tA", B: None}[d])  # B has no token

    async def fake_poll(token):
        return {"s": 25, "ok": True} if token == "tA" else None

    sel = PlanSelector()
    with patch.object(mod, "poll_api", new=AsyncMock(side_effect=fake_poll)):
        payload = _run(mod.poll_active_payload(sel))
    assert payload == {"s": 25, "ok": True}  # only A had a token


def test_poll_active_payload_returns_none_when_all_fail(monkeypatch):
    monkeypatch.setattr(mod, "read_config_dirs", lambda: [A, B])
    monkeypatch.setattr(mod, "read_token_for", lambda d: None)
    with patch.object(mod, "poll_api", new=AsyncMock(return_value=None)):
        assert _run(mod.poll_active_payload(PlanSelector())) is None


def test_poll_active_payload_selects_higher_util_plan(monkeypatch):
    monkeypatch.setattr(mod, "read_config_dirs", lambda: [A, B])
    monkeypatch.setattr(mod, "read_token_for", lambda d: {A: "tA", B: "tB"}[d])

    async def fake_poll(token):
        return {"s": 12, "ok": True} if token == "tA" else {"s": 40, "ok": True}

    with patch.object(mod, "poll_api", new=AsyncMock(side_effect=fake_poll)):
        payload = _run(mod.poll_active_payload(PlanSelector()))
    assert payload["s"] == 40  # startup -> highest util plan (B)


# ---------------------------------------------------------------------------
# discover_target — the daemon only ever targets the device this system already
# holds; it never scans for a nearby device by name (there is no scan fallback).
# ---------------------------------------------------------------------------

def test_discover_target_darwin_uses_os_held_device(monkeypatch):
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    sentinel = object()
    with patch.object(mod, "retrieve_connected_macos", new=AsyncMock(return_value=sentinel)):
        assert _run(mod.discover_target()) is sentinel  # used directly, no scan


def test_discover_target_darwin_returns_none_when_not_held(monkeypatch):
    # Not held by the OS -> wait (return None); never grabs an arbitrary device.
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    with patch.object(mod, "retrieve_connected_macos", new=AsyncMock(return_value=None)):
        assert _run(mod.discover_target()) is None


def test_discover_target_non_darwin_uses_pinned_address(monkeypatch):
    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setattr(mod, "load_cached_address", lambda: "AA:BB:CC:DD:EE:FF")
    assert _run(mod.discover_target()) == "AA:BB:CC:DD:EE:FF"


def test_discover_target_non_darwin_returns_none_without_pin(monkeypatch):
    # No pinned address cached -> wait; never scans by name.
    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setattr(mod, "load_cached_address", lambda: None)
    assert _run(mod.discover_target()) is None


# ---------------------------------------------------------------------------
# read_target_device / normalize_device_name
# ---------------------------------------------------------------------------

def test_normalize_device_name_bare_suffix():
    assert mod.normalize_device_name("mor") == "Clawdmeter-mor"

def test_normalize_device_name_full_name_passthrough():
    assert mod.normalize_device_name("Clawdmeter-mor") == "Clawdmeter-mor"

def test_normalize_device_name_bare_base_passthrough():
    assert mod.normalize_device_name("Clawdmeter") == "Clawdmeter"

def test_normalize_device_name_trims_and_blank_is_none():
    assert mod.normalize_device_name("  bob ") == "Clawdmeter-bob"
    assert mod.normalize_device_name("   ") is None

def test_target_device_unset_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "CONFIG_FILE", tmp_path / "config")  # absent
    assert mod.read_target_device() is None

def test_target_device_key_absent_returns_none(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("clock = auto\nchime = on\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    assert mod.read_target_device() is None

def test_target_device_bare_suffix(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("device = mor   # my board\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    assert mod.read_target_device() == "Clawdmeter-mor"

def test_target_device_full_name(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("device = Clawdmeter-alice\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    assert mod.read_target_device() == "Clawdmeter-alice"


def test_target_name_matchable():
    # Unnamed board and valid 1-7 char [A-Za-z0-9-] suffixes can match.
    assert mod._target_name_matchable("Clawdmeter") is True
    assert mod._target_name_matchable("Clawdmeter-mor") is True
    assert mod._target_name_matchable("Clawdmeter-a1-b2c") is True   # 5 chars
    assert mod._target_name_matchable("Clawdmeter-1234567") is True  # exactly 7
    # Un-matchable: too long, illegal char, empty suffix, missing hyphen.
    assert mod._target_name_matchable("Clawdmeter-12345678") is False  # 8 chars
    assert mod._target_name_matchable("Clawdmeter-bad_x") is False     # underscore
    assert mod._target_name_matchable("Clawdmeter-") is False          # empty suffix
    assert mod._target_name_matchable("Clawdmeterx") is False          # no hyphen


def test_target_device_invalid_suffix_warns_but_returns_name(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("device = toolongsuffix\n")  # 13 chars -> no board can advertise it
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    logs = []
    monkeypatch.setattr(mod, "log", lambda m: logs.append(m))
    # Returned as-is so the daemon WAITS (never grabs the wrong board)...
    assert mod.read_target_device() == "Clawdmeter-toolongsuffix"
    # ...but a distinct warning is emitted rather than failing silently.
    assert any("can't match any board" in m for m in logs)


def test_target_device_valid_suffix_no_warning(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("device = mor\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    logs = []
    monkeypatch.setattr(mod, "log", lambda m: logs.append(m))
    assert mod.read_target_device() == "Clawdmeter-mor"
    assert logs == []  # a valid name must not trigger the warning


# ---------------------------------------------------------------------------
# connect_and_run — board confirmation via custom name characteristic (…0005)
# ---------------------------------------------------------------------------

def test_connect_wrong_board_name_returns_false(monkeypatch):
    """A connected peripheral whose 0x2A00 != expected is rejected (False)."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_connected = True
    # 0x2A00 reports a different board than we want.
    client.read_gatt_char = AsyncMock(return_value=b"Clawdmeter-other")

    monkeypatch.setattr(mod, "BleakClient", lambda *a, **k: client)
    stop = asyncio.Event()

    ok = _run(mod.connect_and_run("UUID-1", stop, expected_name="Clawdmeter-mor"))

    assert ok is False
    client.read_gatt_char.assert_awaited_once_with(mod.NAME_CHAR_UUID)
    client.disconnect.assert_awaited()  # we hung up on the wrong board


def test_connect_match_sets_preferred_uuid_and_falls_through(monkeypatch):
    """0x2A00 == expected -> _preferred_uuid is set and we fall into the session path."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(return_value=b"Clawdmeter-mor")
    # Reached only after the match, inside Session.setup_refresh_subscription().
    client.start_notify = AsyncMock()

    monkeypatch.setattr(mod, "BleakClient", lambda *a, **k: client)

    mod._preferred_uuid = None
    try:
        # Already-set stop_event so the post-match `while` loop body never
        # runs — we're exercising the match branch, not the polling loop.
        stop = asyncio.Event()
        stop.set()

        ok = _run(mod.connect_and_run("UUID-1", stop, expected_name="Clawdmeter-mor"))

        assert mod._preferred_uuid == "UUID-1"
        client.read_gatt_char.assert_awaited_once_with(mod.NAME_CHAR_UUID)
        assert ok is False  # loop never ran, so used_successfully stays False
        client.disconnect.assert_awaited()  # finally-block hangup on loop exit
    finally:
        mod._preferred_uuid = None


def test_connect_gap_read_failure_treated_as_mismatch(monkeypatch):
    """A 0x2A00 read failure is treated like a name mismatch: skip, don't crash."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(side_effect=mod.BleakError("boom"))

    monkeypatch.setattr(mod, "BleakClient", lambda *a, **k: client)

    mod._preferred_uuid = None
    try:
        stop = asyncio.Event()

        ok = _run(mod.connect_and_run("UUID-1", stop, expected_name="Clawdmeter-mor"))

        assert ok is False
        client.disconnect.assert_awaited()
        assert mod._preferred_uuid is None  # never reached the match branch
    finally:
        mod._preferred_uuid = None
