#!/usr/bin/env python3
"""Оформление дайджеста для Telegram (parse_mode=HTML).

Telegram понимает узкий набор тегов: b, i, u, s, a, code, pre, blockquote.
Всё остальное экранируем, иначе прилетит TelegramBadRequest.

Отправитель уже получил ГОТОВЫЕ записи из базы — здесь только вёрстка,
никакой логики отбора.
"""

import html
from datetime import datetime

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")

MAX_LEN = 4096   # жёсткий лимит одного сообщения Telegram
MAX_ITEMS = 8    # потолок позиций: дайджест читается, а не пролистывается


def esc(text):
    return html.escape(text or "", quote=False)


def human_date(moment=None):
    moment = moment or datetime.now().astimezone()
    return f"{moment.day} {MONTHS[moment.month - 1]}"


def render_digest(items):
    """Собирает HTML-текст дайджеста из готовых записей.

    Пустой список — валидный вход: получается heartbeat «нового нет».
    """
    head = f"📰 <b>Дайджест · {human_date()}</b>"

    shown = items[:MAX_ITEMS]
    dropped = max(0, len(items) - MAX_ITEMS)

    if not shown:
        return head + "\n\nЗа последние сутки нового по твоим интересам не нашлось."

    blocks = []
    for i, item in enumerate(shown, 1):
        # key = guid (чистый); link тащит UTM-хвосты ленты
        key = str(item.get("key") or "")
        url = key if key.startswith("http") else item.get("link", "")
        blocks.append(
            f"\n<b>{i}. {esc(item['title'])}</b>\n"
            f"{esc(item.get('ai_summary', ''))}\n"
            f"<a href=\"{esc(url)}\">читать</a>"
        )
    if dropped:
        blocks.append(f"\n<i>…и ещё {dropped} — покажу в следующий раз</i>")

    footer = f"\n\n<i>{len(shown)} материалов за последние сутки</i>"

    text = head + "\n" + "\n".join(blocks) + footer
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN - 20].rsplit("\n", 1)[0] + "\n<i>…обрезано</i>"
    return text


def render_failure(stage, error, stats=None):
    """Сообщение о поломке — уходит вместо дайджеста, чтобы молчания не было."""
    lines = ["⚠️ <b>Дайджест не отправлен</b>",
             f"Этап: <code>{esc(stage)}</code>",
             f"Причина: <code>{esc(str(error))[:300]}</code>"]
    return "\n".join(lines)
