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


def plural(n, one, few, many):
    """Русское согласование: 1 новость, 2 новости, 5 новостей.

    Без этого футер читается как машинный отчёт («1 новостей»), а он должен
    читаться как фраза — иначе глаз спотыкается и цифры не усваиваются.
    """
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def render_filter_line(filter_funnel, waiting):
    """Строка про заходы фильтра: во сколько работал и сколько запросов стоил.

    Главное здесь — видимость провала. Раньше неудачные заходы ИИ просто
    выпадали из статистики, и человек не мог отличить «сегодня новостей нет»
    от «отбор не состоялся». Теперь у каждого захода своё время и свой итог.
    """
    if not filter_funnel:
        return ""
    runs = filter_funnel.get("runs_list") or []
    if not runs:
        return "\n<i>🤖 Отбор ИИ ещё не запускался</i>"

    parts = []
    for run in runs:
        when = to_local(datetime.fromisoformat(run["ran_at"])).strftime("%H:%M")
        if run["error"]:
            parts.append(f"в {when} — ИИ не ответил")
        else:
            n = run["attempts"] or 0
            parts.append(f"в {when} — {n} {plural(n, 'запрос', 'запроса', 'запросов')}")
    line = f"\n<i>🤖 Отбор ИИ: {', '.join(parts)}</i>"

    # Итоговую сумму показываем только вечером: отбор в 20:00 последний за
    # сутки, значит это окончательный расход дня. Утром такая строка была бы
    # промежуточной и только сбивала бы — впереди ещё одно окно.
    total = filter_funnel.get("attempts", 0)
    if not is_morning() and total:
        line += (f"\n<i>       за сутки {total} "
                 f"{plural(total, 'запрос', 'запроса', 'запросов')} к ИИ</i>")
    if any(run["error"] for run in runs) and waiting:
        line += (f"\n<i>⏳ {waiting} {plural(waiting, 'новость ждёт', 'новости ждут', 'новостей ждут')} "
                 f"следующего отбора</i>")
    return line


def render_funnel(funnel, filter_funnel=None, waiting=0, total=0, overflow=0):
    """Футер сообщения — три строки, каждая отвечает на свой вопрос.

    Порядок идёт от близкого к далёкому:
      📬 что в этой пачке — про сообщение, которое человек читает прямо сейчас;
      🌙/📊 что случилось с прошлого выпуска — работа сборщика и ИИ;
      🤖 как отработал отбор — во сколько и какой ценой.

    Средняя строка РАЗНАЯ утром и вечером, и это не косметика. Отбор в 20:00 —
    последний за сутки, после него цифры дня уже не изменятся: вечерний выпуск
    подводит ИТОГ («📊 Итог за сутки»), утренний отчитывается за ночь («🌙 За
    ночь»). Раньше обе подписывались одинаково — «за N часов», — и число
    прыгало между 12 и 24 в зависимости от того, был ли вечерний выпуск;
    человек читал его как ошибку, потому что считать в уме было нечего.

    Первая строка появилась тогда же: без неё футер отчитывался за период,
    список показывал очередь, и «отобрано 16» рядом с десятью новостями
    выглядело противоречием. Теперь у машины свой счёт, у цеха свой.

    Из старой версии убрана строка «лента 40 → в окне 168ч 40»: она печаталась
    неизменной 383 захода подряд (habr всегда отдаёт страницу из 40 записей,
    все моложе недели), занимала половину футера и не сообщала ничего.
    """
    if not funnel:
        return ""
    lines = []

    if total:
        head = f"📬 В этом дайджесте {total} {plural(total, 'новость', 'новости', 'новостей')}"
        if overflow:
            head += (f", ещё {overflow} {plural(overflow, 'ждёт', 'ждут', 'ждут')} "
                     f"следующего")
        lines.append(head)

    new = funnel.get("new_items", 0)
    sel = (filter_funnel or {}).get("selected", 0)
    rej = (filter_funnel or {}).get("rejected", 0)
    if is_morning():
        # Утро — промежуточный отчёт: что накопилось с вечернего выпуска.
        lines.append(f"🌙 За ночь собрано {new} "
                     f"{plural(new, 'новость', 'новости', 'новостей')}: "
                     f"отобрано {sel}, отсеяно {rej}")
    else:
        # Вечер — окончательный: отбор в 20:00 последний за сутки, эти цифры
        # уже не изменятся до полуночи. Потому и «итог», а не «за период».
        lines.append(f"📊 Итог за сутки: собрано {new}, "
                     f"отобрано {sel}, отсеяно {rej}")

    if funnel.get("failed_runs"):
        n = funnel["failed_runs"]
        lines.append(f"⚠️ Сбор падал {n} {plural(n, 'раз', 'раза', 'раз')}")

    text = "\n\n" + "\n".join(f"<i>{line}</i>" for line in lines)
    return text + render_filter_line(filter_funnel, waiting)


def is_morning():
    """Утренний это выпуск или вечерний. Час берём местный, а не аргументом:
    cron и так знает, когда зовёт, а лишний параметр — повод рассинхрона."""
    return now_local().hour < MORNING_UNTIL


def digest_title():
    """Шапка по времени суток. Отправок две (08:30 и 20:30), и в ленте чата
    они должны различаться с одного взгляда."""
    return "🌅 Утренний" if is_morning() else "🌆 Вечерний"


def render_digest(items, funnel=None, filter_funnel=None, waiting=0,
                  overflow=0, part=1, parts=1, start_num=1, total=0):
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
    tail = render_funnel(funnel, filter_funnel, waiting, total, overflow)

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
    # Про остаток очереди больше не пишем здесь: он ушёл в первую строку футера
    # («в этом дайджесте 10, ещё 6 ждут следующего») — одно место вместо двух.
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
