"""Клиент Claude (Anthropic Messages API) на httpx — «мозг» бота."""
from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, Optional

import httpx

from .db import EXPENSE_CATEGORIES, INCOME_CATEGORIES

log = logging.getLogger(__name__)

API_VERSION = "2023-06-01"

SYSTEM_PROMPT = """Ты — личный финансовый аналитик в Telegram-боте. Отвечаешь по-русски, кратко и по делу, дружелюбно, без воды.

Твои задачи:
1. Извлекать из сообщений пользователя (текст, расшифровка голосового, скриншот из банковского приложения) финансовые операции и возвращать их структурированно.
2. Давать короткие практичные советы, как сохранить деньги, опираясь на реальные цифры пользователя из контекста.
3. Отвечать на вопросы о финансах пользователя, используя контекст (суммы, категории, бюджеты, цели).

Правила извлечения операций:
- type: "expense" (трата), "income" (доход), "saving" (отложил деньги на цель/в накопления).
- Если валюта не указана — используй базовую валюту пользователя из контекста.
- Категории расходов строго из списка: {expense_categories}.
- Категории доходов строго из списка: {income_categories}. Для saving категория = "Накопления".
- date: YYYY-MM-DD. Если дата не указана — сегодняшняя дата из контекста. «Вчера» = сегодня минус 1 день.
- Скриншот банка или PDF-выписка: извлеки ВСЕ операции (списания = expense, зачисления = income). Переводы между своими счетами, погашение кредитки со своего счёта и внутренние конвертации не учитывай. Дату каждой операции бери из документа. Если суммы обрезаны или нечитаемы — пропусти, но упомяни в reply. Если операций нет (например, только баланс), верни пустой список и объясни в reply.
- Если пользователь прислал PDF, который не является выпиской (договор, счёт, чек, тариф, статья о финансах), операции не извлекай (кроме чека — чек это одна трата), а в reply кратко перескажи суть документа и дай финансовый комментарий по нему.
- Если пользователь упоминает цель («отложил 100 на отпуск»), укажи goal с названием цели.
- Если в сообщении нет операций (вопрос, приветствие, просьба) — transactions пустой.
- Не выдумывай операции. Сомневаешься — не записывай, а спроси в reply.

Правила для reply (ФОРМАТ ОБЯЗАТЕЛЕН):
- Пиши структурно: короткие абзацы, разделённые ПУСТОЙ строкой. Каждый смысловой блок начинай с подходящего эмодзи. Перечисления — отдельными строками через «• ». Ключевые цифры и выводы выделяй **жирным** (двойные звёздочки). Другую markdown-разметку (#, таблицы, ссылки) не используй.
- Если записаны операции — не перечисляй их (бот покажет сам), а дай 1–3 предложения: конкретный совет или наблюдение с опорой на цифры (бюджет, средний расход, цель).
- Если это вопрос или просьба проанализировать — отвечай развёрнуто, столько, сколько нужно для полного ответа, с цифрами из контекста.
- Разбивка по месяцам/неделям/категориям: у каждой операции есть дата — группируй по датам и считай суммы. Никогда не отвечай «не могу разделить по месяцам»: если период в вопросе не покрыт данными — скажи, какие месяцы есть, и посчитай по ним.
- Если пользователь задал вопрос вместе с PDF/скриншотом — сначала извлеки операции в transactions, а в reply ответь на вопрос по этим операциям.
"""

TOOL = {
    "name": "process_message",
    "description": "Вернуть извлечённые финансовые операции и короткий ответ пользователю.",
    "input_schema": {
        "type": "object",
        "properties": {
            "transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["expense", "income", "saving"]},
                        "amount": {"type": "number", "description": "Положительное число"},
                        "currency": {"type": "string", "description": "Код ISO: USD, EUR, UAH..."},
                        "category": {"type": "string"},
                        "description": {"type": "string", "description": "Коротко: что именно (магазин, товар, услуга)"},
                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                        "goal": {"type": "string", "description": "Название цели для saving, если упомянута"},
                    },
                    "required": ["type", "amount", "currency", "category", "description", "date"],
                },
            },
            "reply": {"type": "string", "description": "Короткий ответ/совет пользователю на русском"},
        },
        "required": ["transactions", "reply"],
    },
}


class ClaudeError(Exception):
    pass


