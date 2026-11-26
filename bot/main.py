from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv
from telegram.ext import Application, ApplicationBuilder

from bot.calendar.feed import CalendarFeedClient
from bot.calendar.notifications import enqueue_calendar_notifications
from bot.commands import publish_owner_commands, register_handlers
from bot.config import Settings
from bot.fetchers.base import BaseFetcher
from bot.fetchers.google_news import GoogleNewsFetcher
from bot.fetchers.rss import RssFetcher
from bot.pipeline import DeepSeekAnalyzer, GeminiAnalyzer, NewsPipeline
from bot.scheduler import (
    OwnerMessageQueue,
    fetch_and_enqueue,
    refresh_calendar_feed,
    start_scheduler,
)
from bot.sources import SOURCES
from bot.storage import StateStore

LOGGER = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    await publish_owner_commands(application, settings.owner_chat_id)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    store = StateStore(str(settings.db_path))
    await store.open()
    fetchers: dict[str, BaseFetcher] = {
        "rss": RssFetcher(store),
        "google_news": GoogleNewsFetcher(store),
    }
    messages = OwnerMessageQueue(application.bot, settings.owner_chat_id)
    pipeline = NewsPipeline(
        GeminiAnalyzer(
            settings.gemini_api_key,
            settings.gemini_model,
            settings.gemini_max_output_tokens,
        ),
        DeepSeekAnalyzer(
            settings.deepseek_api_key,
            settings.deepseek_model,
            settings.deepseek_max_output_tokens,
            base_url=settings.deepseek_base_url,
        ),
        gemini_calls_per_cycle=settings.gemini_calls_per_cycle,
        deepseek_daily_call_cap=settings.deepseek_daily_call_cap,
    )
    calendar_feed_client = CalendarFeedClient(
        settings.calendar_feed_url,
        store,
    )
    application.bot_data.update(
        state_store=store,
        fetchers=fetchers,
        news_pipeline=pipeline,
        calendar_feed_client=calendar_feed_client,
        owner_messages=messages,
        delivery_task=asyncio.create_task(messages.run()),
    )

    await fetch_and_enqueue(fetchers, SOURCES, messages, store, pipeline)
    await refresh_calendar_feed(store, calendar_feed_client)
    await enqueue_calendar_notifications(
        store,
        messages,
        settings.timezone,
        settings.calendar_reminder_minutes,
    )
    scheduler = start_scheduler(
        fetchers,
        SOURCES,
        messages,
        store,
        pipeline,
        settings.fetch_interval_minutes,
        settings.timezone,
        calendar_feed_client,
        settings.calendar_reminder_minutes,
        settings.digest_time,
    )
    application.bot_data["scheduler"] = scheduler
    LOGGER.info("Feed polling scheduler started")


async def post_shutdown(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)

    delivery_task: asyncio.Task[None] | None = application.bot_data.get(
        "delivery_task"
    )
    if delivery_task is not None:
        delivery_task.cancel()
        try:
            await delivery_task
        except asyncio.CancelledError:
            pass

    fetchers: dict[str, BaseFetcher] = application.bot_data.get("fetchers", {})
    for fetcher in fetchers.values():
        await fetcher.close()

    pipeline: NewsPipeline | None = application.bot_data.get("news_pipeline")
    if pipeline is not None:
        await pipeline.close()

    calendar_feed_client: CalendarFeedClient | None = application.bot_data.get(
        "calendar_feed_client"
    )
    if calendar_feed_client is not None:
        await calendar_feed_client.close()

    store: StateStore | None = application.bot_data.get("state_store")
    if store is not None:
        await store.close()


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    register_handlers(application)
    application.run_polling(allowed_updates=[])


if __name__ == "__main__":
    main()
