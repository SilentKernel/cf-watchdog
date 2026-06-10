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


def _make_client(httpx_mock, *, request_delay_seconds: float = 0.0,
                 retry_attempts: int = 1, retry_delay_seconds: float = 0.0,
                 sleep=None) -> monitor.CloudflareClient:
    real = httpx.Client(
        base_url=monitor.CLOUDFLARE_API_BASE,
        headers={"Authorization": "Bearer test"},
    )
    kwargs = {
        "client": real,
        "request_delay_seconds": request_delay_seconds,
        "retry_attempts": retry_attempts,
        "retry_delay_seconds": retry_delay_seconds,
    }
    if sleep is not None:
        kwargs["sleep"] = sleep
    return monitor.CloudflareClient(token="test", **kwargs)


def _ok(result, **extra):
    return {"success": True, "errors": [], "messages": [], "result": result, **extra}


def _zone_payload(zones, *, page=1, total_pages=1):
    return _ok(zones, result_info={"page": page, "total_pages": total_pages})


def _settings_payload(settings):
    return _ok(settings)


def _mock_full_zone(httpx_mock, zone_id, *, settings, argo=None, smart=None,
                    reserve=None, rulesets=None, pagerules=None):
    """Register mocks for every endpoint collect_zone_state hits.

    `argo`/`smart`/`reserve` may be (status, body) tuples for graceful 4xx; pass
    just a dict for a 200 response. `rulesets` is a list of (summary, detail).
    """
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/{zone_id}/settings",
        json=_settings_payload(settings),
    )

    def _single(url, value):
        if isinstance(value, tuple):
            status, body = value
            httpx_mock.add_response(url=url, status_code=status, json=body)
        else:
            httpx_mock.add_response(url=url, json=_ok(value))

    _single(f"{monitor.CLOUDFLARE_API_BASE}/zones/{zone_id}/argo/tiered_caching",
            argo if argo is not None else {"id": "tcache", "value": "off", "editable": True})
    _single(
        f"{monitor.CLOUDFLARE_API_BASE}/zones/{zone_id}/cache/tiered_cache_smart_topology_enable",
        smart if smart is not None else {"id": "tiered_cache_smart_topology_enable", "value": "off", "editable": True},
    )
    _single(f"{monitor.CLOUDFLARE_API_BASE}/zones/{zone_id}/cache/cache_reserve",
            reserve if reserve is not None else {"id": "cache_reserve", "value": "off", "editable": True})

    rs_pairs = rulesets or []
    summaries = [s for s, _ in rs_pairs]
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/{zone_id}/rulesets",
        json=_ok(summaries),
    )
    for summary, detail in rs_pairs:
        httpx_mock.add_response(
            url=f"{monitor.CLOUDFLARE_API_BASE}/zones/{zone_id}/rulesets/{summary['id']}",
            json=_ok(detail),
        )

    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/{zone_id}/pagerules",
        json=_ok(pagerules or []),
    )


# ---------------------------------------------------------------------------
# Cloudflare client — existing endpoints
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
# Cloudflare client — new endpoints
# ---------------------------------------------------------------------------


def test_get_argo_tiered_caching_ok(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z/argo/tiered_caching",
        json=_ok({"id": "tcache", "value": "on", "editable": True}),
    )
    with _make_client(httpx_mock) as cf:
        assert cf.get_argo_tiered_caching("z") == {"id": "tcache", "value": "on", "editable": True}


def test_get_argo_tiered_caching_404_becomes_status(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z/argo/tiered_caching",
        status_code=404,
        json={"success": False, "errors": [{"code": 1000, "message": "no argo"}]},
    )
    with _make_client(httpx_mock) as cf:
        assert cf.get_argo_tiered_caching("z") == {"_status": 404}


def test_get_cache_reserve_403_becomes_status(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z/cache/cache_reserve",
        status_code=403,
        json={"success": False, "errors": []},
    )
    with _make_client(httpx_mock) as cf:
        assert cf.get_cache_reserve("z") == {"_status": 403}


