"""Курсы валют: бесплатный API frankfurter.app (ЕЦБ) с кэшем и запасной таблицей."""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# Запасные курсы к USD на случай недоступности API (примерные, обновляйте при необходимости)
FALLBACK_TO_USD = {
    "USD": 1.0, "EUR": 1.08, "UAH": 0.024, "PLN": 0.25, "GBP": 1.27, "CHF": 1.12,
    "CZK": 0.043, "RUB": 0.011, "KZT": 0.0021, "TRY": 0.03, "GEL": 0.37, "CAD": 0.73,
}

CURRENCY_ALIASES = {
    "$": "USD", "USD": "USD", "ДОЛЛ": "USD", "ДОЛЛАР": "USD", "ДОЛЛАРОВ": "USD", "БАКС": "USD",
    "€": "EUR", "EUR": "EUR", "ЕВРО": "EUR",
    "₴": "UAH", "UAH": "UAH", "ГРН": "UAH", "ГРИВЕН": "UAH", "ГРИВНА": "UAH", "ГРИВНЫ": "UAH",
    "ZŁ": "PLN", "PLN": "PLN", "ЗЛОТ": "PLN", "ЗЛОТЫХ": "PLN",
    "£": "GBP", "GBP": "GBP", "ФУНТ": "GBP",
    "₽": "RUB", "RUB": "RUB", "РУБ": "RUB",
}


def normalize_currency(code: Optional[str], default: str) -> str:
    if not code:
        return default
    key = code.strip().upper().rstrip(".")
    if key in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[key]
    if len(key) == 3 and key.isalpha():
        return key
    return default


class Rates:
    def __init__(self, ttl_seconds: int = 12 * 3600, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, dict[str, float]]] = {}  # base -> (timestamp, rates)
        self._client = httpx.AsyncClient(timeout=10.0, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def _rates_for(self, base: str) -> dict[str, float]:
        now = time.time()
        cached = self._cache.get(base)
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        try:
            r = await self._client.get("https://api.frankfurter.app/latest", params={"from": base})
            r.raise_for_status()
            rates = {k: float(v) for k, v in r.json()["rates"].items()}
            rates[base] = 1.0
            self._cache[base] = (now, rates)
            return rates
        except Exception as e:
            log.warning("Курсы валют недоступны (%s), использую запасную таблицу", e)
            if cached:
                return cached[1]
            base_usd = FALLBACK_TO_USD.get(base, 1.0)
            return {k: base_usd / v for k, v in FALLBACK_TO_USD.items()}

    async def convert(self, amount: float, from_cur: str, to_cur: str) -> float:
        """Перевести amount из from_cur в to_cur."""
        from_cur, to_cur = from_cur.upper(), to_cur.upper()
        if from_cur == to_cur:
            return amount
        rates = await self._rates_for(to_cur)  # сколько единиц X за 1 to_cur
        rate = rates.get(from_cur)
        if not rate:
            # Попробуем через USD
            usd_from = FALLBACK_TO_USD.get(from_cur)
            usd_to = FALLBACK_TO_USD.get(to_cur)
            if usd_from and usd_to:
                return amount * usd_from / usd_to
            log.warning("Неизвестная валюта %s — считаю 1:1", from_cur)
            return amount
        return amount / rate
