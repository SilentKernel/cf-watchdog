# cf-watchdog

Monitors Cloudflare zone configuration on a schedule and pings Telegram
whenever something drifts — modified values, new fields, removed ones,
ruleset edits, page rule changes, anything.

## Why

On the **Cloudflare Free plan**, Cloudflare occasionally changes zone
parameters on its own — enabling HTTP/3, turning on Bot Fight Mode, rolling
out new features as additional fields in the API response, etc. These
changes happen without notification and can break production (caching
behavior shifts, legitimate traffic gets challenged, TLS handshakes fail
on old clients…).

`cf-watchdog` snapshots your zone configuration on a schedule and alerts
you the moment anything drifts, so you find out from Telegram instead of
from your users.

## What it monitors per zone

Each cycle, for every zone visible to the API token, the following endpoints
are fetched and stored in a single per-zone snapshot:

| Endpoint | What it covers |
| --- | --- |
| `GET /zones/{id}/settings` | All zone settings (HTTP/3, TLS, Bot Fight Mode, etc.) |
| `GET /zones/{id}/argo/tiered_caching` | Argo Tiered Caching state |
| `GET /zones/{id}/cache/tiered_cache_smart_topology_enable` | Smart Tiered Cache |
| `GET /zones/{id}/cache/cache_reserve` | Cache Reserve setting |
| `GET /zones/{id}/rulesets` + `GET /zones/{id}/rulesets/{rs_id}` | Every ruleset, including all rules |
| `GET /zones/{id}/pagerules` | All Page Rules |

Endpoints that return **403 / 404 / 405** (feature not on this plan, e.g.
Argo not subscribed) are recorded as `{"_status": <code>}` rather than
treated as errors. If you later upgrade and the feature becomes available,
that transition surfaces as drift like any other.

## How it works

1. Every `CHECK_INTERVAL_HOURS`, fetch every zone the token can see.
2. Pull all 6 endpoints above and assemble a `v2` snapshot per zone.
3. Persist to `snapshots/{zone_id}/{ISO_timestamp}.json`.
4. Diff against the previous snapshot with `deepdiff`.
5. If anything changed → Telegram alert.

The first run for a new zone establishes a baseline; no alert is sent.
Existing v1 (settings-only) snapshots are detected via a schema sentinel
and silently re-baselined to v2 on the first upgraded run.

### Alerting policy

Two independent channels:

- **Telegram** — drift alerts.
- **healthchecks.io** *(optional)* — only pings success when the **entire**
  cycle succeeded: zone listing worked, every per-zone fetch worked
  (graceful 403/404 do not count as failure), and every Telegram alert that
  was attempted was delivered. Any failure → `/fail` is pinged and
  `.last_run` is **not** written. The Docker `HEALTHCHECK` reads `.last_run`
  and goes red after `2 × CHECK_INTERVAL_HOURS` of staleness.

This means a Telegram outage during a real drift event cannot pass silently:
the strict success ping is skipped, healthchecks.io alerts you out-of-band.

## Setup

### 1. Cloudflare API token

Create a token at https://dash.cloudflare.com/profile/api-tokens with these
**read** permissions on the zones you want to watch:

- Zone → Zone → Read
- Zone → Zone Settings → Read
- Zone → Cache Settings → Read *(for `cache_reserve`, tiered caching)*
- Zone → Page Rules → Read
- Zone → Zone WAF → Read *(for rulesets)*

Missing scopes don't crash the run — the affected zone reports an error
that turn, the cycle is marked failed, and healthchecks.io fires.

### 2. Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
2. Send any message to your bot (so it can DM you back).
3. Get your chat id from [@userinfobot](https://t.me/userinfobot).

### 3. `.env`

Copy `.env.example` to `.env` and fill in:

```env
CLOUDFLARE_API_TOKEN=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
CHECK_INTERVAL_HOURS=6
SNAPSHOTS_DIR=./snapshots
CLOUDFLARE_REQUEST_DELAY=2.0
HEALTHCHECKS_URL=
```

`CLOUDFLARE_REQUEST_DELAY` (seconds) is inserted between every Cloudflare
API call to avoid bursting. Default `2.0`. Set to `0` to disable.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python monitor.py            # forever loop
python monitor.py --once     # single audit cycle, then exit
```

## Run in Docker

```bash
docker compose up -d --build
docker compose logs -f
```

The `snapshots/` directory is mounted as a volume so history survives
container rebuilds.

## Tests

```bash
pytest -v
```

## Maintenance

Snapshots accumulate forever. Prune occasionally if needed:

```bash
find snapshots -name '*.json' -mtime +90 -delete
```
