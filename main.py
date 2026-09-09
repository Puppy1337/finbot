"""Точка входа: python main.py"""
import asyncio
import logging

from finbot.bot import FinBot
from finbot.claude import Claude
from finbot.config import Settings
from finbot.db import Database
from finbot.rates import Rates
from finbot.stt import SpeechToText
from finbot.telegram import Telegram


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = Settings.load()
    db = Database(s.db_path)
    tg = Telegram(s.telegram_token)
    claude = Claude(s.anthropic_api_key, s.anthropic_model, s.anthropic_base_url)
    rates = Rates()
    stt = SpeechToText(s.stt_provider, s.stt_api_key, s.stt_base_url, s.stt_model)
    bot = FinBot(s, db, tg, claude, rates, stt)
    try:
        await bot.run()
    finally:
        await tg.close()
        await claude.close()
        await rates.close()
        await stt.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