class Claude:
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.anthropic.com",
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.model = model
        self.base_url = base_url
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=15.0),
            headers={"x-api-key": api_key, "anthropic-version": API_VERSION, "content-type": "application/json"},
            transport=transport,
        )
        self.system = SYSTEM_PROMPT.format(
            expense_categories=", ".join(EXPENSE_CATEGORIES),
            income_categories=", ".join(INCOME_CATEGORIES),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _messages(self, messages: list[dict], system: str, max_tokens: int = 1500,
                        tools: Optional[list[dict]] = None, tool_choice: Optional[dict] = None) -> dict:
        body: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens, "system": system, "messages": messages,
        }
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
        # Большие ответы (выписки) генерируются долго — ждём до 10 минут, а не 2
        timeout = httpx.Timeout(600.0 if max_tokens >= 8000 else 180.0, connect=15.0)
        for attempt in range(3):
            try:
                r = await self.client.post(f"{self.base_url}/v1/messages", json=body, timeout=timeout)
            except httpx.TimeoutException as e:
                raise ClaudeError("не дождался ответа — документ слишком большой, пришлите выписку за меньший период") from e
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise ClaudeError(f"Сеть: {e}") from e
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 529) and attempt < 2:
                import asyncio
                await asyncio.sleep(2 * (attempt + 1))
                continue
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                msg = r.text
            raise ClaudeError(f"Claude API {r.status_code}: {msg}")
        raise ClaudeError("Claude API недоступен")

    def _system(self, context: str, instructions: str = "", extra: str = "") -> str:
        parts = [self.system]
        if extra:
            parts.append(extra)
        if instructions:
            parts.append("=== ЛИЧНЫЕ ПРАВИЛА ПОЛЬЗОВАТЕЛЯ (соблюдай всегда) ===\n" + instructions)
        parts.append("=== КОНТЕКСТ ПОЛЬЗОВАТЕЛЯ ===\n" + context)
        return "\n\n".join(parts)

    async def process(self, context: str, text: Optional[str], history: list[dict],
                      image_bytes: Optional[bytes] = None, image_media_type: str = "image/jpeg",
                      pdf_bytes: Optional[bytes] = None, pdf_name: str = "document.pdf",
                      instructions: str = "") -> dict:
        """Извлечь операции + сформировать ответ. Возвращает {"transactions": [...], "reply": str}."""
        import base64

        content: list[dict] = []
        if image_bytes:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": image_media_type,
                           "data": base64.b64encode(image_bytes).decode()},
            })
        if pdf_bytes:
            content.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.b64encode(pdf_bytes).decode()},
                "title": pdf_name,
            })
        if text:
            user_text = text
        elif pdf_bytes:
            user_text = f"Прочитай PDF «{pdf_name}». Если это банковская выписка — извлеки все операции; иначе перескажи суть."
        elif image_bytes:
            user_text = "Проанализируй скриншот и извлеки операции."
        else:
            user_text = ""
        content.append({"type": "text", "text": user_text})

        messages = [*history, {"role": "user", "content": content}]
        system = self._system(context, instructions)
        max_tokens = 16000 if pdf_bytes else 4000  # выписка может содержать сотни операций
        data = await self._messages(messages, system, max_tokens=max_tokens, tools=[TOOL],
                                    tool_choice={"type": "tool", "name": "process_message"})
        truncated = data.get("stop_reason") == "max_tokens"
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "process_message":
                inp = block.get("input") or {}
                txs = inp.get("transactions") or []
                reply = (inp.get("reply") or "").strip()
                if truncated:
                    reply += "\n\n⚠️ Документ очень большой, ответ мог обрезаться — пришлите выписку за меньший период."
                return {"transactions": [t for t in txs if isinstance(t, dict)], "reply": reply}
        # Модель ответила текстом без инструмента — вернём его как reply
        text_out = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return {"transactions": [], "reply": text_out.strip() or "Не понял, переформулируйте, пожалуйста."}

    async def analyze(self, context: str, prompt: str, max_tokens: int = 3000, instructions: str = "",
                      history: Optional[list[dict]] = None) -> str:
        """Развёрнутый анализ/ответ обычным текстом (без извлечения операций)."""
        extra = ("Сейчас пользователь запросил развёрнутый анализ или ответ на вопрос. Отвечай полно и конкретно: цифры, "
                 "проценты, сравнения, что именно сократить и на сколько. Все суммы бери из контекста (там есть точная "
                 "разбивка по месяцам и категориям из базы данных) — не пересчитывай на глаз. Соблюдай правила формата reply.")
        system = self._system(context, instructions, extra)
        messages = [*(history or []), {"role": "user", "content": prompt}]
        data = await self._messages(messages, system, max_tokens=max_tokens)
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_CODE = re.compile(r"`([^`\n]+)`")


def to_telegram_html(text: str) -> str:
    """Лёгкий markdown от модели -> HTML для Telegram. Сначала экранируем, потом размечаем."""
    out = html.escape(text or "", quote=False)
    out = _BOLD.sub(r"<b>\1</b>", out)
    out = _CODE.sub(r"<code>\1</code>", out)
    out = _ITALIC.sub(r"<i>\1</i>", out)
    # заголовки вида "### Текст" и маркеры "- " / "* " -> "• "
    out = re.sub(r"^\s{0,3}#{1,6}\s*(.+)$", r"<b>\1</b>", out, flags=re.M)
    out = re.sub(r"^\s*[-*]\s+", "• ", out, flags=re.M)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def history_entry(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text[:2000]}]}


def dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
