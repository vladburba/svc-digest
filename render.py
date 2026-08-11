#!/usr/bin/env python3
"""Оформление дайджеста для Telegram (parse_mode=HTML).

Telegram понимает узкий набор тегов: b, i, u, s, a, code, pre, blockquote.
Всё остальное экранируем, иначе прилетит TelegramBadRequest.

Отправитель уже получил ГОТОВЫЕ записи из базы — здесь только вёрстка,
никакой логики отбора.
"""

import html
from datetime import datetime

from clock import now_local, to_local

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")

MAX_LEN = 4096   # жёсткий лимит одного сообщения Telegram
MAX_ITEMS = 5    # позиций в одном сообщении: компактно; больше — бьётся на сообщения
MORNING_UNTIL = 12   # до этого часа по MSK заход считаем утренним


def esc(text):
    return html.escape(text or "", quote=False)


def human_date(moment=None):
    # Дата шапки — по MSK, а не по зоне контейнера: иначе дайджест, ушедший
    # после полуночи по Москве, был бы подписан вчерашним числом.
    moment = moment or now_local()
    return f"{moment.day} {MONTHS[moment.month - 1]}"


def period_hours(since_iso):
    """Сколько часов прошло с прошлой отправки. None — если её не было.

    Нужно, чтобы футер не врал: раньше там стояло «за сутки», а период на
    самом деле считался с прошлой отправки. Пропустили одну (лежал Telegram) —
    и «сутки» молча превращались в двое.
    """
    if not since_iso or since_iso.startswith("1970"):
        return None
    delta = now_local() - to_local(datetime.fromisoformat(since_iso))
    return max(1, round(delta.total_seconds() / 3600))


def render_filter_line(filter_funnel, waiting):
    """Строка про заходы фильтра: когда отработал, чем кончилось, чего стоил.

    Главное здесь — видимость провала. Раньше неудачные заходы ИИ просто
    выпадали из статистики, и человек не мог отличить «сегодня новостей нет»
    от «отбор не состоялся». Теперь провал подписан и сказано, сколько ждёт.
    """
    if not filter_funnel:
        return ""
    runs = filter_funnel.get("runs_list") or []
    if not runs:
        return "\n<i>🤖 Фильтрация: не запускалась</i>"

    marks = []
    for run in runs:
        when = to_local(datetime.fromisoformat(run["ran_at"])).strftime("%H:%M")
        marks.append(f"{when} ✗" if run["error"] else f"{when} ✓")
    attempts = filter_funnel.get("attempts", 0)
    cost = f" · {attempts} запр. к ИИ" if attempts else ""
    line = f"\n<i>🤖 Фильтрация: {' · '.join(marks)}{cost}</i>"

    if any(run["error"] for run in runs) and waiting:
        line += f"\n<i>⏳ ИИ не ответил — {waiting} новостей ждут следующего окна</i>"
    return line


def render_funnel(funnel, filter_funnel=None, waiting=0, since=None):
    """Футер: что собрано за период и что с этим сделал ИИ.

    Из старой версии убрана строка «лента 40 → в окне 168ч 40»: она печаталась
    неизменной 383 захода подряд (habr всегда отдаёт страницу из 40 записей,
    все моложе недели), занимала половину футера и не сообщала ничего.
    """
    if not funnel:
        return ""
    hours = period_hours(since)
    span = f"За {hours}ч" if hours else "За всё время"
    new = funnel.get("new_items", 0)
    sel = (filter_funnel or {}).get("selected", 0)
    rej = (filter_funnel or {}).get("rejected", 0)

    text = f"\n\n<i>📊 {span}: новых {new} → ✅ отобрано {sel} · ❌ отсеяно {rej}</i>"
    if funnel.get("failed_runs"):
        text += f"\n<i>⚠️ Заходов сбора с ошибкой: {funnel['failed_runs']}</i>"
    return text + render_filter_line(filter_funnel, waiting)


def digest_title():
    """Шапка по времени суток: утренний заход или вечерний.

    Отправок теперь две (08:30 и 20:30), и в ленте чата они должны различаться
    с одного взгляда. Час берём местный, а не аргументом: cron и так знает,
    когда зовёт, а лишний параметр — лишний повод рассинхрона.
    """
    return "🌅 Утренний" if now_local().hour < MORNING_UNTIL else "🌆 Вечерний"


def render_digest(items, funnel=None, filter_funnel=None, waiting=0, since=None,
                  overflow=0, part=1, parts=1, start_num=1):
    """Собирает HTML-текст ОДНОГО сообщения дайджеста.

    Когда новостей много, отправитель бьёт их на несколько сообщений и зовёт
    рендер на каждый кусок:
      items      — записи этого сообщения (нарезку делает отправитель);
      part/parts — номер и всего сообщений («(2/3)» в шапке, если parts>1);
      start_num  — с какого номера нумеровать (сквозная нумерация через части);
      overflow   — остаток сверх потолка сообщений (придёт в следующий раз);
      funnel     — воронка сбора; кладём в ПОСЛЕДНЕЕ сообщение как сводку;
      filter_funnel — итог работы ИИ за период (туда же, в последнее);
      waiting    — сколько записей ждёт фильтра прямо сейчас;
      since      — момент прошлой отправки, чтобы честно подписать период.
    Пустой items → heartbeat «нового нет».
    """
    suffix = f"  <i>({part}/{parts})</i>" if parts > 1 else ""
    head = f"📰 <b>{digest_title()} дайджест · {human_date()}</b>{suffix}"
    tail = render_funnel(funnel, filter_funnel, waiting, since)

    if not items:
        return head + "\n\nНового по твоим интересам не нашлось." + tail

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
