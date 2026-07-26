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


def render_funnel(funnel):
    """Строка воронки для футера: сколько собрано → отобрано → отсеяно."""
    if not funnel:
        return ""
    new = funnel.get("new_items", 0)
    sel = funnel.get("selected", 0)
    rej = funnel.get("rejected", 0)
    feed = funnel.get("last_feed")
    window = funnel.get("last_window")
    lens = f"лента {feed} → в окне 48ч {window} · " if feed is not None else ""
    return (f"\n\n<i>📊 За сутки: {lens}новых {new} "
            f"→ ✅ в дайджест {sel}, ❌ ИИ отсеял {rej}</i>")


def render_digest(items, funnel=None):
    """Собирает HTML-текст дайджеста из готовых записей.

    Пустой список — валидный вход: получается heartbeat «нового нет».
    funnel (опц.) — воронка за сутки, уходит в футер как доказательство работы.
    """
    head = f"📰 <b>Дайджест · {human_date()}</b>"
    tail = render_funnel(funnel)

    shown = items[:MAX_ITEMS]
    dropped = max(0, len(items) - MAX_ITEMS)

    if not shown:
        return head + "\n\nЗа последние сутки нового по твоим интересам не нашлось." + tail

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

    text = head + "\n" + "\n".join(blocks) + tail
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN - 20].rsplit("\n", 1)[0] + "\n<i>…обрезано</i>"
    return text


def render_failure(stage, error, stats=None):
    """Сообщение о поломке — уходит вместо дайджеста, чтобы молчания не было."""
    lines = ["⚠️ <b>Дайджест не отправлен</b>",
             f"Этап: <code>{esc(stage)}</code>",
             f"Причина: <code>{esc(str(error))[:300]}</code>"]
    return "\n".join(lines)
