"""Минимальный клиент Telegram Bot API на httpx (long polling)."""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 4000


def esc(text: Any) -> str:
    """Экранирование для parse_mode=HTML."""
    return html.escape(str(text), quote=False)


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}"
        self.file_api = f"https://api.telegram.org/file/bot{token}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), transport=transport)

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, **params: Any) -> Any:
        data = {k: v for k, v in params.items() if v is not None}
        r = await self.client.post(f"{self.api}/{method}", json=data)
        payload = r.json()
        if not payload.get("ok"):
            raise TelegramError(f"{method}: {payload.get('description')} (код {payload.get('error_code')})")
        return payload["result"]

    # ---------- получение обновлений ----------
    async def get_updates(self, offset: Optional[int], timeout: int = 30) -> list[dict]:
        try:
            return await self.call(
                "getUpdates", offset=offset, timeout=timeout,
                allowed_updates=["message", "callback_query"],
            )
        except (httpx.HTTPError, ValueError) as e:
            log.warning("getUpdates: %s", e)
            await asyncio.sleep(3)
            return []

    # ---------- отправка ----------
    async def send_message(self, chat_id: int, text: str, reply_markup: Optional[dict] = None,
                           parse_mode: str = "HTML") -> Optional[dict]:
        result = None
        chunks = _split(text)
        for i, chunk in enumerate(chunks):
            markup = reply_markup if i == len(chunks) - 1 else None
            try:
                result = await self.call("sendMessage", chat_id=chat_id, text=chunk, parse_mode=parse_mode,
                                         reply_markup=markup, disable_web_page_preview=True)
            except TelegramError as e:
                # Если HTML не распарсился — отправляем как обычный текст
                if "parse" in str(e).lower() and parse_mode:
                    result = await self.call("sendMessage", chat_id=chat_id, text=chunk, reply_markup=markup)
                else:
                    raise
        return result

    async def send_photo(self, chat_id: int, image_bytes: bytes, caption: str = "", filename: str = "chart.png") -> dict:
        r = await self.client.post(
            f"{self.api}/sendPhoto",
            data={"chat_id": str(chat_id), "caption": caption[:1000], "parse_mode": "HTML"},
            files={"photo": (filename, image_bytes, "image/png")},
        )
        payload = r.json()
        if not payload.get("ok"):
            raise TelegramError(f"sendPhoto: {payload.get('description')}")
        return payload["result"]

    async def send_document(self, chat_id: int, content: bytes, filename: str, caption: str = "") -> dict:
        r = await self.client.post(
            f"{self.api}/sendDocument",
            data={"chat_id": str(chat_id), "caption": caption[:1000]},
            files={"document": (filename, content, "application/octet-stream")},
        )
        payload = r.json()
        if not payload.get("ok"):
            raise TelegramError(f"sendDocument: {payload.get('description')}")
        return payload["result"]

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            await self.call("sendChatAction", chat_id=chat_id, action=action)
        except Exception:
            pass

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text or None)
        except Exception as e:
            log.debug("answerCallbackQuery: %s", e)

    async def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup: Optional[dict] = None) -> None:
        try:
            await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:MAX_MESSAGE_LEN],
                            parse_mode="HTML", reply_markup=reply_markup)
        except TelegramError as e:
            log.debug("editMessageText: %s", e)

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        await self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])

    # ---------- файлы ----------
    async def download_file(self, file_id: str) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        path = info["file_path"]
        r = await self.client.get(f"{self.file_api}/{path}")
        r.raise_for_status()
        return r.content


def _split(text: str) -> list[str]:
    if len(text) <= MAX_MESSAGE_LEN:
        return [text]
    parts: list[str] = []
    while text:
        if len(text) <= MAX_MESSAGE_LEN:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, MAX_MESSAGE_LEN)
        if cut < MAX_MESSAGE_LEN // 2:
            cut = MAX_MESSAGE_LEN
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """[[("Текст", "callback_data"), ...], ...] -> reply_markup."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}