def test_get_smart_topology_ok(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z/cache/tiered_cache_smart_topology_enable",
        json=_ok({"id": "tiered_cache_smart_topology_enable", "value": "off"}),
    )
    with _make_client(httpx_mock) as cf:
        assert cf.get_tiered_cache_smart_topology("z")["value"] == "off"


def test_list_rulesets_ok(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z/rulesets",
        json=_ok([{"id": "rs1", "name": "default"}]),
    )
    with _make_client(httpx_mock) as cf:
        rs = cf.list_rulesets("z")
    assert rs == [{"id": "rs1", "name": "default"}]


def test_get_ruleset_ok(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z/rulesets/rs1",
        json=_ok({"id": "rs1", "rules": [{"id": "r1", "action": "block"}]}),
    )
    with _make_client(httpx_mock) as cf:
        rs = cf.get_ruleset("z", "rs1")
    assert rs["rules"][0]["action"] == "block"


def test_get_ruleset_403_becomes_status(httpx_mock):
    # Cloudflare-managed rulesets are listable but not readable by the token.
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z/rulesets/rs1",
        status_code=403,
        json={"success": False},
    )
    with _make_client(httpx_mock) as cf:
        assert cf.get_ruleset("z", "rs1") == {"_status": 403}


def test_list_pagerules_ok(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z/pagerules",
        json=_ok([{"id": "p1", "actions": [{"id": "browser_check", "value": "on"}]}]),
    )
    with _make_client(httpx_mock) as cf:
        pr = cf.list_pagerules("z")
    assert pr[0]["id"] == "p1"


# ---------------------------------------------------------------------------
# Request pacing
# ---------------------------------------------------------------------------


def test_request_delay_skips_first_call_then_sleeps(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z/settings",
        json=_settings_payload([{"id": "http3", "value": "off"}]),
    )
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z/argo/tiered_caching",
        json=_ok({"id": "tcache", "value": "on"}),
    )
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z/cache/cache_reserve",
        json=_ok({"id": "cache_reserve", "value": "off"}),
    )

    sleep_calls: list[float] = []
    with _make_client(httpx_mock, request_delay_seconds=2.0,
                       sleep=lambda s: sleep_calls.append(s)) as cf:
        cf.get_zone_settings("z")
        cf.get_argo_tiered_caching("z")
        cf.get_cache_reserve("z")

    # 3 calls → sleep should have fired 2 times (skip the first)
    assert sleep_calls == [2.0, 2.0]


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


_SETTINGS_URL = f"{monitor.CLOUDFLARE_API_BASE}/zones/z/settings"


def test_retry_recovers_from_transient_500(httpx_mock):
    httpx_mock.add_response(url=_SETTINGS_URL, status_code=500, json={"success": False})
    httpx_mock.add_response(
        url=_SETTINGS_URL, json=_settings_payload([{"id": "http3", "value": "off"}])
    )

    sleep_calls: list[float] = []
    with _make_client(httpx_mock, retry_attempts=5, retry_delay_seconds=1.0,
                      sleep=sleep_calls.append) as cf:
        settings = cf.get_zone_settings("z")

    assert settings["http3"]["value"] == "off"
    assert sleep_calls == [1.0]
    assert len(httpx_mock.get_requests()) == 2


def test_retry_recovers_from_transient_connect_error(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("nope"), url=_SETTINGS_URL)
    httpx_mock.add_response(
        url=_SETTINGS_URL, json=_settings_payload([{"id": "http3", "value": "on"}])
    )

    with _make_client(httpx_mock, retry_attempts=5, retry_delay_seconds=1.0,
                      sleep=lambda s: None) as cf:
        settings = cf.get_zone_settings("z")

    assert settings["http3"]["value"] == "on"
    assert len(httpx_mock.get_requests()) == 2


