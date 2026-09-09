"""Логика бота: команды, обработка сообщений, напоминания."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from . import reports
from .claude import Claude, ClaudeError, history_entry, to_telegram_html
from .config import Settings
from .db import EXPENSE_CATEGORIES, INCOME_CATEGORIES, Database, Transaction
from .rates import Rates, normalize_currency
from .reports import fmt, month_range, today_in, tx_line, week_range
from .stt import STTError, SpeechToText
from .telegram import Telegram, esc, inline_keyboard

log = logging.getLogger(__name__)

MAX_PDF_BYTES = 20 * 1024 * 1024  # лимит Telegram Bot API на скачивание файлов

COMMANDS = [
    ("start", "Начало работы"),
    ("help", "Как пользоваться"),
    ("today", "Траты за сегодня"),
    ("week", "Отчёт за неделю"),
    ("month", "Отчёт за месяц с графиком"),
    ("months", "Сравнение последних месяцев"),
    ("advice", "Развёрнутый совет, как сэкономить"),
    ("budget", "Бюджеты по категориям"),
    ("goal", "Цели накопления"),
    ("save", "Отложить на цель"),
    ("remind", "Напоминания"),
    ("currency", "Базовая валюта"),
    ("undo", "Удалить последнюю запись"),
    ("export", "Выгрузить все данные в CSV"),
    ("rules", "Личные правила для бота"),
]

HELP = """<b>Как пользоваться</b>

✍️ <b>Просто пишите</b>: «кофе 4.5», «такси 12 евро вчера», «зарплата 3000», «отложил 200 на отпуск».
🎤 <b>Голосом</b>: наговорите траты голосовым сообщением.
📸 <b>Скриншот из банка</b>: пришлите картинку — бот вытащит операции.
📄 <b>PDF-выписка</b>: пришлите файл из банка — бот разберёт все операции (или перескажет любой другой финансовый документ).
❓ <b>Вопросы</b>: «сколько я потратил на еду?», «хватит ли до зарплаты?», «на чём сэкономить?».

<b>Команды</b>
/today — сегодня · /week — неделя · /month — месяц + график · /months — по месяцам
/advice — подробный разбор и план экономии
/budget Еда 300 — лимит на категорию в месяц (/budget — список)
/goal Отпуск 2000 2026-12-31 — цель (/goal — список)
/save Отпуск 100 — отложить на цель
/remind 21:00 — ежедневное напоминание (/remind off — выключить)
/currency EUR — базовая валюта
/undo — удалить последнюю запись · /export — CSV со всеми данными
/rules — ваши постоянные правила для бота (стиль ответов, что считать тратой и т.п.)

