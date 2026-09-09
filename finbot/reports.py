"""Сводки, контекст для Claude и графики (Pillow)."""
from __future__ import annotations

import calendar
import io
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .db import Database, Transaction
from .telegram import esc

ASSETS = Path(__file__).parent / "assets"

TYPE_LABEL = {"expense": "Трата", "income": "Доход", "saving": "Накопление"}
TYPE_EMOJI = {"expense": "💸", "income": "💰", "saving": "🏦"}


def fmt(amount: float, currency: str) -> str:
    s = f"{amount:,.2f}".replace(",", " ")
    if s.endswith(".00"):
        s = s[:-3]
    return f"{s} {currency}"


def today_in(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def month_range(d: date) -> tuple[date, date]:
    last = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=1), d.replace(day=last)


def week_range(d: date) -> tuple[date, date]:
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def tx_line(t: Transaction, base_cur: str) -> str:
    amount = fmt(t.amount, t.currency)
    if t.currency != base_cur:
        amount += f" (≈{fmt(t.amount_base, base_cur)})"
    extra = f" · цель «{esc(t.goal)}»" if t.goal else ""
    desc = f" — {esc(t.description)}" if t.description else ""
    return f"{TYPE_EMOJI.get(t.type, '•')} {amount} · {esc(t.category)}{desc}{extra}"


# ---------- контекст для Claude ----------

def build_context(db: Database, user_id: int, base_cur: str, tz: str) -> str:
    today = today_in(tz)
    m_start, m_end = month_range(today)
    w_start, w_end = week_range(today)
    lines = [
        f"Сегодня: {today.isoformat()} ({['пн','вт','ср','чт','пт','сб','вс'][today.weekday()]}). Базовая валюта: {base_cur}.",
    ]
    exp_month = db.total(user_id, m_start, m_end, "expense")
    inc_month = db.total(user_id, m_start, m_end, "income")
    sav_month = db.total(user_id, m_start, m_end, "saving")
    exp_week = db.total(user_id, w_start, w_end, "expense")
    exp_today = db.total(user_id, today, today, "expense")
    days_passed = today.day
    days_total = m_end.day
    avg_day = exp_month / days_passed if days_passed else 0
    forecast = avg_day * days_total
    lines.append(
        f"Текущий месяц ({m_start.isoformat()}…{m_end.isoformat()}): расходы {exp_month:.2f}, доходы {inc_month:.2f}, "
        f"отложено {sav_month:.2f}. Средний расход в день {avg_day:.2f}, прогноз на месяц {forecast:.2f}. "
        f"Прошло дней: {days_passed} из {days_total}."
    )
    lines.append(f"Эта неделя: расходы {exp_week:.2f}. Сегодня: расходы {exp_today:.2f}.")

    by_cat = db.sum_by_category(user_id, m_start, m_end, "expense")
    if by_cat:
        lines.append("Расходы по категориям за месяц: " + "; ".join(f"{c} {v:.2f}" for c, v in by_cat.items()))

    # прошлый месяц для сравнения
    prev_end = m_start - timedelta(days=1)
    p_start, p_end = month_range(prev_end)
    prev_exp = db.total(user_id, p_start, p_end, "expense")
    if prev_exp:
        prev_cat = db.sum_by_category(user_id, p_start, p_end, "expense")
        lines.append(
            f"Прошлый месяц: расходы {prev_exp:.2f}; по категориям: "
            + "; ".join(f"{c} {v:.2f}" for c, v in list(prev_cat.items())[:8])
        )

    budgets = db.budgets(user_id)
    if budgets:
        parts = []
        for cat, limit in budgets.items():
            spent = by_cat.get(cat, 0.0)
            pct = spent / limit * 100 if limit else 0
            parts.append(f"{cat}: {spent:.2f} из {limit:.2f} ({pct:.0f}%)")
        lines.append("Бюджеты на месяц: " + "; ".join(parts))
    else:
        lines.append("Бюджеты не заданы (команда /budget).")

    goals = db.goals(user_id)
    if goals:
        parts = []
        for g in goals:
            saved = db.saved_for_goal(user_id, g["name"])
            dl = f", срок {g['deadline']}" if g["deadline"] else ""
            parts.append(f"«{g['name']}»: накоплено {saved:.2f} из {g['target']:.2f}{dl}")
        lines.append("Цели накопления: " + "; ".join(parts))

    recent = db.recent_transactions(user_id, 12)
    if recent:
        lines.append("Последние операции: " + "; ".join(
            f"{t.tx_date} {TYPE_LABEL.get(t.type, t.type)} {t.amount:.2f} {t.currency} {t.category}"
            + (f" ({t.description})" if t.description else "") for t in recent
        ))
    else:
        lines.append("Операций пока нет.")
    return "\n".join(lines)


# ---------- текстовые отчёты ----------