def test_retry_gives_up_after_max_attempts_on_500(httpx_mock):
    for _ in range(5):
        httpx_mock.add_response(url=_SETTINGS_URL, status_code=500, json={"success": False})

    sleep_calls: list[float] = []
    with _make_client(httpx_mock, retry_attempts=5, retry_delay_seconds=1.0,
                      sleep=sleep_calls.append) as cf:
        with pytest.raises(httpx.HTTPStatusError):
            cf.get_zone_settings("z")

    assert len(httpx_mock.get_requests()) == 5
    assert sleep_calls == [1.0] * 4  # sleeps between attempts, not after the last


def test_retry_gives_up_after_max_attempts_on_connect_error(httpx_mock):
    for _ in range(5):
        httpx_mock.add_exception(httpx.ConnectError("nope"), url=_SETTINGS_URL)

    with _make_client(httpx_mock, retry_attempts=5, retry_delay_seconds=1.0,
                      sleep=lambda s: None) as cf:
        with pytest.raises(httpx.ConnectError):
            cf.get_zone_settings("z")

    assert len(httpx_mock.get_requests()) == 5


def test_retry_skips_graceful_403(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z/cache/cache_reserve",
        status_code=403,
        json={"success": False},
    )

    sleep_calls: list[float] = []
    with _make_client(httpx_mock, retry_attempts=5, retry_delay_seconds=1.0,
                      sleep=sleep_calls.append) as cf:
        assert cf.get_cache_reserve("z") == {"_status": 403}

    assert len(httpx_mock.get_requests()) == 1
    assert sleep_calls == []


def test_retry_handles_429(httpx_mock):
    httpx_mock.add_response(url=_SETTINGS_URL, status_code=429, json={"success": False})
    httpx_mock.add_response(
        url=_SETTINGS_URL, json=_settings_payload([{"id": "http3", "value": "off"}])
    )

    with _make_client(httpx_mock, retry_attempts=5, retry_delay_seconds=1.0,
                      sleep=lambda s: None) as cf:
        settings = cf.get_zone_settings("z")

    assert settings["http3"]["value"] == "off"
    assert len(httpx_mock.get_requests()) == 2


def test_default_retry_config():
    cf = monitor.CloudflareClient(token="test")
    try:
        assert cf._retry_attempts == 5
        assert cf._retry_delay == 1.0
    finally:
        cf.close()


# ---------------------------------------------------------------------------
# collect_zone_state aggregator
# ---------------------------------------------------------------------------


def test_collect_zone_state_happy_path(httpx_mock):
    _mock_full_zone(
        httpx_mock,
        "z1",
        settings=[{"id": "http3", "value": "on"}],
        rulesets=[
            (
                {"id": "rs1", "name": "default"},
                {"id": "rs1", "rules": [{"id": "r1", "action": "block"}]},
            )
        ],
        pagerules=[{"id": "pr1", "status": "active"}],
    )
    with _make_client(httpx_mock) as cf:
        state, errors = monitor.collect_zone_state(cf, "z1")

    assert errors == []
    assert state["_schema"] == monitor.SNAPSHOT_SCHEMA
    assert state["settings"]["http3"]["value"] == "on"
    assert state["argo_tiered_caching"]["value"] == "off"
    assert state["cache_reserve"]["value"] == "off"
    assert state["tiered_cache_smart_topology"]["value"] == "off"
    assert state["rulesets"]["rs1"]["rules"][0]["action"] == "block"
    assert state["pagerules"]["pr1"]["status"] == "active"


def test_collect_zone_state_records_unavailable_features(httpx_mock):
    _mock_full_zone(
        httpx_mock,
        "z1",
        settings=[{"id": "http3", "value": "on"}],
        argo=(404, {"success": False}),
        reserve=(403, {"success": False}),
    )
    with _make_client(httpx_mock) as cf:
        state, errors = monitor.collect_zone_state(cf, "z1")

    assert errors == []
    assert state["argo_tiered_caching"] == {"_status": 404}
    assert state["cache_reserve"] == {"_status": 403}


