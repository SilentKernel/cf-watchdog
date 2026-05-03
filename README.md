# cf-watchdog

Monitors Cloudflare zone settings and pings Telegram whenever something
changes — modified values, brand new fields, or removed ones.

## Why

On the **Cloudflare Free plan**, Cloudflare occasionally changes zone
parameters on its own — enabling HTTP/3, turning on Bot Fight Mode, rolling
out new features as additional fields in the API response, etc. These
changes happen without notification and can break production (caching
behavior shifts, legitimate traffic gets challenged, TLS handshakes fail
on old clients…).

`cf-watchdog` snapshots your zone settings on a schedule and alerts you the
moment anything drifts, so you find out from Telegram instead of from your
users.

## How it works

1. Every `CHECK_INTERVAL_HOURS`, fetches every zone visible to the API token.
2. Pulls the full `/zones/{id}/settings` payload for each one.
3. Snapshots it to `snapshots/{zone_id}/{ISO_timestamp}.json`.
4. Diffs against the previous snapshot with `deepdiff`.
5. If anything changed (modified value, new key, removed key) → Telegram alert.

The first run for a new zone establishes a baseline; no alert is sent.

## Setup

### 1. Cloudflare API token

Create a token at https://dash.cloudflare.com/profile/api-tokens with:
- **Zone → Zone → Read**
- **Zone → Zone Settings → Read**

Scope: *All zones* (or the specific zones you want to watch).

### 2. Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
2. Send any message to your bot (so it can DM you back).
3. Get your chat id from [@userinfobot](https://t.me/userinfobot) (it's the
   numeric `Id` field). For groups, add the bot and use the group's negative
   `chat.id` from `https://api.telegram.org/bot<TOKEN>/getUpdates`.

### 3. `.env`

Copy `.env.example` to `.env` and fill in:

```env
CLOUDFLARE_API_TOKEN=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
CHECK_INTERVAL_HOURS=6
SNAPSHOTS_DIR=./snapshots
```

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
container rebuilds. The container's healthcheck fails if `.last_run` is
older than `2 × CHECK_INTERVAL_HOURS`.

## Tests

```bash
pytest -v
```

## Maintenance

Snapshots accumulate forever. Prune occasionally if needed:

```bash
find snapshots -name '*.json' -mtime +90 -delete
```
