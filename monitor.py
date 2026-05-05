"""Cloudflare configuration drift monitor.

Polls Cloudflare zone settings + selected per-zone resources on a fixed
interval, snapshots them as JSON, diffs each snapshot against the previous
one with deepdiff, and posts a Markdown alert to Telegram whenever anything
changes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from deepdiff import DeepDiff
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
LAST_RUN_MARKER = Path(".last_run")
TELEGRAM_MAX_LEN = 3800  # leave headroom under the 4096 hard limit
SNAPSHOT_SCHEMA = "v2"

logger = logging.getLogger("cf-watchdog")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    cloudflare_token: str
    telegram_token: str
    telegram_chat_id: str
    interval_seconds: int
    snapshots_dir: Path
    healthchecks_url: str | None
    request_delay_seconds: float = 2.0


def load_config() -> Config:
    """Load config from environment (with optional .env file)."""
    load_dotenv()

    required = ("CLOUDFLARE_API_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")

    interval_hours = float(os.getenv("CHECK_INTERVAL_HOURS", "6"))
    snapshots_dir = Path(os.getenv("SNAPSHOTS_DIR", "./snapshots")).resolve()
    request_delay = float(os.getenv("CLOUDFLARE_REQUEST_DELAY", "2.0"))

    return Config(
        cloudflare_token=os.environ["CLOUDFLARE_API_TOKEN"],
        telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
        interval_seconds=int(interval_hours * 3600),
        snapshots_dir=snapshots_dir,
        healthchecks_url=(os.getenv("HEALTHCHECKS_URL") or "").rstrip("/") or None,
        request_delay_seconds=request_delay,
    )


# ---------------------------------------------------------------------------
# Healthchecks.io
# ---------------------------------------------------------------------------


def ping_healthchecks(base_url: str | None, suffix: str = "") -> None:
    """Ping a healthchecks.io URL. Suffix is '', '/start', or '/fail'."""
    if not base_url:
        return
    url = base_url + suffix
    try:
        httpx.get(url, timeout=10.0)
    except Exception as exc:  # noqa: BLE001 - never break the audit
        logger.warning("healthchecks ping (%s) failed: %s", suffix or "ok", exc)


# ---------------------------------------------------------------------------
# Cloudflare client
# ---------------------------------------------------------------------------


# HTTP statuses that mean "the feature/resource isn't enabled on this plan"
# rather than a real error. We capture them in the snapshot as `_status: code`
# so a future plan upgrade surfaces as drift.
_GRACEFUL_STATUSES = (403, 404, 405)


class CloudflareClient:
    """Thin wrapper around the Cloudflare REST API."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        request_delay_seconds: float = 0.0,
        sleep: Any = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(
            base_url=CLOUDFLARE_API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._request_delay = request_delay_seconds
        self._sleep = sleep
        self._first_request = True

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CloudflareClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- internals -----------------------------------------------------------

    def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        if self._first_request:
            self._first_request = False
        elif self._request_delay > 0:
            self._sleep(self._request_delay)
        return self._client.get(path, **kwargs)

    def _get_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        r = self._get(path, **kwargs)
        r.raise_for_status()
        payload = r.json()
        if not payload.get("success", False):
            raise RuntimeError(f"Cloudflare API error at {path}: {payload.get('errors')}")
        return payload

    def _get_result_or_status(self, path: str) -> dict[str, Any]:
        """Fetch a single-resource endpoint, treating 'feature not available'
        statuses as a stable, diff-friendly placeholder."""
        r = self._get(path)
        if r.status_code in _GRACEFUL_STATUSES:
            return {"_status": r.status_code}
        r.raise_for_status()
        payload = r.json()
        if not payload.get("success", False):
            raise RuntimeError(f"Cloudflare API error at {path}: {payload.get('errors')}")
        return payload.get("result") or {}

    # -- endpoints -----------------------------------------------------------

    def list_zones(self) -> list[dict[str, Any]]:
        """Return every zone visible to the token (paginated)."""
        zones: list[dict[str, Any]] = []
        page = 1
        per_page = 50
        while True:
            payload = self._get_json(
                "/zones", params={"page": page, "per_page": per_page}
            )
            result = payload.get("result", [])
            zones.extend(result)
            info = payload.get("result_info") or {}
            total_pages = info.get("total_pages", 1)
            if page >= total_pages or not result:
                break
            page += 1
        return zones

    def get_zone_settings(self, zone_id: str) -> dict[str, dict[str, Any]]:
        """Return zone settings keyed by setting id (stable across reorders)."""
        payload = self._get_json(f"/zones/{zone_id}/settings")
        result = payload.get("result", [])
        return {item["id"]: item for item in result if "id" in item}

    def get_argo_tiered_caching(self, zone_id: str) -> dict[str, Any]:
        return self._get_result_or_status(f"/zones/{zone_id}/argo/tiered_caching")

    def get_tiered_cache_smart_topology(self, zone_id: str) -> dict[str, Any]:
        return self._get_result_or_status(
            f"/zones/{zone_id}/cache/tiered_cache_smart_topology_enable"
        )

    def get_cache_reserve(self, zone_id: str) -> dict[str, Any]:
        return self._get_result_or_status(f"/zones/{zone_id}/cache/cache_reserve")

    def list_rulesets(self, zone_id: str) -> list[dict[str, Any]]:
        payload = self._get_json(f"/zones/{zone_id}/rulesets")
        return payload.get("result") or []

    def get_ruleset(self, zone_id: str, ruleset_id: str) -> dict[str, Any]:
        # Some rulesets (Cloudflare-managed) are listable but not deeply readable
        # by the caller's token — that's a plan/scope thing, not a real error.
        return self._get_result_or_status(f"/zones/{zone_id}/rulesets/{ruleset_id}")

    def list_pagerules(self, zone_id: str) -> list[dict[str, Any]]:
        payload = self._get_json(f"/zones/{zone_id}/pagerules")
        return payload.get("result") or []


# ---------------------------------------------------------------------------
# Per-zone state aggregator
# ---------------------------------------------------------------------------


def collect_zone_state(
    cf: CloudflareClient, zone_id: str
) -> tuple[dict[str, Any], list[str]]:
    """Build the v2 snapshot dict for a zone.

    Returns (snapshot, errors). `errors` is a list of human-readable strings;
    when non-empty, the cycle is considered failed (no success healthcheck
    ping, no .last_run update).
    """
    errors: list[str] = []
    snapshot: dict[str, Any] = {"_schema": SNAPSHOT_SCHEMA}

    def _section(name: str, fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            return None

    settings = _section("settings", cf.get_zone_settings, zone_id)
    if settings is not None:
        snapshot["settings"] = settings

    argo = _section("argo_tiered_caching", cf.get_argo_tiered_caching, zone_id)
    if argo is not None:
        snapshot["argo_tiered_caching"] = argo

    smart = _section(
        "tiered_cache_smart_topology", cf.get_tiered_cache_smart_topology, zone_id
    )
    if smart is not None:
        snapshot["tiered_cache_smart_topology"] = smart

    reserve = _section("cache_reserve", cf.get_cache_reserve, zone_id)
    if reserve is not None:
        snapshot["cache_reserve"] = reserve

    rulesets_list = _section("rulesets", cf.list_rulesets, zone_id)
    if rulesets_list is not None:
        rulesets: dict[str, dict[str, Any]] = {}
        for rs in rulesets_list:
            rs_id = rs.get("id")
            if not rs_id:
                continue
            detail = _section(f"ruleset {rs_id}", cf.get_ruleset, zone_id, rs_id)
            if detail is not None:
                rulesets[rs_id] = detail
        snapshot["rulesets"] = rulesets

    pagerules_list = _section("pagerules", cf.list_pagerules, zone_id)
    if pagerules_list is not None:
        snapshot["pagerules"] = {
            pr["id"]: pr for pr in pagerules_list if "id" in pr
        }

    return snapshot, errors


# ---------------------------------------------------------------------------
# Snapshot store
# ---------------------------------------------------------------------------


def _zone_dir(snapshots_dir: Path, zone_id: str) -> Path:
    d = snapshots_dir / zone_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_snapshot(snapshots_dir: Path, zone_id: str, data: dict[str, Any]) -> Path:
    """Write a timestamped snapshot file and return its path."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = _zone_dir(snapshots_dir, zone_id) / f"{ts}.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    return path


def load_latest_snapshot(
    snapshots_dir: Path, zone_id: str, *, exclude: Path | None = None
) -> dict[str, Any] | None:
    """Return the most recent snapshot for a zone, or None if none exists."""
    d = snapshots_dir / zone_id
    if not d.exists():
        return None
    files = sorted(p for p in d.glob("*.json") if p != exclude)
    if not files:
        return None
    return json.loads(files[-1].read_text())


def is_legacy_snapshot(snapshot: dict[str, Any] | None) -> bool:
    """A v1 snapshot is a flat {setting_id: {...}} without our schema sentinel."""
    if not snapshot:
        return False
    return snapshot.get("_schema") != SNAPSHOT_SCHEMA


# ---------------------------------------------------------------------------
# Diff + formatting
# ---------------------------------------------------------------------------


def compute_diff(old: dict[str, Any], new: dict[str, Any]) -> DeepDiff:
    """Diff two snapshots, surfacing added/removed values explicitly."""
    return DeepDiff(old, new, verbose_level=2, ignore_order=True)


def _short(value: Any, limit: int = 200) -> str:
    s = json.dumps(value, default=str, sort_keys=True) if not isinstance(value, str) else value
    return s if len(s) <= limit else s[: limit - 1] + "…"


def format_diff_for_telegram(zone_name: str, zone_id: str, diff: DeepDiff) -> str:
    """Build a MarkdownV2 message describing the drift."""
    lines = [
        f"🚨 *Cloudflare drift detected*",
        f"Zone: *{escape_markdown(zone_name, version=2)}* "
        f"\\(`{escape_markdown(zone_id, version=2)}`\\)",
    ]

    changed = diff.get("values_changed", {}) | diff.get("type_changes", {})
    added = diff.get("dictionary_item_added", {}) | diff.get("iterable_item_added", {})
    removed = diff.get("dictionary_item_removed", {}) | diff.get(
        "iterable_item_removed", {}
    )

    if changed:
        lines.append("\n🔁 *Changed*")
        for path, info in changed.items():
            old = _short(info.get("old_value"))
            new = _short(info.get("new_value"))
            lines.append(
                f"• `{escape_markdown(path, version=2)}`: "
                f"`{escape_markdown(old, version=2)}` → "
                f"`{escape_markdown(new, version=2)}`"
            )

    if added:
        lines.append("\n🆕 *New* \\(likely a Cloudflare\\-added feature or new rule\\)")
        for path, value in added.items():
            lines.append(
                f"• `{escape_markdown(path, version=2)}` \\= "
                f"`{escape_markdown(_short(value), version=2)}`"
            )

    if removed:
        lines.append("\n❌ *Removed*")
        for path, value in removed.items():
            lines.append(
                f"• `{escape_markdown(path, version=2)}` "
                f"\\(was `{escape_markdown(_short(value), version=2)}`\\)"
            )

    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_LEN:
        text = text[:TELEGRAM_MAX_LEN] + "\n…\\(truncated\\)"
    return text


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


async def send_telegram(bot: Bot, chat_id: str, text: str) -> bool:
    """Send a MarkdownV2 message. Returns True on success, False on failure."""
    try:
        await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN_V2
        )
        return True
    except Exception as exc:  # noqa: BLE001 - never crash the loop on notify failure
        logger.error("Telegram send failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# One audit cycle
# ---------------------------------------------------------------------------


async def run_once(cf: CloudflareClient, bot: Bot, cfg: Config) -> bool:
    """Run a single audit pass over every zone.

    Returns True iff the entire cycle succeeded:
    - zone listing worked
    - every per-zone fetch succeeded (graceful 4xx for unavailable features
      does NOT count as failure)
    - every Telegram drift alert that was attempted was delivered.
    """
    ping_healthchecks(cfg.healthchecks_url, "/start")
    failures: list[str] = []

    try:
        zones = cf.list_zones()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to list zones: %s", exc)
        ping_healthchecks(cfg.healthchecks_url, "/fail")
        return False

    logger.info("Auditing %d zone(s)", len(zones))

    for zone in zones:
        zone_id = zone["id"]
        zone_name = zone.get("name", zone_id)

        new_state, zone_errors = collect_zone_state(cf, zone_id)
        if zone_errors:
            for err in zone_errors:
                logger.error("[%s] %s", zone_name, err)
                failures.append(f"{zone_name}: {err}")
            # Don't snapshot a partial state — it would create false drift.
            continue

        previous = load_latest_snapshot(cfg.snapshots_dir, zone_id)
        snapshot_path = save_snapshot(cfg.snapshots_dir, zone_id, new_state)

        if previous is None:
            logger.info("[%s] baseline snapshot saved (%s)", zone_name, snapshot_path.name)
            continue

        if is_legacy_snapshot(previous):
            logger.info(
                "[%s] legacy snapshot detected, re-baselining to v2 (%s)",
                zone_name,
                snapshot_path.name,
            )
            continue

        diff = compute_diff(previous, new_state)
        if not diff:
            logger.info("[%s] no changes", zone_name)
            continue

        logger.warning("[%s] drift detected: %s", zone_name, list(diff.keys()))
        message = format_diff_for_telegram(zone_name, zone_id, diff)
        delivered = await send_telegram(bot, cfg.telegram_chat_id, message)
        if not delivered:
            failures.append(f"{zone_name}: telegram alert delivery failed")

    if failures:
        logger.error("Cycle finished with %d failure(s)", len(failures))
        ping_healthchecks(cfg.healthchecks_url, "/fail")
        return False

    LAST_RUN_MARKER.write_text(datetime.now(timezone.utc).isoformat())
    ping_healthchecks(cfg.healthchecks_url)
    return True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def main_once(cfg: Config) -> None:
    """Run a single audit cycle and exit."""
    cfg.snapshots_dir.mkdir(parents=True, exist_ok=True)
    async with Bot(cfg.telegram_token) as bot:
        with CloudflareClient(
            cfg.cloudflare_token,
            request_delay_seconds=cfg.request_delay_seconds,
        ) as cf:
            await run_once(cf, bot, cfg)


async def main_loop(cfg: Config) -> None:
    cfg.snapshots_dir.mkdir(parents=True, exist_ok=True)

    stop_event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            signal.signal(sig, _handle_signal)

    async with Bot(cfg.telegram_token) as bot:
        with CloudflareClient(
            cfg.cloudflare_token,
            request_delay_seconds=cfg.request_delay_seconds,
        ) as cf:
            while not stop_event.is_set():
                try:
                    await run_once(cf, bot, cfg)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Unexpected error in audit cycle: %s", exc)
                logger.info("Sleeping %d seconds", cfg.interval_seconds)
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=cfg.interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Cloudflare configuration drift monitor")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single audit cycle and exit (no sleep loop).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    cfg = load_config()
    if args.once:
        asyncio.run(main_once(cfg))
    else:
        asyncio.run(main_loop(cfg))


if __name__ == "__main__":
    main()
