"""Хранилище на SQLite (стандартная библиотека, без внешних зависимостей)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

EXPENSE_CATEGORIES = [
    "Еда", "Кафе и рестораны", "Транспорт", "Жильё", "Коммунальные", "Связь и интернет",
    "Здоровье", "Одежда", "Развлечения", "Подписки", "Образование", "Путешествия",
    "Подарки", "Красота", "Дети", "Питомцы", "Техника", "Другое",
]
INCOME_CATEGORIES = ["Зарплата", "Фриланс", "Подарок", "Инвестиции", "Возврат", "Другое"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    name          TEXT,
    currency      TEXT NOT NULL DEFAULT 'USD',
    remind_time   TEXT,               -- "HH:MM" или NULL (выключено)
    weekly_report INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    type         TEXT NOT NULL,       -- expense | income | saving
    amount       REAL NOT NULL,       -- в исходной валюте
    currency     TEXT NOT NULL,
    amount_base  REAL NOT NULL,       -- в базовой валюте пользователя
    category     TEXT NOT NULL,
    description  TEXT,
    goal         TEXT,
    tx_date      TEXT NOT NULL,       -- YYYY-MM-DD
    source       TEXT NOT NULL DEFAULT 'text',   -- text | voice | photo | manual
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tx_user_date ON transactions(user_id, tx_date);
CREATE TABLE IF NOT EXISTS budgets (
    user_id   INTEGER NOT NULL,
    category  TEXT NOT NULL,
    amount    REAL NOT NULL,          -- лимит в месяц, базовая валюта
    PRIMARY KEY (user_id, category)
);
CREATE TABLE IF NOT EXISTS goals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    name      TEXT NOT NULL,
    target    REAL NOT NULL,
    deadline  TEXT,                    -- YYYY-MM-DD или NULL
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sent_notifications (
    user_id INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    day     TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, day)
);
"""