def test_collect_zone_state_collects_errors_on_500(httpx_mock):
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z1/settings",
        status_code=500,
        json={"success": False},
    )
    # The aggregator continues after a 500 to attempt other endpoints
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z1/argo/tiered_caching",
        json=_ok({"id": "tcache", "value": "on"}),
    )
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z1/cache/tiered_cache_smart_topology_enable",
        json=_ok({"id": "x", "value": "off"}),
    )
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z1/cache/cache_reserve",
        json=_ok({"id": "cache_reserve", "value": "off"}),
    )
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z1/rulesets",
        json=_ok([]),
    )
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z1/pagerules",
        json=_ok([]),
    )
    with _make_client(httpx_mock) as cf:
        state, errors = monitor.collect_zone_state(cf, "z1")

    assert any("settings" in e for e in errors)
    assert "settings" not in state  # not snapshotted
    assert state["argo_tiered_caching"]["value"] == "on"


# ---------------------------------------------------------------------------
# Snapshot store
# ---------------------------------------------------------------------------


def test_snapshot_round_trip(tmp_path: Path):
    data = {"_schema": "v2", "settings": {"http3": {"id": "http3", "value": "off"}}}
    p = monitor.save_snapshot(tmp_path, "z1", data)
    assert p.exists()
    loaded = monitor.load_latest_snapshot(tmp_path, "z1")
    assert loaded == data


def test_load_latest_picks_newest(tmp_path: Path):
    monitor.save_snapshot(tmp_path, "z1", {"v": 1})
    time.sleep(1.1)
    monitor.save_snapshot(tmp_path, "z1", {"v": 2})
    assert monitor.load_latest_snapshot(tmp_path, "z1") == {"v": 2}


def test_load_latest_returns_none_when_empty(tmp_path: Path):
    assert monitor.load_latest_snapshot(tmp_path, "missing") is None


def test_is_legacy_snapshot():
    assert monitor.is_legacy_snapshot({"http3": {"id": "http3"}}) is True
    assert monitor.is_legacy_snapshot({"_schema": "v2", "settings": {}}) is False
    assert monitor.is_legacy_snapshot(None) is False
    assert monitor.is_legacy_snapshot({}) is False


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def test_compute_diff_detects_changed_value():
    old = {"settings": {"http3": {"value": "off"}}}
    new = {"settings": {"http3": {"value": "on"}}}
    diff = monitor.compute_diff(old, new)
    assert "values_changed" in diff


def test_compute_diff_detects_added_key():
    old = {"settings": {}}
    new = {"settings": {"bot_fight_mode_v2": {"value": "on"}}}
    diff = monitor.compute_diff(old, new)
    assert "dictionary_item_added" in diff


def test_compute_diff_empty_when_identical():
    same = {"settings": {"http3": {"value": "off"}}}
    assert not monitor.compute_diff(same, same)


def test_compute_diff_detects_ruleset_rule_change():
    old = {"rulesets": {"rs1": {"id": "rs1", "rules": [{"id": "r1", "action": "block"}]}}}
    new = {"rulesets": {"rs1": {"id": "rs1", "rules": [{"id": "r1", "action": "challenge"}]}}}
    diff = monitor.compute_diff(old, new)
    assert "values_changed" in diff


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_format_diff_includes_all_sections():
    old = {
        "settings": {
            "http3": {"id": "http3", "value": "off"},
            "legacy": {"id": "legacy", "value": "on"},
        }
    }
    new = {
        "settings": {
            "http3": {"id": "http3", "value": "on"},
            "bot_fight_mode_v2": {"id": "bot_fight_mode_v2", "value": "on"},
        }
    }
    diff = monitor.compute_diff(old, new)
    msg = monitor.format_diff_for_telegram("example.com", "abc123", diff)
    plain = msg.replace("\\", "")

    assert "Cloudflare drift detected" in plain
    assert "example.com" in plain
    assert "abc123" in plain
    assert "Changed" in plain
    assert "http3" in plain
    assert "bot_fight_mode_v2" in plain
    assert "legacy" in plain


def test_format_diff_truncates_huge_messages():
    old = {f"k{i}": {"id": f"k{i}", "value": "x" * 50} for i in range(500)}
    new = {f"k{i}": {"id": f"k{i}", "value": "y" * 50} for i in range(500)}
    diff = monitor.compute_diff(old, new)
    msg = monitor.format_diff_for_telegram("z.com", "zid", diff)
    assert len(msg) <= monitor.TELEGRAM_MAX_LEN + 50


