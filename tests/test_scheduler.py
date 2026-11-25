from __future__ import annotations

import asyncio
import time
import unittest

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.scheduler import MESSAGE_INTERVAL_SECONDS, OwnerMessageQueue


class RecordingBot:
    def __init__(self) -> None:
        self.calls: list[tuple[float, int, str]] = []
        self.markups: list[InlineKeyboardMarkup | None] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        self.calls.append((time.monotonic(), chat_id, text))
        self.markups.append(reply_markup)


class OwnerMessageQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_messages_are_sent_to_owner_at_one_second_intervals(self) -> None:
        bot = RecordingBot()
        queue = OwnerMessageQueue(bot, owner_chat_id=123)
        delivery_task = asyncio.create_task(queue.run())
        await queue.put("first")
        await queue.put("second")

        await queue.stop()
        await delivery_task

        self.assertEqual(MESSAGE_INTERVAL_SECONDS, 1.0)
        self.assertEqual(
            [call[1:] for call in bot.calls],
            [(123, "first"), (123, "second")],
        )
        self.assertGreaterEqual(bot.calls[1][0] - bot.calls[0][0], 0.99)

    async def test_queue_delivers_inline_markup_only_to_owner(self) -> None:
        bot = RecordingBot()
        queue = OwnerMessageQueue(bot, owner_chat_id=123)
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Toggle", callback_data="market:macro")]]
        )
        delivery_task = asyncio.create_task(queue.run(interval_seconds=0))
        await queue.put("Choose an option", reply_markup=markup)
        await queue.stop()
        await delivery_task

        self.assertEqual(bot.calls[0][1:], (123, "Choose an option"))
        self.assertEqual(bot.markups, [markup])