@dataclass
class Transaction:
    id: int
    user_id: int
    type: str
    amount: float
    currency: str
    amount_base: float
    category: str
    description: str
    goal: Optional[str]
    tx_date: str
    source: str


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Добавление колонок в существующие базы."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)").fetchall()}
        if "instructions" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN instructions TEXT NOT NULL DEFAULT ''")

    # ---------- пользователи ----------
    def ensure_user(self, user_id: int, name: str, default_currency: str) -> sqlite3.Row:
        self.conn.execute(
            "INSERT OR IGNORE INTO users(user_id, name, currency) VALUES (?, ?, ?)",
            (user_id, name, default_currency),
        )
        self.conn.commit()
        return self.get_user(user_id)

    def get_user(self, user_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def all_users(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM users").fetchall()

    def set_currency(self, user_id: int, currency: str) -> None:
        self.conn.execute("UPDATE users SET currency=? WHERE user_id=?", (currency, user_id))
        self.conn.commit()

    def set_remind_time(self, user_id: int, value: Optional[str]) -> None:
        self.conn.execute("UPDATE users SET remind_time=? WHERE user_id=?", (value, user_id))
        self.conn.commit()

    def set_instructions(self, user_id: int, text: str) -> None:
        self.conn.execute("UPDATE users SET instructions=? WHERE user_id=?", (text, user_id))
        self.conn.commit()

    def monthly_by_category(self, user_id: int, months: int = 6, type: str = "expense") -> dict[str, dict[str, float]]:
        """{'2026-07': {'Еда': 120.0, ...}, ...} за последние N месяцев, по данным базы."""
        rows = self.conn.execute(
            "SELECT substr(tx_date,1,7) AS ym, category, SUM(amount_base) AS s FROM transactions "
            "WHERE user_id=? AND type=? GROUP BY ym, category ORDER BY ym, s DESC", (user_id, type)
        ).fetchall()
        out: dict[str, dict[str, float]] = {}
        for r in rows:
            out.setdefault(r["ym"], {})[r["category"]] = float(r["s"])
        keys = sorted(out)[-months:]
        return {k: out[k] for k in keys}

    def set_weekly_report(self, user_id: int, enabled: bool) -> None:
        self.conn.execute("UPDATE users SET weekly_report=? WHERE user_id=?", (int(enabled), user_id))
        self.conn.commit()

    # ---------- транзакции ----------
    def add_transaction(self, user_id: int, type: str, amount: float, currency: str, amount_base: float,
                        category: str, description: str, tx_date: str, source: str = "text",
                        goal: Optional[str] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO transactions(user_id, type, amount, currency, amount_base, category, description, goal, tx_date, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, type, amount, currency, amount_base, category, description, goal, tx_date, source),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def is_duplicate(self, user_id: int, amount: float, currency: str, tx_date: str, description: str) -> bool:
        row = self.conn.execute(
            "SELECT description FROM transactions WHERE user_id=? AND tx_date=? AND currency=? AND abs(amount-?)<0.005",
            (user_id, tx_date, currency, amount),
        ).fetchall()
        key = (description or "").casefold().strip()
        return any((r["description"] or "").casefold().strip() == key for r in row)

    def delete_transactions(self, user_id: int, ids: Iterable[int]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        cur = self.conn.execute(f"DELETE FROM transactions WHERE user_id=? AND id IN ({q})", (user_id, *ids))
        self.conn.commit()
        return cur.rowcount

    def delete_last(self, user_id: int) -> Optional[Transaction]:
        row = self.conn.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        if not row:
            return None
        self.conn.execute("DELETE FROM transactions WHERE id=?", (row["id"],))
        self.conn.commit()
        return self._tx(row)

    def transactions_between(self, user_id: int, start: date, end: date, type: Optional[str] = None) -> list[Transaction]:
        sql = "SELECT * FROM transactions WHERE user_id=? AND tx_date BETWEEN ? AND ?"
        params: list = [user_id, start.isoformat(), end.isoformat()]
        if type:
            sql += " AND type=?"
            params.append(type)
        sql += " ORDER BY tx_date, id"
        return [self._tx(r) for r in self.conn.execute(sql, params).fetchall()]

    def recent_transactions(self, user_id: int, limit: int = 10) -> list[Transaction]:
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY tx_date DESC, id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        return [self._tx(r) for r in rows]

    def all_transactions(self, user_id: int) -> list[Transaction]:
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY tx_date, id", (user_id,)
        ).fetchall()
        return [self._tx(r) for r in rows]

    def sum_by_category(self, user_id: int, start: date, end: date, type: str = "expense") -> dict[str, float]:
        rows = self.conn.execute(
            "SELECT category, SUM(amount_base) AS s FROM transactions WHERE user_id=? AND type=? AND tx_date BETWEEN ? AND ?"
            " GROUP BY category ORDER BY s DESC",
            (user_id, type, start.isoformat(), end.isoformat()),
        ).fetchall()
        return {r["category"]: float(r["s"]) for r in rows}

    def total(self, user_id: int, start: date, end: date, type: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_base),0) AS s FROM transactions WHERE user_id=? AND type=? AND tx_date BETWEEN ? AND ?",
            (user_id, type, start.isoformat(), end.isoformat()),
        ).fetchone()
        return float(row["s"])

    def saved_for_goal(self, user_id: int, goal_name: str) -> float:
        # lower() в SQLite не работает с кириллицей — сравниваем в Python
        rows = self.conn.execute(
            "SELECT goal, amount_base FROM transactions WHERE user_id=? AND type='saving'", (user_id,)
        ).fetchall()
        key = goal_name.casefold().strip()
        return float(sum(r["amount_base"] for r in rows if (r["goal"] or "").casefold().strip() == key))

    # ---------- бюджеты ----------
    def set_budget(self, user_id: int, category: str, amount: float) -> None:
        self.conn.execute(
            "INSERT INTO budgets(user_id, category, amount) VALUES (?,?,?) ON CONFLICT(user_id, category) DO UPDATE SET amount=excluded.amount",
            (user_id, category, amount),
        )
        self.conn.commit()

    def delete_budget(self, user_id: int, category: str) -> bool:
        cur = self.conn.execute("DELETE FROM budgets WHERE user_id=? AND category=?", (user_id, category))
        self.conn.commit()
        return cur.rowcount > 0

    def budgets(self, user_id: int) -> dict[str, float]:
        rows = self.conn.execute("SELECT category, amount FROM budgets WHERE user_id=?", (user_id,)).fetchall()
        return {r["category"]: float(r["amount"]) for r in rows}

    # ---------- цели ----------
    def add_goal(self, user_id: int, name: str, target: float, deadline: Optional[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO goals(user_id, name, target, deadline) VALUES (?,?,?,?)", (user_id, name, target, deadline)
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def goals(self, user_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM goals WHERE user_id=? ORDER BY id", (user_id,)).fetchall()

    def find_goal(self, user_id: int, name: str) -> Optional[sqlite3.Row]:
        key = name.casefold().strip()
        for g in self.goals(user_id):
            if g["name"].casefold().strip() == key:
                return g
        return None

    def delete_goal(self, user_id: int, name: str) -> bool:
        g = self.find_goal(user_id, name)
        if not g:
            return False
        self.conn.execute("DELETE FROM goals WHERE id=?", (g["id"],))
        self.conn.commit()
        return True

    # ---------- уведомления ----------
    def mark_notified(self, user_id: int, kind: str, day: str) -> bool:
        """True, если уведомление ещё не отправлялось сегодня (и теперь помечено)."""
        try:
            self.conn.execute("INSERT INTO sent_notifications(user_id, kind, day) VALUES (?,?,?)", (user_id, kind, day))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    # ---------- служебное ----------
    @staticmethod
    def _tx(r: sqlite3.Row) -> Transaction:
        return Transaction(
            id=r["id"], user_id=r["user_id"], type=r["type"], amount=float(r["amount"]), currency=r["currency"],
            amount_base=float(r["amount_base"]), category=r["category"], description=r["description"] or "",
            goal=r["goal"], tx_date=r["tx_date"], source=r["source"],
        )