# ---------------------------------------------------------------------------
# Telegram + healthchecks
# ---------------------------------------------------------------------------


def test_ping_healthchecks_noop_when_url_missing(httpx_mock):
    monitor.ping_healthchecks(None)
    monitor.ping_healthchecks("")


def test_ping_healthchecks_calls_url(httpx_mock):
    httpx_mock.add_response(url="https://hc-ping.com/abc", status_code=200)
    httpx_mock.add_response(url="https://hc-ping.com/abc/start", status_code=200)
    httpx_mock.add_response(url="https://hc-ping.com/abc/fail", status_code=200)
    monitor.ping_healthchecks("https://hc-ping.com/abc")
    monitor.ping_healthchecks("https://hc-ping.com/abc", "/start")
    monitor.ping_healthchecks("https://hc-ping.com/abc", "/fail")


def test_ping_healthchecks_swallows_errors(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("nope"))
    monitor.ping_healthchecks("https://hc-ping.com/abc")


async def test_send_telegram_returns_true_on_success():
    bot = AsyncMock()
    assert await monitor.send_telegram(bot, "123", "hi") is True
    bot.send_message.assert_awaited_once()


async def test_send_telegram_returns_false_on_failure():
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("boom")
    assert await monitor.send_telegram(bot, "123", "hi") is False


# ---------------------------------------------------------------------------
# End-to-end run_once
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, *, healthchecks_url=None) -> monitor.Config:
    return monitor.Config(
        cloudflare_token="t",
        telegram_token="tt",
        telegram_chat_id="42",
        interval_seconds=1,
        snapshots_dir=tmp_path / "snapshots",
        healthchecks_url=healthchecks_url,
        request_delay_seconds=0.0,
    )


async def test_run_once_first_baseline_then_drift(tmp_path: Path, httpx_mock, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path)

    # First cycle.
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones?page=1&per_page=50",
        json=_zone_payload([{"id": "z1", "name": "example.com"}]),
    )
    _mock_full_zone(httpx_mock, "z1",
                    settings=[{"id": "http3", "value": "off"}])

    bot = AsyncMock()
    cf = _make_client(httpx_mock)
    ok = await monitor.run_once(cf, bot, cfg)
    assert ok is True
    bot.send_message.assert_not_awaited()
    snaps = list((cfg.snapshots_dir / "z1").glob("*.json"))
    assert len(snaps) == 1
    assert json.loads(snaps[0].read_text())["_schema"] == "v2"

    # Second cycle: http3 flips on, plus a new bot_fight_mode_v2 setting.
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones?page=1&per_page=50",
        json=_zone_payload([{"id": "z1", "name": "example.com"}]),
    )
    _mock_full_zone(
        httpx_mock, "z1",
        settings=[
            {"id": "http3", "value": "on"},
            {"id": "bot_fight_mode_v2", "value": "on"},
        ],
    )
    time.sleep(1.1)
    ok = await monitor.run_once(cf, bot, cfg)
    cf.close()

    assert ok is True
    bot.send_message.assert_awaited_once()
    sent = bot.send_message.await_args.kwargs["text"].replace("\\", "")
    assert "http3" in sent
    assert "bot_fight_mode_v2" in sent
    assert Path(".last_run").exists()


async def test_run_once_legacy_snapshot_silently_rebaselines(tmp_path: Path, httpx_mock, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path)

    # Pre-existing v1-style snapshot
    zone_dir = cfg.snapshots_dir / "z1"
    zone_dir.mkdir(parents=True)
    (zone_dir / "2020-01-01T00-00-00Z.json").write_text(
        json.dumps({"http3": {"id": "http3", "value": "off"}})
    )

    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones?page=1&per_page=50",
        json=_zone_payload([{"id": "z1", "name": "example.com"}]),
    )
    _mock_full_zone(httpx_mock, "z1",
                    settings=[{"id": "http3", "value": "on"}])  # totally different

    bot = AsyncMock()
    cf = _make_client(httpx_mock)
    ok = await monitor.run_once(cf, bot, cfg)
    cf.close()

    assert ok is True
    bot.send_message.assert_not_awaited()  # silent migration
    snaps = sorted((cfg.snapshots_dir / "z1").glob("*.json"))
    assert len(snaps) == 2
    assert json.loads(snaps[-1].read_text())["_schema"] == "v2"