Под каждой записью есть кнопка «Отменить», если бот понял неверно."""


def parse_amount(s: str) -> Optional[float]:
    s = s.strip().replace(" ", "").replace(",", ".")
    try:
        v = float(s)
        return v if v >= 0 else None
    except ValueError:
        return None


def parse_date(s: str) -> Optional[str]:
    s = s.strip()
    for f in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    return None


def match_category(name: str, type_: str) -> str:
    if type_ == "saving":
        return "Накопления"
    pool = EXPENSE_CATEGORIES if type_ == "expense" else INCOME_CATEGORIES
    n = (name or "").strip().lower()
    for c in pool:
        if c.lower() == n:
            return c
    for c in pool:
        if n and (n in c.lower() or c.lower() in n):
            return c
    return "Другое"


class FinBot:
    def __init__(self, settings: Settings, db: Database, tg: Telegram, claude: Claude, rates: Rates, stt: SpeechToText):
        self.s = settings
        self.db = db
        self.tg = tg
        self.claude = claude
        self.rates = rates
        self.stt = stt
        self.history: dict[int, list[dict]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    # ---------- запуск ----------
    async def run(self) -> None:
        try:
            await self.tg.set_commands(COMMANDS)
        except Exception as e:
            log.warning("setMyCommands: %s", e)
        log.info("Бот запущен, ожидаю сообщения…")
        scheduler = asyncio.create_task(self.scheduler_loop())
        offset: Optional[int] = None
        try:
            while True:
                updates = await self.tg.get_updates(offset)
                for u in updates:
                    offset = u["update_id"] + 1
                    asyncio.create_task(self._safe_handle(u))
        finally:
            scheduler.cancel()

    async def _safe_handle(self, update: dict) -> None:
        try:
            await self.handle_update(update)
        except Exception:
            log.exception("Ошибка обработки обновления")
            chat_id = (update.get("message") or {}).get("chat", {}).get("id")
            if chat_id:
                try:
                    await self.tg.send_message(chat_id, "⚠️ Что-то пошло не так. Попробуйте ещё раз.")
                except Exception:
                    pass

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    # ---------- маршрутизация ----------
    async def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self.handle_callback(update["callback_query"])
            return
        msg = update.get("message")
        if not msg:
            return
        from_user = msg.get("from") or {}
        user_id = from_user.get("id")
        chat_id = msg["chat"]["id"]
        if not user_id or msg["chat"].get("type") != "private":
            return
        if self.s.allowed_user_ids and user_id not in self.s.allowed_user_ids:
            await self.tg.send_message(chat_id, f"⛔ Доступ закрыт. Ваш ID: <code>{user_id}</code> — добавьте его в ALLOWED_USER_IDS.")
            return
        name = " ".join(x for x in (from_user.get("first_name"), from_user.get("last_name")) if x) or "Пользователь"
        user = self.db.ensure_user(user_id, name, self.s.base_currency)

        async with self._lock(user_id):
            text = msg.get("text")
            if text and text.startswith("/"):
                await self.handle_command(user, chat_id, text)
            elif text:
                await self.process_input(user, chat_id, text=text, source="text")
            elif msg.get("voice") or msg.get("audio"):
                await self.handle_voice(user, chat_id, msg.get("voice") or msg.get("audio"))
            elif msg.get("photo"):
                await self.handle_photo(user, chat_id, msg["photo"][-1]["file_id"], msg.get("caption"), "image/jpeg")
            elif msg.get("document") and str(msg["document"].get("mime_type", "")).startswith("image/"):
                await self.handle_photo(user, chat_id, msg["document"]["file_id"], msg.get("caption"),
                                        msg["document"]["mime_type"])
            elif msg.get("document") and (str(msg["document"].get("mime_type", "")) == "application/pdf"
                                          or str(msg["document"].get("file_name", "")).lower().endswith(".pdf")):
                await self.handle_pdf(user, chat_id, msg["document"], msg.get("caption"))
            else:
                await self.tg.send_message(chat_id, "Я понимаю текст, голосовые, скриншоты и PDF 🙂 /help")

    # ---------- команды ----------
    async def handle_command(self, user, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0].lstrip("/")
        arg = parts[1].strip() if len(parts) > 1 else ""
        uid = user["user_id"]
        cur = user["currency"]
        tz = self.s.timezone
        today = today_in(tz)

        if cmd == "start":
            await self.tg.send_message(
                chat_id,
                f"Привет, {esc(user['name'])}! 👋 Я ваш финансовый аналитик.\n\n"
                f"Записывайте траты текстом, голосом или скриншотами — я всё учту, буду следить за бюджетами "
                f"и подсказывать, как сохранить деньги.\n\nБазовая валюта: <b>{cur}</b> (изменить: /currency EUR).\n\n" + HELP,
            )
        elif cmd == "help":
            await self.tg.send_message(chat_id, HELP)
        elif cmd == "id":
            await self.tg.send_message(chat_id, f"Ваш Telegram ID: <code>{uid}</code>")
        elif cmd == "today":
            txs = self.db.transactions_between(uid, today, today)
            head = reports.period_report(self.db, uid, cur, today, today, "Сегодня")
            body = "\n".join(tx_line(t, cur) for t in txs)
            await self.tg.send_message(chat_id, head + ("\n\n<b>Операции:</b>\n" + body if body else ""))
        elif cmd == "week":
            await self.send_period(uid, chat_id, cur, *week_range(today), "Неделя", chart=True)
        elif cmd == "month":
            await self.send_period(uid, chat_id, cur, *month_range(today), today.strftime("Месяц %m.%Y"), chart=True)
        elif cmd == "months":
            n = int(arg) if arg.isdigit() and 1 <= int(arg) <= 24 else 6
            await self.tg.send_message(chat_id, reports.months_report(self.db, uid, cur, n))
        elif cmd == "advice":
            await self.cmd_advice(uid, chat_id)
        elif cmd == "rules":
            await self.cmd_rules(user, chat_id, arg)
        elif cmd == "budget":
            await self.cmd_budget(uid, chat_id, cur, arg)
        elif cmd == "goal":
            await self.cmd_goal(uid, chat_id, cur, arg)
        elif cmd == "save":
            await self.cmd_save(user, chat_id, arg)
        elif cmd == "remind":
            await self.cmd_remind(uid, chat_id, arg)
        elif cmd == "currency":
            await self.cmd_currency(uid, chat_id, arg)
        elif cmd == "undo":
            t = self.db.delete_last(uid)
            await self.tg.send_message(chat_id, ("🗑 Удалено: " + tx_line(t, cur)) if t else "Удалять нечего.")
        elif cmd == "export":
            data = reports.export_csv(self.db, uid)
            await self.tg.send_document(chat_id, data, f"finance_{today.isoformat()}.csv",
                                        "Все операции (CSV, разделитель «;»)")
        else:
            await self.tg.send_message(chat_id, "Не знаю такой команды. /help")

    async def send_period(self, uid: int, chat_id: int, cur: str, start: date, end: date, title: str, chart: bool) -> None:
        text = reports.period_report(self.db, uid, cur, start, end, title)
        await self.tg.send_message(chat_id, text)
        if chart:
            txs = self.db.transactions_between(uid, start, end)
            if any(t.type == "expense" for t in txs):
                try:
                    by_cat = self.db.sum_by_category(uid, start, end, "expense")
                    png = reports.chart_png(by_cat, reports.daily_totals(txs, start, end), cur,
                                            f"{title}: расходы", self.db.budgets(uid))
                    await self.tg.send_photo(chat_id, png)
                except Exception:
                    log.exception("Не удалось построить график")

    async def cmd_advice(self, uid: int, chat_id: int) -> None:
        await self.tg.send_chat_action(chat_id)
        u = self.db.get_user(uid)
        ctx = reports.build_context(self.db, uid, u["currency"], self.s.timezone)
        try:
            text = await self.claude.analyze(
                ctx,
                "Сделай разбор моих финансов за текущий месяц: где перерасход, что необычного по сравнению с прошлым "
                "месяцем, как идут бюджеты и цели. Дай 3–5 конкретных шагов, как сэкономить, с оценкой суммы экономии "
                "в месяц. Если данных мало — скажи, что именно ещё стоит записывать.",
                instructions=u["instructions"],
            )
        except ClaudeError as e:
            text = f"⚠️ Не удалось получить анализ: {e}"
        await self.tg.send_message(chat_id, to_telegram_html(text))

    async def cmd_rules(self, user, chat_id: int, arg: str) -> None:
        uid = user["user_id"]
        current = self.db.get_user(uid)["instructions"]
        if not arg:
            body = f"<b>Ваши правила:</b>\n{esc(current)}" if current else "Правил пока нет."
            await self.tg.send_message(
                chat_id, body + "\n\nЗадать: <code>/rules текст правил</code> (можно несколько строк).\n"
                "Добавить к существующим: <code>/rules + ещё правило</code>.\nУдалить все: <code>/rules off</code>.\n\n"
                "Пример: <code>/rules Отвечай абзацами с эмодзи. Переводы друзьям не считай тратами. Подписки выделяй отдельно.</code>")
            return
        if arg.lower() in ("off", "clear", "удалить", "сброс"):
            self.db.set_instructions(uid, "")
            await self.tg.send_message(chat_id, "Личные правила удалены.")
            return
        if arg.startswith("+"):
            new = (current + "\n" + arg[1:].strip()).strip()
        else:
            new = arg.strip()
        self.db.set_instructions(uid, new[:2000])
        await self.tg.send_message(chat_id, f"✅ Запомнил. Теперь бот всегда учитывает:\n{esc(new[:2000])}")

    async def cmd_budget(self, uid: int, chat_id: int, cur: str, arg: str) -> None:
        if not arg:
            await self.tg.send_message(chat_id, reports.budgets_report(self.db, uid, cur, self.s.timezone))
            return
        m = re.match(r"^(.*?)[\s:=]+([\d\s.,]+)$", arg)
        if not m:
            await self.tg.send_message(chat_id, "Формат: <code>/budget Категория Сумма</code>, например <code>/budget Еда 300</code>")
            return
        cat = match_category(m.group(1), "expense")
        amount = parse_amount(m.group(2))
        if amount is None:
            await self.tg.send_message(chat_id, "Не понял сумму. Пример: <code>/budget Еда 300</code>")
            return
        if amount == 0:
            ok = self.db.delete_budget(uid, cat)
            await self.tg.send_message(chat_id, f"Бюджет «{esc(cat)}» удалён." if ok else f"Бюджета «{esc(cat)}» не было.")
            return
        self.db.set_budget(uid, cat, amount)
        note = "" if m.group(1).strip().lower() == cat.lower() else f" (категорию «{esc(m.group(1).strip())}» отнёс к «{esc(cat)}»)"
        await self.tg.send_message(chat_id, f"✅ Бюджет «{esc(cat)}»: {fmt(amount, cur)} в месяц{note}.\n\n"
                                   + reports.budgets_report(self.db, uid, cur, self.s.timezone))

    async def cmd_goal(self, uid: int, chat_id: int, cur: str, arg: str) -> None:
        if not arg:
            await self.tg.send_message(chat_id, reports.goals_report(self.db, uid, cur, self.s.timezone))
            return
        low = arg.lower()
        for prefix in ("удалить ", "delete ", "del "):
            if low.startswith(prefix):
                name = arg[len(prefix):].strip()
                ok = self.db.delete_goal(uid, name)
                await self.tg.send_message(chat_id, f"Цель «{esc(name)}» удалена." if ok else f"Цель «{esc(name)}» не найдена.")
                return
        m = re.match(r"^(.*?)\s+([\d\s.,]+?)(?:\s+(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{2,4}))?$", arg)
        if not m or parse_amount(m.group(2)) is None:
            await self.tg.send_message(chat_id, "Формат: <code>/goal Название Сумма [Дата]</code>, например "
                                       "<code>/goal Отпуск 2000 2026-12-31</code>")
            return
        name, target = m.group(1).strip(), parse_amount(m.group(2))
        deadline = parse_date(m.group(3)) if m.group(3) else None
        if m.group(3) and not deadline:
            await self.tg.send_message(chat_id, "Дата в формате ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")
            return
        if self.db.find_goal(uid, name):
            self.db.delete_goal(uid, name)
        self.db.add_goal(uid, name, target, deadline)
        await self.tg.send_message(chat_id, f"🎯 Цель «{esc(name)}» на {fmt(target, cur)} сохранена.\n\n"
                                   + reports.goals_report(self.db, uid, cur, self.s.timezone))

    async def cmd_save(self, user, chat_id: int, arg: str) -> None:
        uid, cur = user["user_id"], user["currency"]
        m = re.match(r"^(.*?)\s+([\d\s.,]+)$", arg or "")
        if not m or not parse_amount(m.group(2)):
            await self.tg.send_message(chat_id, "Формат: <code>/save Название_цели Сумма</code>")
            return
        name, amount = m.group(1).strip(), parse_amount(m.group(2))
        g = self.db.find_goal(uid, name)
        goal_name = g["name"] if g else name
        self.db.add_transaction(uid, "saving", amount, cur, amount, "Накопления", f"Пополнение цели «{goal_name}»",
                                today_in(self.s.timezone).isoformat(), "manual", goal=goal_name)
        await self.tg.send_message(chat_id, f"🏦 Отложено {fmt(amount, cur)} на «{esc(goal_name)}».\n\n"
                                   + reports.goals_report(self.db, uid, cur, self.s.timezone))

    async def cmd_remind(self, uid: int, chat_id: int, arg: str) -> None:
        u = self.db.get_user(uid)
        if not arg:
            state = f"ежедневно в {u['remind_time']}" if u["remind_time"] else "выключено"
            weekly = "включён" if u["weekly_report"] else "выключен"
            await self.tg.send_message(
                chat_id,
                f"⏰ Напоминание записать траты: <b>{state}</b>.\n📊 Еженедельный отчёт ({self.s.weekly_report_time}, "
                f"{['пн','вт','ср','чт','пт','сб','вс'][self.s.weekly_report_day]}): <b>{weekly}</b>.\n\n"
                "Команды: <code>/remind 21:00</code>, <code>/remind off</code>, <code>/remind weekly on|off</code>",
            )
            return
        low = arg.lower().strip()
        if low in ("off", "выкл", "стоп", "нет"):
            self.db.set_remind_time(uid, None)
            await self.tg.send_message(chat_id, "Ежедневное напоминание выключено.")
            return
        if low.startswith("weekly"):
            on = not low.endswith(("off", "выкл"))
            self.db.set_weekly_report(uid, on)
            await self.tg.send_message(chat_id, "Еженедельный отчёт " + ("включён." if on else "выключен."))
            return
        m = re.match(r"^(\d{1,2})[:.](\d{2})$", low)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            await self.tg.send_message(chat_id, "Укажите время в формате ЧЧ:ММ, например <code>/remind 21:00</code>")
            return
        value = f"{int(m.group(1)):02d}:{m.group(2)}"
        self.db.set_remind_time(uid, value)
        await self.tg.send_message(chat_id, f"⏰ Буду напоминать каждый день в {value} ({self.s.timezone}).")

    async def cmd_currency(self, uid: int, chat_id: int, arg: str) -> None:
        if not arg:
            await self.tg.send_message(chat_id, f"Базовая валюта: <b>{self.db.get_user(uid)['currency']}</b>. "
                                       "Изменить: <code>/currency EUR</code>\n\nВажно: уже записанные суммы не пересчитываются.")
            return
        code = normalize_currency(arg, "")
        if not code:
            await self.tg.send_message(chat_id, "Укажите код валюты: USD, EUR, UAH, PLN…")
            return
        self.db.set_currency(uid, code)
        await self.tg.send_message(chat_id, f"✅ Базовая валюта теперь <b>{code}</b>.")

    # ---------- голос и фото ----------
    async def handle_voice(self, user, chat_id: int, voice: dict) -> None:
        if not self.stt.available:
            await self.tg.send_message(chat_id, "🎤 Распознавание голоса не настроено (STT_API_KEY в .env). Пока напишите текстом.")
            return
        await self.tg.send_chat_action(chat_id)
        try:
            audio = await self.tg.download_file(voice["file_id"])
            text = await self.stt.transcribe(audio)
        except STTError as e:
            await self.tg.send_message(chat_id, f"⚠️ Не удалось распознать голос: {esc(e)}")
            return
        await self.process_input(user, chat_id, text=text, source="voice", prefix=f"🎤 <i>{esc(text)}</i>\n\n")

    async def handle_photo(self, user, chat_id: int, file_id: str, caption: Optional[str], mime: str) -> None:
        await self.tg.send_chat_action(chat_id)
        image = await self.tg.download_file(file_id)
        if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            mime = "image/jpeg"
        await self.process_input(user, chat_id, text=caption, source="photo", image=image, image_mime=mime)

    async def handle_pdf(self, user, chat_id: int, doc: dict, caption: Optional[str]) -> None:
        size = int(doc.get("file_size") or 0)
        if size > MAX_PDF_BYTES:
            await self.tg.send_message(chat_id, f"📄 Файл слишком большой ({size // 1024 // 1024} МБ). "
                                       f"Максимум {MAX_PDF_BYTES // 1024 // 1024} МБ — выгрузите выписку за меньший период.")
            return
        await self.tg.send_chat_action(chat_id, "upload_document")
        pdf = await self.tg.download_file(doc["file_id"])
        if not pdf.startswith(b"%PDF"):
            await self.tg.send_message(chat_id, "📄 Это не похоже на PDF-файл.")
            return
        name = doc.get("file_name") or "document.pdf"
        if size > 200 * 1024:
            await self.tg.send_message(chat_id, "📄 Читаю выписку… Большой файл может занять несколько минут.")
        await self.process_input(user, chat_id, text=caption, source="pdf", pdf=pdf, pdf_name=name,
                                 prefix=f"📄 <i>{esc(name)}</i>\n\n")

    # ---------- главный обработчик ----------
    async def process_input(self, user, chat_id: int, text: Optional[str], source: str,
                            image: Optional[bytes] = None, image_mime: str = "image/jpeg", prefix: str = "",
                            pdf: Optional[bytes] = None, pdf_name: str = "document.pdf") -> None:
        uid, cur, tz = user["user_id"], user["currency"], self.s.timezone
        await self.tg.send_chat_action(chat_id)
        ctx = reports.build_context(self.db, uid, cur, tz)
        hist = self.history.setdefault(uid, [])
        instructions = self.db.get_user(uid)["instructions"]
        try:
            result = await self.claude.process(ctx, text, hist, image_bytes=image, image_media_type=image_mime,
                                               pdf_bytes=pdf, pdf_name=pdf_name, instructions=instructions)
        except ClaudeError as e:
            log.error("Claude: %s", e)
            await self.tg.send_message(chat_id, f"⚠️ Не удалось связаться с аналитиком: {esc(e)}")
            return

        saved_ids: list[int] = []
        lines: list[str] = []
        skipped = 0
        today = today_in(tz)
        for t in result["transactions"]:
            try:
                amount = abs(float(t.get("amount", 0)))
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            type_ = t.get("type") if t.get("type") in ("expense", "income", "saving") else "expense"
            currency = normalize_currency(t.get("currency"), cur)
            tx_date = parse_date(str(t.get("date") or "")) or today.isoformat()
            if date.fromisoformat(tx_date) > today + timedelta(days=1):
                tx_date = today.isoformat()
            category = match_category(t.get("category", ""), type_)
            desc = (t.get("description") or "").strip()[:120]
            goal = (t.get("goal") or "").strip() or None
            if goal:
                g = self.db.find_goal(uid, goal)
                goal = g["name"] if g else goal
            if source in ("photo", "pdf") and self.db.is_duplicate(uid, amount, currency, tx_date, desc):
                skipped += 1
                continue
            amount_base = await self.rates.convert(amount, currency, cur)
            tid = self.db.add_transaction(uid, type_, amount, currency, round(amount_base, 2), category, desc,
                                          tx_date, source, goal=goal)
            saved_ids.append(tid)
            date_note = "" if tx_date == today.isoformat() else f" ({datetime.fromisoformat(tx_date).strftime('%d.%m')})"
            lines.append(tx_line(Transaction(tid, uid, type_, amount, currency, round(amount_base, 2), category,
                                             desc, goal, tx_date, source), cur) + date_note)

        parts = [prefix] if prefix else []
        if lines:
            if len(lines) > 25:
                shown = "\n".join(lines[:25]) + f"\n… и ещё {len(lines) - 25}"
            else:
                shown = "\n".join(lines)
            parts.append(f"✅ <b>Записал ({len(lines)}):</b>\n" + shown)
            if skipped:
                parts.append(f"↩️ Пропустил {skipped} — уже были записаны.")
            warnings = self.budget_warnings(uid, cur, today)
            if warnings:
                parts.append("\n".join(warnings))
        elif skipped:
            parts.append(f"↩️ Все {skipped} операции уже были записаны раньше.")
        # Если к документу был вопрос — отвечаем вторым запросом по свежей базе (точные суммы по месяцам)
        if lines and (pdf or image) and text and len(text.strip()) > 3:
            try:
                fresh_ctx = reports.build_context(self.db, uid, cur, tz)
                answer = await self.claude.analyze(
                    fresh_ctx, f"Я только что загрузил документ «{pdf_name if pdf else 'скриншот'}», операции из него уже "
                               f"записаны в базу (см. контекст). Мой вопрос: {text}", instructions=instructions)
                if answer:
                    result["reply"] = answer
            except ClaudeError as e:
                log.warning("follow-up analyze: %s", e)
        if result["reply"]:
            parts.append(("💡 " if lines else "") + to_telegram_html(result["reply"]))
        # callback_data ограничен 64 байтами — передаём диапазон id (записи одного сообщения идут подряд)
        markup = inline_keyboard([[("↩️ Отменить", f"undo:{min(saved_ids)}-{max(saved_ids)}")]]) if saved_ids else None
        await self.tg.send_message(chat_id, "\n\n".join(p for p in parts if p).strip() or "🤔", reply_markup=markup)

        # короткая память диалога
        user_summary = text or ("[PDF]" if pdf else "[скриншот]")
        if lines:
            user_summary += "\n[записано: " + "; ".join(re.sub(r"<[^>]+>", "", l) for l in lines) + "]"
        hist.append(history_entry("user", user_summary))
        hist.append(history_entry("assistant", result["reply"] or "Записал."))
        del hist[:-10]

    def budget_warnings(self, uid: int, cur: str, today: date) -> list[str]:
        budgets = self.db.budgets(uid)
        if not budgets:
            return []
        m_start, m_end = month_range(today)
        by_cat = self.db.sum_by_category(uid, m_start, m_end, "expense")
        out = []
        for cat, limit in budgets.items():
            spent = by_cat.get(cat, 0.0)
            if not limit:
                continue
            pct = spent / limit * 100
            if pct >= 100:
                out.append(f"🔴 Бюджет «{esc(cat)}» превышен: {fmt(spent, cur)} из {fmt(limit, cur)} ({pct:.0f}%)")
            elif pct >= 80:
                out.append(f"🟡 Бюджет «{esc(cat)}»: {pct:.0f}% — осталось {fmt(limit - spent, cur)} до конца месяца")
        return out

    # ---------- кнопки ----------
    async def handle_callback(self, cq: dict) -> None:
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        uid = cq.get("from", {}).get("id")
        if data.startswith("undo:") and chat_id and uid:
            spec = data[5:]
            if "-" in spec:
                a, b = spec.split("-", 1)
                ids = list(range(int(a), int(b) + 1)) if a.isdigit() and b.isdigit() else []
            else:
                ids = [int(x) for x in spec.split(",") if x.isdigit()]
            n = self.db.delete_transactions(uid, ids)
            await self.tg.answer_callback(cq["id"], f"Удалено записей: {n}")
            old = msg.get("text") or ""
            await self.tg.edit_message(chat_id, msg["message_id"], esc(old) + f"\n\n🗑 <b>Отменено</b> (удалено записей: {n})")
        else:
            await self.tg.answer_callback(cq["id"])

    # ---------- напоминания ----------
    async def scheduler_loop(self) -> None:
        while True:
            try:
                await self.scheduler_tick()
            except Exception:
                log.exception("scheduler")
            await asyncio.sleep(30)

    async def scheduler_tick(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(ZoneInfo(self.s.timezone))
        hhmm = now.strftime("%H:%M")
        day = now.date().isoformat()
        for u in self.db.all_users():
            uid, cur = u["user_id"], u["currency"]
            if u["remind_time"] == hhmm and self.db.mark_notified(uid, "daily", day):
                spent = self.db.total(uid, now.date(), now.date(), "expense")
                txt = (f"⏰ Не забудьте записать сегодняшние траты!\nПока за сегодня: {fmt(spent, cur)}."
                       if spent else "⏰ Не забудьте записать сегодняшние траты! Пока за сегодня ничего нет.")
                await self._notify(uid, txt)
            if (u["weekly_report"] and now.weekday() == self.s.weekly_report_day and hhmm == self.s.weekly_report_time
                    and self.db.mark_notified(uid, "weekly", day)):
                last_week_end = now.date() - timedelta(days=now.weekday() + 1)
                start, end = week_range(last_week_end)
                await self._notify(uid, "📊 <b>Итоги прошлой недели</b>")
                await self.send_period(uid, uid, cur, start, end, "Прошлая неделя", chart=True)
                try:
                    ctx = reports.build_context(self.db, uid, cur, self.s.timezone)
                    advice = await self.claude.analyze(
                        ctx, f"Прошла неделя {start.isoformat()}–{end.isoformat()}. Дай 3 коротких вывода и 1 совет на "
                             f"следующую неделю, как сэкономить.", max_tokens=600)
                    await self._notify(uid, "💡 " + to_telegram_html(advice))
                except ClaudeError as e:
                    log.warning("weekly advice: %s", e)

    async def _notify(self, uid: int, text: str) -> None:
        try:
            await self.tg.send_message(uid, text)
        except Exception as e:
            log.warning("notify %s: %s", uid, e)