def period_report(db: Database, user_id: int, base_cur: str, start: date, end: date, title: str) -> str:
    exp = db.total(user_id, start, end, "expense")
    inc = db.total(user_id, start, end, "income")
    sav = db.total(user_id, start, end, "saving")
    by_cat = db.sum_by_category(user_id, start, end, "expense")
    days = (end - start).days + 1
    lines = [f"<b>{esc(title)}</b> ({start.strftime('%d.%m')} – {end.strftime('%d.%m.%Y')})", ""]
    lines.append(f"💸 Расходы: <b>{fmt(exp, base_cur)}</b>")
    if inc:
        lines.append(f"💰 Доходы: <b>{fmt(inc, base_cur)}</b>")
    if sav:
        lines.append(f"🏦 Отложено: <b>{fmt(sav, base_cur)}</b>")
    if inc:
        balance = inc - exp - sav
        lines.append(f"{'📈' if balance >= 0 else '📉'} Остаток: <b>{fmt(balance, base_cur)}</b>")
    if days > 1 and exp:
        lines.append(f"📅 В среднем в день: {fmt(exp / days, base_cur)}")
    if by_cat:
        lines.append("")
        lines.append("<b>По категориям:</b>")
        budgets = db.budgets(user_id)
        for cat, val in by_cat.items():
            pct = val / exp * 100 if exp else 0
            b = ""
            if cat in budgets and days >= 28:
                b_pct = val / budgets[cat] * 100 if budgets[cat] else 0
                flag = "🔴" if b_pct >= 100 else ("🟡" if b_pct >= 80 else "🟢")
                b = f" {flag} {b_pct:.0f}% бюджета"
            lines.append(f"• {esc(cat)}: {fmt(val, base_cur)} ({pct:.0f}%){b}")
    if not by_cat and not inc:
        lines.append("")
        lines.append("Пока пусто. Напишите или наговорите первую трату 🙂")
    return "\n".join(lines)


def budgets_report(db: Database, user_id: int, base_cur: str, tz: str) -> str:
    budgets = db.budgets(user_id)
    if not budgets:
        return ("Бюджеты не заданы.\nПример: <code>/budget Еда 300</code> — лимит 300 в месяц на категорию «Еда».\n"
                "Категории: " + ", ".join(EXPENSE_CATEGORIES_SHORT()))
    today = today_in(tz)
    m_start, m_end = month_range(today)
    by_cat = db.sum_by_category(user_id, m_start, m_end, "expense")
    days_left = (m_end - today).days + 1
    lines = [f"<b>Бюджеты на {today.strftime('%m.%Y')}</b> (осталось дней: {days_left})", ""]
    for cat, limit in budgets.items():
        spent = by_cat.get(cat, 0.0)
        pct = spent / limit * 100 if limit else 0
        flag = "🔴" if pct >= 100 else ("🟡" if pct >= 80 else "🟢")
        left = limit - spent
        lines.append(f"{flag} {esc(cat)}: {fmt(spent, base_cur)} / {fmt(limit, base_cur)} ({pct:.0f}%)"
                     + (f", осталось {fmt(left, base_cur)}" if left > 0 else ", лимит превышен"))
    lines.append("")
    lines.append("Изменить: <code>/budget Категория Сумма</code>, удалить: <code>/budget Категория 0</code>")
    return "\n".join(lines)


def EXPENSE_CATEGORIES_SHORT() -> list[str]:
    from .db import EXPENSE_CATEGORIES
    return EXPENSE_CATEGORIES


def goals_report(db: Database, user_id: int, base_cur: str, tz: str) -> str:
    goals = db.goals(user_id)
    if not goals:
        return ("Целей пока нет.\nПример: <code>/goal Отпуск 2000 2026-12-31</code> (дата необязательна).\n"
                "Пополнять: напишите «отложил 100 на отпуск» или <code>/save Отпуск 100</code>.")
    today = today_in(tz)
    lines = ["<b>Цели накопления</b>", ""]
    for g in goals:
        saved = db.saved_for_goal(user_id, g["name"])
        pct = min(saved / g["target"] * 100, 100) if g["target"] else 0
        bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
        line = f"🎯 <b>{esc(g['name'])}</b>: {fmt(saved, base_cur)} / {fmt(g['target'], base_cur)}\n{bar} {pct:.0f}%"
        if g["deadline"]:
            try:
                dl = date.fromisoformat(g["deadline"])
                days = (dl - today).days
                remain = max(g["target"] - saved, 0)
                if days > 0 and remain > 0:
                    months = max(days / 30.4, 0.1)
                    line += f"\nДо {dl.strftime('%d.%m.%Y')} осталось {days} дн. — нужно ≈{fmt(remain / months, base_cur)} в месяц"
                elif remain <= 0:
                    line += "\n✅ Цель достигнута!"
                else:
                    line += f"\n⏰ Срок {dl.strftime('%d.%m.%Y')} прошёл, не хватает {fmt(remain, base_cur)}"
            except ValueError:
                pass
        lines.append(line)
        lines.append("")
    lines.append("Удалить цель: <code>/goal удалить Название</code>")
    return "\n".join(lines)


