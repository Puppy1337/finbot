"""Сквозной тест без реальных сетевых вызовов (Telegram/Claude/курсы/STT замоканы).
Запуск: python -m tests.test_flow
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finbot.bot import FinBot  # noqa: E402
from finbot.claude import Claude  # noqa: E402
from finbot.config import Settings  # noqa: E402
from finbot.db import Database  # noqa: E402
from finbot.rates import Rates  # noqa: E402
from finbot.stt import SpeechToText  # noqa: E402
from finbot.telegram import Telegram  # noqa: E402

SENT: list[dict] = []
DOWNLOADS: set = set()
CLAUDE_REQUESTS: list[dict] = []
CLAUDE_REPLY = {"transactions": [], "reply": "ok"}


def tg_handler(request: httpx.Request) -> httpx.Response:
    method = request.url.path.split("/")[-1]
    if request.url.path.startswith("/file/"):
        return httpx.Response(200, content=b"%PDF-1.4 fake" if "d1" in DOWNLOADS else b"\x89PNG fake")
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        body = json.loads(request.content)
    SENT.append({"method": method, **({k: v for k, v in body.items()} if body else {})})
    if method == "getFile":
        DOWNLOADS.clear(); DOWNLOADS.add(body.get("file_id", ""))
        return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/1.ogg"}})
    if method in ("sendPhoto", "sendDocument"):
        SENT[-1]["multipart"] = True
    return httpx.Response(200, json={"ok": True, "result": {"message_id": len(SENT), "chat": {"id": 1}}})


def claude_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    CLAUDE_REQUESTS.append(body)
    if body.get("tools"):
        return httpx.Response(200, json={"content": [
            {"type": "tool_use", "id": "t1", "name": "process_message", "input": CLAUDE_REPLY}]})
    return httpx.Response(200, json={"content": [{"type": "text", "text": "Совет: меньше кофе."}]})


def rates_handler(request: httpx.Request) -> httpx.Response:
    base = request.url.params.get("from")
    rates = {"USD": {"EUR": 0.9, "UAH": 41.0}, "EUR": {"USD": 1.1, "UAH": 45.0}}[base]
    return httpx.Response(200, json={"base": base, "rates": rates})


def stt_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"text": "кофе четыре доллара"})


def msg(text=None, uid=1, **extra) -> dict:
    m = {"message_id": 1, "from": {"id": uid, "first_name": "Антон"}, "chat": {"id": uid, "type": "private"}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": 1, "message": m}


async def run():
    global CLAUDE_REPLY
    os.environ["TELEGRAM_BOT_TOKEN"] = "x"
    os.environ["ANTHROPIC_API_KEY"] = "x"
    os.environ["ALLOWED_USER_IDS"] = "1"
    s = Settings.load()
    s.stt_api_key = "k"
    tmp = tempfile.mkdtemp()
    db = Database(os.path.join(tmp, "t.db"))
    tg = Telegram("x", transport=httpx.MockTransport(tg_handler))
    claude = Claude("x", "claude-sonnet-5", transport=httpx.MockTransport(claude_handler))
    rates = Rates(transport=httpx.MockTransport(rates_handler))
    stt = SpeechToText("openai", "k", transport=httpx.MockTransport(stt_handler))
    bot = FinBot(s, db, tg, claude, rates, stt)
    today = datetime.now(ZoneInfo(s.timezone)).date()

    # 1. /start
    await bot.handle_update(msg("/start"))
    assert "Привет" in SENT[-1]["text"], SENT[-1]

    # 2. доступ закрыт чужому
    await bot.handle_update(msg("/start", uid=2))
    assert "Доступ закрыт" in SENT[-1]["text"]

    # 3. трата текстом в евро -> конвертация в USD
    CLAUDE_REPLY = {"transactions": [
        {"type": "expense", "amount": 10, "currency": "EUR", "category": "Кафе и рестораны",
         "description": "кофе", "date": today.isoformat()}],
        "reply": "Кофе каждый день — это 300 в месяц."}
    await bot.handle_update(msg("кофе 10 евро"))
    out = SENT[-1]
    assert "Записал" in out["text"] and "10 EUR" in out["text"] and "≈11.11 USD" in out["text"], out["text"]
    assert out["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("undo:")
    assert "КОНТЕКСТ ПОЛЬЗОВАТЕЛЯ" in CLAUDE_REQUESTS[-1]["system"]
    assert CLAUDE_REQUESTS[-1]["tool_choice"]["name"] == "process_message"

    # 4. бюджет + предупреждение
    await bot.handle_update(msg("/budget кафе 12"))
    assert "Бюджет «Кафе и рестораны»" in SENT[-1]["text"], SENT[-1]["text"]
    CLAUDE_REPLY = {"transactions": [
        {"type": "expense", "amount": 3, "currency": "USD", "category": "Кафе и рестораны",
         "description": "капучино", "date": today.isoformat()}], "reply": "Хм."}
    await bot.handle_update(msg("капучино 3"))
    assert "Бюджет «Кафе и рестораны» превышен" in SENT[-1]["text"], SENT[-1]["text"]

    # 5. отмена через кнопку
    cb_data = SENT[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await bot.handle_update({"update_id": 9, "callback_query": {
        "id": "cb1", "from": {"id": 1}, "data": cb_data,
        "message": {"message_id": 5, "chat": {"id": 1}, "text": "старый текст"}}})
    assert any(x["method"] == "editMessageText" and "Отменено" in x["text"] for x in SENT[-2:]), SENT[-2:]
    assert db.total(1, today, today, "expense") == 11.11

    # 6. голос
    CLAUDE_REPLY = {"transactions": [
        {"type": "expense", "amount": 4, "currency": "USD", "category": "Еда", "description": "кофе",
         "date": today.isoformat()}], "reply": "ok"}
    await bot.handle_update(msg(voice={"file_id": "v1", "duration": 3}))
    assert "🎤" in SENT[-1]["text"] and "кофе четыре доллара" in SENT[-1]["text"], SENT[-1]["text"]

    # 7. скриншот -> картинка попадает в запрос; дубликаты пропускаются
    CLAUDE_REPLY = {"transactions": [
        {"type": "expense", "amount": 4, "currency": "USD", "category": "Еда", "description": "кофе",
         "date": today.isoformat()},
        {"type": "income", "amount": 1000, "currency": "USD", "category": "Зарплата", "description": "аванс",
         "date": (today - timedelta(days=1)).isoformat()}], "reply": "Аванс пришёл."}
    await bot.handle_update(msg(photo=[{"file_id": "p1"}], caption="вот"))
    assert CLAUDE_REQUESTS[-1]["messages"][-1]["content"][0]["type"] == "image"
    assert "Пропустил 1" in SENT[-1]["text"] and "1 000 USD" in SENT[-1]["text"], SENT[-1]["text"]

    # 7b. PDF-выписка: документ уходит в Claude, много операций, кнопка отмены влезает в 64 байта
    CLAUDE_REPLY = {"transactions": [
        {"type": "expense", "amount": 1 + i, "currency": "EUR", "category": "Еда", "description": f"магазин {i}",
         "date": (today - timedelta(days=i % 5)).isoformat()} for i in range(30)], "reply": "Выписка разобрана."}
    await bot.handle_update(msg(document={"file_id": "d1", "mime_type": "application/pdf",
                                          "file_name": "statement.pdf", "file_size": 12345}))
    req = CLAUDE_REQUESTS[-1]
    assert req["messages"][-1]["content"][0]["type"] == "document", req["messages"][-1]["content"][0]
    assert req["messages"][-1]["content"][0]["source"]["media_type"] == "application/pdf"
    assert req["max_tokens"] == 16000
    out = SENT[-1]
    assert "statement.pdf" in out["text"] and "Записал (30)" in out["text"] and "и ещё 5" in out["text"], out["text"]
    cb = out["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert len(cb.encode()) <= 64 and "-" in cb, cb
    before = len(db.all_transactions(1))
    await bot.handle_update({"update_id": 10, "callback_query": {
        "id": "cb2", "from": {"id": 1}, "data": cb, "message": {"message_id": 6, "chat": {"id": 1}, "text": "x"}}})
    assert len(db.all_transactions(1)) == before - 30
    # PDF с вопросом -> после записи идёт второй запрос (analyze), ответ форматируется в HTML
    CLAUDE_REPLY = {"transactions": [
        {"type": "expense", "amount": 7, "currency": "EUR", "category": "Транспорт", "description": "ridenow",
         "date": (today - timedelta(days=40)).isoformat()}], "reply": "ok"}
    n_req = len(CLAUDE_REQUESTS)
    await bot.handle_update(msg(document={"file_id": "d1", "mime_type": "application/pdf",
                                          "file_name": "st2.pdf", "file_size": 100}, caption="сколько по месяцам?"))
    assert len(CLAUDE_REQUESTS) == n_req + 2 and "tools" not in CLAUDE_REQUESTS[-1]
    assert "разбивка по месяцам" in CLAUDE_REQUESTS[-1]["system"], CLAUDE_REQUESTS[-1]["system"][-800:]
    assert "меньше кофе" in SENT[-1]["text"]
    # правила пользователя попадают в system prompt
    await bot.handle_update(msg("/rules Отвечай абзацами с эмодзи"))
    assert "Запомнил" in SENT[-1]["text"]
    await bot.handle_update(msg("/rules + Переводы друзьям не траты"))
    await bot.handle_update(msg("сколько я потратил?"))
    assert "ЛИЧНЫЕ ПРАВИЛА" in CLAUDE_REQUESTS[-1]["system"] and "Переводы друзьям" in CLAUDE_REQUESTS[-1]["system"]
    await bot.handle_update(msg("/months"))
    assert "По месяцам" in SENT[-1]["text"] and "Транспорт" in SENT[-1]["text"], SENT[-1]["text"]
    # PDF без извлечённых операций -> страховочный текстовый запрос с приложенным документом
    CLAUDE_REPLY = {"transactions": [], "reply": "см. ниже полный разбор"}
    n_req = len(CLAUDE_REQUESTS)
    await bot.handle_update(msg(document={"file_id": "d1", "mime_type": "application/pdf",
                                          "file_name": "st3.pdf", "file_size": 100}))
    assert len(CLAUDE_REQUESTS) == n_req + 2
    last = CLAUDE_REQUESTS[-1]
    assert "tools" not in last and last["messages"][-1]["content"][0]["type"] == "document"
    assert "меньше кофе" in SENT[-1]["text"] and "см. ниже" not in SENT[-1]["text"], SENT[-1]["text"]
    # слишком большой PDF
    await bot.handle_update(msg(document={"file_id": "d2", "mime_type": "application/pdf",
                                          "file_name": "big.pdf", "file_size": 50 * 1024 * 1024}))
    assert "слишком большой" in SENT[-1]["text"]

    # 8. вопрос без операций
    CLAUDE_REPLY = {"transactions": [], "reply": "Вы потратили 15 USD."}
    await bot.handle_update(msg("сколько я потратил?"))
    assert SENT[-1]["text"] == "Вы потратили 15 USD." and "reply_markup" not in SENT[-1], SENT[-1]
    assert len(CLAUDE_REQUESTS[-1]["messages"]) > 1  # история диалога передаётся

    # 9. цели и накопления
    await bot.handle_update(msg("/goal Отпуск 2000 31.12.2026"))
    assert "Цель «Отпуск»" in SENT[-1]["text"]
    await bot.handle_update(msg("/save отпуск 500"))
    assert "25%" in SENT[-1]["text"], SENT[-1]["text"]

    # 10. отчёты с графиком
    await bot.handle_update(msg("/month"))
    assert SENT[-1]["method"] == "sendPhoto" and SENT[-2]["method"] == "sendMessage"
    await bot.handle_update(msg("/today"))
    assert "Сегодня" in SENT[-1]["text"]
    await bot.handle_update(msg("/advice"))
    assert "меньше кофе" in SENT[-1]["text"]
    await bot.handle_update(msg("/export"))
    assert SENT[-1]["method"] == "sendDocument"

    # 11. напоминания
    await bot.handle_update(msg("/remind 21:00"))
    assert "21:00" in SENT[-1]["text"]
    now = datetime.now(ZoneInfo(s.timezone)).replace(hour=21, minute=0)
    await bot.scheduler_tick(now)
    assert "Не забудьте" in SENT[-1]["text"], SENT[-1]
    n = len(SENT)
    await bot.scheduler_tick(now)  # второй раз в тот же день не шлёт
    assert len(SENT) == n
    # еженедельный отчёт
    monday9 = now.replace(hour=9, minute=0) - timedelta(days=now.weekday())
    await bot.scheduler_tick(monday9)
    assert any("Итоги прошлой недели" in x.get("text", "") for x in SENT[n:]), SENT[n:]

    # 12. /undo и /currency
    await bot.handle_update(msg("/undo"))
    assert "Удалено" in SENT[-1]["text"]
    await bot.handle_update(msg("/currency евро"))
    assert "EUR" in SENT[-1]["text"]

    # график в файл для визуальной проверки
    from finbot import reports
    png = reports.chart_png(db.sum_by_category(1, *reports.month_range(today)),
                            reports.daily_totals(db.transactions_between(1, *reports.month_range(today)),
                                                 *reports.month_range(today)), "USD", "Тест", {"Еда": 100})
    open(os.path.join(tmp, "chart.png"), "wb").write(png)
    from finbot.claude import to_telegram_html
    h = to_telegram_html("## Итог\n**Еда** 120 € <5%\n- пункт *важно*\n* ещё `код`")
    assert h == "<b>Итог</b>\n<b>Еда</b> 120 € &lt;5%\n• пункт <i>важно</i>\n• ещё <code>код</code>", h
    print("chart:", os.path.join(tmp, "chart.png"))
    print("OK — все проверки пройдены, сообщений отправлено:", len(SENT))


if __name__ == "__main__":
    asyncio.run(run())
