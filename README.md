# Telegram-News-Intelligence

## Current behavior

The bot polls the editable feed list in `bot/sources.py`, silently seeds
existing stories on its first complete fetch, filters irrelevant items, and
deduplicates across sources. Gemini translates/summarizes and scores each new
story; only high-impact scores are sent to DeepSeek for a second, source-grounded
possible-impact assessment. Low- and medium-impact stories are eligible for the
existing digest. Every generated summary and inference is labelled unverified;
the bot never presents model analysis as independently confirmed fact or a
trading signal. High-impact calendar events also get a reminder 30 minutes
before release, with actual results sent once when available. Owner-only
commands are available; messages and callbacks from other chats are ignored.
Per-feed validators, analysis-stage results, provider-call usage, calendar
events, and notification state are stored in SQLite; a news feed is disabled
after five consecutive failed polling cycles.

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
short feed summaries to Gemini; DeepSeek receives only the original source
title and short source summary for high-impact stories. Neither provider
receives source names, links, or full articles.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token |
| `OWNER_CHAT_ID` | Yes | — | Numeric ID of the only chat allowed to receive messages and use commands |
| `GEMINI_API_KEY` | Yes | — | Google Gemini API key |
| `DEEPSEEK_API_KEY` | Yes | — | DeepSeek API key |
| `GEMINI_MODEL` | No | `gemini-3.5-flash-lite` | Gemini translation and initial scoring model |
| `DEEPSEEK_MODEL` | No | `manageit/deepseek-v4.1-flash` | DeepSeek high-impact follow-up model |
| `DEEPSEEK_BASE_URL` | No | `https://ai.manageit.dev/api/v1` | OpenAI-compatible DeepSeek API base URL |
| `GEMINI_CALLS_PER_CYCLE` | No | `20` | Maximum Gemini HTTP requests per feed poll |
| `DEEPSEEK_DAILY_CALL_CAP` | No | `10` | Maximum DeepSeek HTTP requests per UTC day, enforced before each request |
| `GEMINI_MAX_OUTPUT_TOKENS` | No | `512` | Per-request Gemini output cap |
| `DEEPSEEK_MAX_OUTPUT_TOKENS` | No | `512` | Per-request DeepSeek output cap |
| `FETCH_INTERVAL_MINUTES` | No | `10` | Feed polling interval |
| `DIGEST_TIME` | No | `21:00` | Daily digest time in 24-hour `HH:MM` format |
| `TIMEZONE` | No | `Asia/Tehran` | IANA timezone for the digest and calendar display |
| `DB_PATH` | No | `data/newsbot.sqlite3` | SQLite database file path |
| `CALENDAR_FEED_URL` | No | `https://nfs.faireconomy.media/ff_calendar_thisweek.json` | Candidate public weekly calendar JSON feed; verify provider availability and schema before production use |
| `CALENDAR_REMINDER_MINUTES` | No | `30` | Minutes before a high-impact event to send a reminder |

Gemini uses the official `generateContent` REST API with a JSON response
schema; DeepSeek uses Chat Completions JSON mode at
`DEEPSEEK_BASE_URL/chat/completions`. Both requests have a
30-second timeout and at most one retry for network errors or transient HTTP
responses (429/5xx), with bounded backoff. Invalid response shapes or schemas
are errors, not success-shaped fallbacks. Each stage is persisted so completed
work is not billed again on later polls/restarts. A stage left in progress after
a crash is marked failed on startup to avoid repeating a possibly billable
request. Invalid JSON/schema responses get one bounded retry, budgeted as a
separate request. Failed stages are excluded from automatic reprocessing and
counted in `/status`; they require operator review rather than silent retries.

The Gemini cap is per polling cycle (a retry uses another request slot). The
DeepSeek cap is per UTC day and is reserved atomically before every request,
including retries; if exhausted, high-impact work stays pending until the next
day. These controls limit request volume and output tokens; actual spend also
depends on current provider pricing and input-token rates.

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
2. In the service's Variables settings, configure the four required values
   (`TELEGRAM_BOT_TOKEN`, `OWNER_CHAT_ID`, `GEMINI_API_KEY`, and
   `DEEPSEEK_API_KEY`). Set optional values from the table above only when you
   want to override their defaults. Do not commit `.env` or put secret values
   in source control.
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

## Milestone 4: two-stage Persian analysis and delivery

After keyword filtering, deduplication, and silent initial seeding, Gemini
returns a validated Persian summary, assets, sentiment, impact, reasoning, and
self-reported confidence using the official structured `generateContent` API.
Only high-impact items (the conservative definition of "important") proceed to
DeepSeek. DeepSeek receives only the original source title and capped source
summary—not Gemini's generated text—and returns a conditional possible-impact
scenario plus an uncertainty caveat. Low- and medium-impact summaries are
stored for the digest without DeepSeek calls.

Both stages, their statuses, and outputs are persisted per story. Duplicate
stories are discarded before provider calls; completed stages are not repeated
after polling or restart. Transient network errors and HTTP 429/5xx responses
receive at most one bounded retry. Invalid JSON, unsupported response shapes,
and schema violations fail the stage explicitly without a success-shaped
fallback. The Gemini request cap is per poll; DeepSeek requests are reserved
atomically against a configurable UTC daily cap. Per-request output token
limits further bound usage. Messages are escaped Telegram HTML, include source,
time, and link, and state that translation and claims are unverified, the
impact is a possible scenario, and the result is not a trading signal or
investment advice. Configure `DB_PATH` on persistent storage to retain stage
and deduplication state.

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
`/digest`, `/pause`, and `/resume`. The typo alias `/calender` also opens the
calendar. Status, sources, markets, and calendar commands are published in the
owner's Telegram command menu. Source and market toggles, pause state, and
digest delivery state persist in SQLite. `/pause` stops scheduled polling,
calendar checks, and automatic digests while leaving owner commands available.
The low- and medium-impact digest runs daily at `DIGEST_TIME` in `TIMEZONE`;
`/digest` sends pending items immediately.