async def test_run_once_list_zones_failure_returns_false(tmp_path: Path, httpx_mock, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path, healthchecks_url="https://hc-ping.com/abc")

    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones?page=1&per_page=50",
        status_code=500,
        json={"success": False},
    )
    httpx_mock.add_response(url="https://hc-ping.com/abc/start", status_code=200)
    httpx_mock.add_response(url="https://hc-ping.com/abc/fail", status_code=200)

    bot = AsyncMock()
    cf = _make_client(httpx_mock)
    ok = await monitor.run_once(cf, bot, cfg)
    cf.close()

    assert ok is False
    assert not Path(".last_run").exists()
    bot.send_message.assert_not_awaited()


async def test_run_once_telegram_failure_returns_false(tmp_path: Path, httpx_mock, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path, healthchecks_url="https://hc-ping.com/abc")

    # Seed baseline.
    cf = _make_client(httpx_mock)
    bot = AsyncMock()

    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones?page=1&per_page=50",
        json=_zone_payload([{"id": "z1", "name": "example.com"}]),
    )
    _mock_full_zone(httpx_mock, "z1", settings=[{"id": "http3", "value": "off"}])
    httpx_mock.add_response(url="https://hc-ping.com/abc/start", status_code=200)
    httpx_mock.add_response(url="https://hc-ping.com/abc", status_code=200)
    assert await monitor.run_once(cf, bot, cfg) is True

    time.sleep(1.1)
    # Cycle 2: drift → telegram fails.
    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones?page=1&per_page=50",
        json=_zone_payload([{"id": "z1", "name": "example.com"}]),
    )
    _mock_full_zone(httpx_mock, "z1", settings=[{"id": "http3", "value": "on"}])
    httpx_mock.add_response(url="https://hc-ping.com/abc/start", status_code=200)
    httpx_mock.add_response(url="https://hc-ping.com/abc/fail", status_code=200)

    Path(".last_run").unlink(missing_ok=True)
    bot.send_message.side_effect = RuntimeError("telegram down")
    ok = await monitor.run_once(cf, bot, cfg)
    cf.close()

    assert ok is False
    assert not Path(".last_run").exists()


async def test_run_once_zone_fetch_failure_returns_false(tmp_path: Path, httpx_mock, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path, healthchecks_url="https://hc-ping.com/abc")

    httpx_mock.add_response(
        url=f"{monitor.CLOUDFLARE_API_BASE}/zones?page=1&per_page=50",
        json=_zone_payload([{"id": "z1", "name": "example.com"}]),
    )
    # All endpoints 500 — every section errors.
    for path in (
        "/settings",
        "/argo/tiered_caching",
        "/cache/tiered_cache_smart_topology_enable",
        "/cache/cache_reserve",
        "/rulesets",
        "/pagerules",
    ):
        httpx_mock.add_response(
            url=f"{monitor.CLOUDFLARE_API_BASE}/zones/z1{path}",
            status_code=500,
            json={"success": False},
        )
    httpx_mock.add_response(url="https://hc-ping.com/abc/start", status_code=200)
    httpx_mock.add_response(url="https://hc-ping.com/abc/fail", status_code=200)

    bot = AsyncMock()
    cf = _make_client(httpx_mock)
    ok = await monitor.run_once(cf, bot, cfg)
    cf.close()

    assert ok is False
    assert not Path(".last_run").exists()
    # No snapshot written for the failed zone
    assert not (cfg.snapshots_dir / "z1").exists() or not list(
        (cfg.snapshots_dir / "z1").glob("*.json")
    )
