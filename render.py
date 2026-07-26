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
MAX_ITEMS = 5    # позиций в одном сообщении: компактно; больше — бьётся на сообщения


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


def render_digest(items, funnel=None, overflow=0, part=1, parts=1, start_num=1):
    """Собирает HTML-текст ОДНОГО сообщения дайджеста.

    Когда новостей много, отправитель бьёт их на несколько сообщений и зовёт
    рендер на каждый кусок:
      items    — записи этого сообщения (нарезку делает отправитель);
      part/parts — номер и всего сообщений («(2/3)» в шапке, если parts>1);
      start_num  — с какого номера нумеровать (сквозная нумерация через части);
      overflow — остаток сверх потолка сообщений (придёт в следующий раз);
      funnel   — воронка; кладём в ПОСЛЕДНЕЕ сообщение как сводку.
    Пустой items → heartbeat «нового нет».
    """
    suffix = f"  <i>({part}/{parts})</i>" if parts > 1 else ""
    head = f"📰 <b>Дайджест · {human_date()}</b>{suffix}"
    tail = render_funnel(funnel)

    if not items:
        return head + "\n\nЗа последние сутки нового по твоим интересам не нашлось." + tail

    blocks = []
    for i, item in enumerate(items, start_num):
        # key = guid (чистый); link тащит UTM-хвосты ленты
        key = str(item.get("key") or "")
        url = key if key.startswith("http") else item.get("link", "")
        blocks.append(
            f"\n<b>{i}. {esc(item['title'])}</b>\n"
            f"{esc(item.get('ai_summary', ''))}\n"
            f"<a href=\"{esc(url)}\">читать</a>"
        )
    if overflow > 0:
        blocks.append(f"\n<i>…ещё {overflow} в очереди — придут в следующем дайджесте</i>")

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