def export_csv(db: Database, user_id: int) -> bytes:
    import csv
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "date", "type", "amount", "currency", "amount_base", "category", "description", "goal", "source"])
    for t in db.all_transactions(user_id):
        w.writerow([t.id, t.tx_date, t.type, f"{t.amount:.2f}", t.currency, f"{t.amount_base:.2f}", t.category,
                    t.description, t.goal or "", t.source])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM, чтобы Excel понял кириллицу


# ---------- графики ----------

PALETTE = ["#4C78A8", "#F58518", "#54A24B", "#E45756", "#72B7B2", "#EECA3B", "#B279A2",
           "#FF9DA6", "#9D755D", "#BAB0AC", "#6C8EBF", "#D67D3E"]


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(str(ASSETS / name), size)
    except OSError:
        return ImageFont.load_default()


def chart_png(by_cat: dict[str, float], daily: dict[date, float], base_cur: str, title: str,
              budgets: Optional[dict[str, float]] = None) -> bytes:
    """Картинка: слева — расходы по категориям, справа — по дням."""
    from PIL import Image, ImageDraw

    W, H = 1200, 700
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f_title, f_lbl, f_small = _font(30, True), _font(20), _font(16)
    d.text((40, 24), title, fill="#222", font=f_title)

    # --- Левая панель: горизонтальные бары по категориям ---
    cats = list(by_cat.items())[:10]
    left_x, top_y = 40, 90
    panel_w = 560
    d.text((left_x, top_y), "По категориям", fill="#444", font=f_lbl)
    top_y += 40
    if cats:
        maxv = max(v for _, v in cats) or 1
        row_h = min(48, (H - top_y - 40) // max(len(cats), 1))
        label_w = 190
        bar_max = panel_w - label_w - 120
        for i, (cat, val) in enumerate(cats):
            y = top_y + i * row_h
            name = cat if len(cat) <= 16 else cat[:15] + "…"
            d.text((left_x, y + row_h // 2 - 10), name, fill="#333", font=f_small)
            bw = int(bar_max * val / maxv)
            color = PALETTE[i % len(PALETTE)]
            d.rounded_rectangle([left_x + label_w, y + 8, left_x + label_w + max(bw, 3), y + row_h - 8],
                                radius=6, fill=color)
            if budgets and cat in budgets and budgets[cat] > 0:
                lim_x = left_x + label_w + int(bar_max * min(budgets[cat] / maxv, 1.0))
                d.line([lim_x, y + 4, lim_x, y + row_h - 4], fill="#E45756", width=2)
            d.text((left_x + label_w + max(bw, 3) + 8, y + row_h // 2 - 9), fmt(val, base_cur),
                   fill="#333", font=f_small)
    else:
        d.text((left_x, top_y), "Нет данных", fill="#999", font=f_lbl)

    # --- Правая панель: столбики по дням ---
    rx, ry = 660, 90
    rw, rh = W - rx - 40, H - ry - 80
    d.text((rx, ry), "По дням", fill="#444", font=f_lbl)
    ry += 40
    rh -= 40
    days = sorted(daily.items())
    if days:
        maxv = max(v for _, v in days) or 1
        n = len(days)
        gap = 2 if n > 20 else 6
        bw = max((rw - gap * (n - 1)) // n, 2)
        base_y = ry + rh
        # сетка
        for k in range(5):
            gy = ry + rh * k // 4
            d.line([rx, gy, rx + rw, gy], fill="#EEE", width=1)
            d.text((rx + rw - 90, gy - 18), fmt(maxv * (4 - k) / 4, base_cur).replace(f" {base_cur}", ""),
                   fill="#AAA", font=f_small)
        avg = sum(v for _, v in days) / n
        ay = base_y - int(rh * avg / maxv)
        d.line([rx, ay, rx + rw, ay], fill="#F58518", width=2)
        d.text((rx, ay - 20), f"среднее {fmt(avg, base_cur)}", fill="#F58518", font=f_small)
        for i, (day, val) in enumerate(days):
            x = rx + i * (bw + gap)
            h = int(rh * val / maxv)
            d.rectangle([x, base_y - h, x + bw, base_y], fill="#4C78A8")
            if n <= 31 and (n <= 12 or i % max(n // 10, 1) == 0):
                d.text((x, base_y + 6), day.strftime("%d"), fill="#666", font=f_small)
    else:
        d.text((rx, ry), "Нет данных", fill="#999", font=f_lbl)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def daily_totals(txs: list[Transaction], start: date, end: date) -> dict[date, float]:
    out: dict[date, float] = {}
    d = start
    while d <= end:
        out[d] = 0.0
        d += timedelta(days=1)
    for t in txs:
        if t.type == "expense":
            dd = date.fromisoformat(t.tx_date)
            out[dd] = out.get(dd, 0.0) + t.amount_base
    return out
