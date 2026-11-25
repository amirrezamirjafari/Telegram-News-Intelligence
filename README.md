# Telegram-News-Intelligence

## Current behavior

The bot polls the editable feed list in `bot/sources.py`, silently seeds
existing stories on its first complete fetch, filters irrelevant items,
deduplicates across sources, and analyzes new stories. High- and medium-impact
Persian analyses are sent only to `OWNER_CHAT_ID`; low-impact analyses are
stored for a later digest. High-impact calendar events also get a reminder 30
minutes before release, with actual results sent once when available. Owner-only
commands are available; messages and callbacks from other chats are ignored.
Per-feed ETag/Last-Modified values, calendar events, and notification state are
stored in SQLite; a news feed is disabled after five consecutive failed polling
cycles.

## Setup

Requires Python 3.13 and a Telegram bot token. Create a virtual environment,
install the dependencies, and configure the environment:

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Set the required values in `.env`. The bot sends only filtered story titles and
short feed summaries to the analyzer; it does not send source names, links, or
full articles.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token |
| `OWNER_CHAT_ID` | Yes | — | Numeric ID of the only chat allowed to receive messages and use commands |
| `API_KEY` | Yes | — | API key for the analyzer |
| `MODEL` | No | `gpt-4o-mini` | Model sent in the OpenAI Chat Completions request |
| `FETCH_INTERVAL_MINUTES` | No | `10` | Feed polling interval |
| `DIGEST_TIME` | No | `21:00` | Daily digest time in 24-hour `HH:MM` format |
| `TIMEZONE` | No | `Asia/Tehran` | IANA timezone for the digest and calendar display |
| `DB_PATH` | No | `data/newsbot.sqlite3` | SQLite database file path |
| `CALENDAR_FEED_URL` | No | `https://nfs.faireconomy.media/ff_calendar_thisweek.json` | Candidate public weekly calendar JSON feed; verify provider availability and schema before production use |
| `CALENDAR_REMINDER_MINUTES` | No | `30` | Minutes before a high-impact event to send a reminder |

The analyzer currently sends OpenAI Chat Completions requests to the fixed
endpoint `https://api.openai.com/v1/chat/completions`. `API_KEY` and `MODEL`
are configurable; there is no configurable API base URL.

Run the bot from the repository root:

```sh
python -m bot.main
```

It fetches feeds at startup and then polls on the configured interval. HTTP
requests use conditional headers when a feed supplies ETag or Last-Modified,
with retries and a browser-like User-Agent. Investing.com may deny automated
requests; its 403 responses are reported and count toward the five-failure
disable threshold. Outgoing messages are queued and delivered at no more than
one message per second.

The configured FXStreet, CNBC Markets, CNBC Economy, MarketWatch, Investing.com,
investingLive Central Banks, investingLive Breaking News, investingLive Forex
Orders, and Reuters Google News endpoints were checked and returned valid feeds.
The `https://investinglive.com/rss` address is an HTML discovery page, not a feed.
Reuters is queried through Google News and is not an official Reuters RSS feed.
The bot stores titles, short summaries, and links, never full articles.

## Railway deployment

The repository includes a `.python-version` pin for Python 3.13 and a
`Procfile` worker process that runs `python -m bot.main`. Railway detects
`requirements.txt` and installs the listed dependencies during the build. This
is a long-running worker; it does not serve HTTP traffic or require a public
domain.

1. Create a Railway project and deploy this repository as a service.
2. In the service's Variables settings, configure the three required values
   (`TELEGRAM_BOT_TOKEN`, `OWNER_CHAT_ID`, and `API_KEY`). Set optional values
   from the table above only when you want to override their defaults. Do not
   commit `.env` or put secret values in source control.
3. Add a Railway Volume to the service and mount it at `/data`. Set
   `DB_PATH=/data/newsbot.sqlite3` in the service variables. The SQLite
   database contains deduplication state, source settings, calendar cache, and
   deferred analyses; without the mounted volume these are not guaranteed to
   survive redeployments.
4. Deploy the service and check its logs for startup errors. Railway restarts
   the worker on failure; no deployment or Railway resources are created by
   this repository.

The default `CALENDAR_FEED_URL` remains a candidate Forex Factory-compatible
weekly JSON feed, not a verified provider contract. Verify its URL and response
schema before relying on calendar notifications in production.

## Milestone 2: silent seeding and deduplication

On its first complete fetch cycle, the bot records existing stories without
sending them. It then sends only unseen items, suppressing canonical URL and
normalized-title matches plus near-identical normalized titles across sources.
The SQLite database stores the normalized identities and their SHA-256 hashes.
Conditional HTTP validators are used after initial seeding, so upgrades from
earlier databases also fetch full feeds during the seed cycle.

## Milestone 3: keyword filtering and market classification

The title and, when available, a short feed summary are matched against
currency/pair, gold/DXY, central-bank, macro-data, major-index, and stock
keywords before storage or delivery. Irrelevant entries are dropped; retained
entries are labeled `forex`, `global_stock`, or `macro`. Summaries are used only
for matching and are stored with retained titles and links, capped at 500
characters; raw feed summaries are not sent to Telegram and full articles are
never stored.

## Milestone 4: Persian analysis and delivery

The analyzer validates the required JSON fields and retries once when the
response is malformed or has an invalid schema. It receives only each retained
story's title and capped feed summary. High- and medium-impact analyses are
formatted as escaped Telegram HTML and sent to the owner, including the source,
published time, link, and financial disclaimer. Low-impact analyses are stored
in SQLite with `deferred` status for a future digest; no digest is implemented
in this milestone. Configure `DB_PATH` on a persistent volume to retain these
analyses.

## Milestone 5: economic calendar

The calendar module reads a structured weekly JSON feed only; it does not
scrape Forex Factory pages. Feed failures or malformed data leave the last good
SQLite cache in use. Event times are stored in UTC and displayed in `TIMEZONE`.
High-impact reminders and actual-result notifications are recorded before
delivery so they are not duplicated after a restart. Missing actual, forecast,
or previous values are displayed as unavailable. No calendar command or digest
is included.

## Milestone 6: commands, settings, and digest

The owner can use `/start`, `/status`, `/sources`, `/markets`, `/calendar`,
`/digest`, `/pause`, and `/resume`. Source and market toggles, pause state, and
digest delivery state persist in SQLite. `/pause` stops scheduled polling,
calendar checks, and automatic digests while leaving owner commands available.
The low-impact digest runs daily at `DIGEST_TIME` in `TIMEZONE`; `/digest`
sends pending items immediately.
