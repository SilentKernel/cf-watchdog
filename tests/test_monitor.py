"""Tests for the cf-watchdog monitor."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import monitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(httpx_mock) -> monitor.CloudflareClient:
    real = httpx.Client(
        base_url=monitor.CLOUDFLARE_API_BASE,
        headers={"Authorization": "Bearer test"},
    )
    return monitor.CloudflareClient(token="test", client=real)


def _zone_payload(zones, *, page=1, total_pages=1):
    return {
        "success": True,
        "errors": [],
        "result": zones,
        "result_info": {"page": page, "total_pages": total_pages},
    }


def _settings_payload(settings):
    return {
        "success": True,
        "errors": [],
        "result": settings,
    }


# ---------------------------------------------------------------------------
# Cloudflare client
# ---------------------------------------------------------------------------


def test_list_zones_handles_pagination(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones?page=1&per_page=50",
        json=_zone_payload([{"id": "a", "name": "a.com"}], page=1, total_pages=2),
    )
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones?page=2&per_page=50",
        json=_zone_payload([{"id": "b", "name": "b.com"}], page=2, total_pages=2),
    )

    with _make_client(httpx_mock) as cf:
        zones = cf.list_zones()

    assert [z["id"] for z in zones] == ["a", "b"]


def test_get_zone_settings_keyed_by_id(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/abc/settings",
        json=_settings_payload(
            [
                {"id": "http3", "value": "off"},
                {"id": "min_tls_version", "value": "1.0"},
            ]
        ),
    )

    with _make_client(httpx_mock) as cf:
        settings = cf.get_zone_settings("abc")

    assert set(settings.keys()) == {"http3", "min_tls_version"}
    assert settings["http3"]["value"] == "off"


def test_get_zone_settings_raises_on_api_error(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/x/settings",
        json={"success": False, "errors": [{"code": 1000, "message": "boom"}]},
    )
    with _make_client(httpx_mock) as cf:
        with pytest.raises(RuntimeError):
            cf.get_zone_settings("x")


# ---------------------------------------------------------------------------
# Snapshot store
# ---------------------------------------------------------------------------


def test_snapshot_round_trip(tmp_path: Path):
    data = {"http3": {"id": "http3", "value": "off"}}
    p = monitor.save_snapshot(tmp_path, "z1", data)
    assert p.exists()
    loaded = monitor.load_latest_snapshot(tmp_path, "z1")
    assert loaded == data


def test_load_latest_picks_newest(tmp_path: Path):
    monitor.save_snapshot(tmp_path, "z1", {"v": 1})
    time.sleep(1.1)  # ensure different ISO seconds
    monitor.save_snapshot(tmp_path, "z1", {"v": 2})
    assert monitor.load_latest_snapshot(tmp_path, "z1") == {"v": 2}


def test_load_latest_returns_none_when_empty(tmp_path: Path):
    assert monitor.load_latest_snapshot(tmp_path, "missing") is None


def test_load_latest_can_exclude_a_path(tmp_path: Path):
    p1 = monitor.save_snapshot(tmp_path, "z1", {"v": 1})
    time.sleep(1.1)
    p2 = monitor.save_snapshot(tmp_path, "z1", {"v": 2})
    assert monitor.load_latest_snapshot(tmp_path, "z1", exclude=p2) == {"v": 1}
    assert monitor.load_latest_snapshot(tmp_path, "z1", exclude=p1) == {"v": 2}


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def test_compute_diff_detects_changed_value():
    old = {"http3": {"id": "http3", "value": "off"}}
    new = {"http3": {"id": "http3", "value": "on"}}
    diff = monitor.compute_diff(old, new)
    assert "values_changed" in diff


def test_compute_diff_detects_added_key():
    old = {"http3": {"id": "http3", "value": "off"}}
    new = {
        "http3": {"id": "http3", "value": "off"},
        "bot_fight_mode_v2": {"id": "bot_fight_mode_v2", "value": "on"},
    }
    diff = monitor.compute_diff(old, new)
    assert "dictionary_item_added" in diff


def test_compute_diff_detects_removed_key():
    old = {
        "http3": {"id": "http3", "value": "off"},
        "legacy": {"id": "legacy", "value": "on"},
    }
    new = {"http3": {"id": "http3", "value": "off"}}
    diff = monitor.compute_diff(old, new)
    assert "dictionary_item_removed" in diff


def test_compute_diff_empty_when_identical():
    same = {"http3": {"id": "http3", "value": "off"}}
    assert not monitor.compute_diff(same, same)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_format_diff_includes_all_sections():
    old = {
        "http3": {"id": "http3", "value": "off"},
        "legacy": {"id": "legacy", "value": "on"},
    }
    new = {
        "http3": {"id": "http3", "value": "on"},
        "bot_fight_mode_v2": {"id": "bot_fight_mode_v2", "value": "on"},
    }
    diff = monitor.compute_diff(old, new)
    msg = monitor.format_diff_for_telegram("example.com", "abc123", diff)
    # Unescape MarkdownV2 backslashes for substring checks.
    plain = msg.replace("\\", "")

    assert "Cloudflare drift detected" in plain
    assert "example.com" in plain
    assert "abc123" in plain
    assert "Changed" in plain
    assert "http3" in plain
    assert "New settings" in plain
    assert "bot_fight_mode_v2" in plain
    assert "Removed" in plain
    assert "legacy" in plain


def test_format_diff_truncates_huge_messages():
    old = {f"k{i}": {"id": f"k{i}", "value": "x" * 50} for i in range(500)}
    new = {f"k{i}": {"id": f"k{i}", "value": "y" * 50} for i in range(500)}
    diff = monitor.compute_diff(old, new)
    msg = monitor.format_diff_for_telegram("z.com", "zid", diff)
    assert len(msg) <= monitor.TELEGRAM_MAX_LEN + 50  # plus the truncation suffix


# ---------------------------------------------------------------------------
# Telegram resilience
# ---------------------------------------------------------------------------


async def test_send_telegram_swallows_errors():
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("boom")
    # Must not raise
    await monitor.send_telegram(bot, "123", "hi")
    bot.send_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# End to end: first run baseline, then drift triggers Telegram
# ---------------------------------------------------------------------------


async def test_run_once_first_baseline_then_drift(tmp_path: Path, httpx_mock, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = monitor.Config(
        cloudflare_token="t",
        telegram_token="tt",
        telegram_chat_id="42",
        interval_seconds=1,
        snapshots_dir=tmp_path / "snapshots",
    )

    # First cycle: list zones, then settings (http3=off).
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones?page=1&per_page=50",
        json=_zone_payload([{"id": "z1", "name": "example.com"}]),
    )
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z1/settings",
        json=_settings_payload([{"id": "http3", "value": "off"}]),
    )

    bot = AsyncMock()
    cf = _make_client(httpx_mock)
    await monitor.run_once(cf, bot, cfg)

    # First run: baseline saved, no Telegram notification.
    bot.send_message.assert_not_awaited()
    snaps = list((cfg.snapshots_dir / "z1").glob("*.json"))
    assert len(snaps) == 1

    # Second cycle: Cloudflare flips http3 to on.
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones?page=1&per_page=50",
        json=_zone_payload([{"id": "z1", "name": "example.com"}]),
    )
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z1/settings",
        json=_settings_payload(
            [
                {"id": "http3", "value": "on"},
                {"id": "bot_fight_mode_v2", "value": "on"},
            ]
        ),
    )
    time.sleep(1.1)  # ensure new snapshot has a later ISO-second timestamp
    await monitor.run_once(cf, bot, cfg)

    cf.close()

    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.kwargs["text"].replace("\\", "")
    assert "http3" in sent_text
    assert "off" in sent_text and "on" in sent_text
    assert "bot_fight_mode_v2" in sent_text  # new key highlighted

    # .last_run marker written
    assert Path(".last_run").exists()
