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


def is_morning():
    """Утренний это выпуск или вечерний. Час берём местный, а не аргументом:
    cron и так знает, когда зовёт, а лишний параметр — повод рассинхрона."""
    return now_local().hour < MORNING_UNTIL


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
            # «из N выбрал M» — обе цифры из ОДНОГО захода, поэтому сходятся
            # между собой и с тем, что человек видит в списке выше.
            parts.append(f"в {when} — из {run['in_batch'] or 0} выбрал {run['selected'] or 0}")
    line = f"\n<i>🤖 Отбор: {', '.join(parts)}</i>"

    # Итог суток — только вечером: отбор в 20:00 последний за день, значит
    # цифры окончательные. Утром такая строка была бы промежуточной: впереди
    # ещё одно окно, и к вечеру она бы устарела.
    total = filter_funnel.get("attempts", 0)
    if not is_morning() and len(runs) > 1:
        seen = filter_funnel.get("in_batch", 0)
        # Сознательно без «отобрано»: отобранное за сутки могло уйти двумя
        # разными выпусками, и рядом с «отправлено» в первой строке это
        # читалось как расхождение. Здесь — только работа ИИ и её цена.
        line += (f"\n<i>📊 За сутки: рассмотрено {seen} "
                 f"{plural(seen, 'статья', 'статьи', 'статей')} · {total} "
                 f"{plural(total, 'запрос', 'запроса', 'запросов')} к ИИ</i>")
    if any(run["error"] for run in runs) and waiting:
        line += (f"\n<i>⏳ {waiting} {plural(waiting, 'новость ждёт', 'новости ждут', 'новостей ждут')} "
                 f"следующего отбора</i>")
    return line


def render_funnel(funnel, filter_funnel=None, waiting=0, total=0, overflow=0):
    """Футер самого дайджеста: только то, что читателю нужно знать СЕЙЧАС.

    Здесь остались две вещи: остался ли хвост и не сломался ли отбор. Всё
    остальное — сколько рассмотрено, чего стоило, как ведут себя модели —
    уехало в отдельное диагностическое сообщение (решение Влада 2026-09-21).

    Так закончились четыре подряд переделки футера. Каждая пыталась уместить
    работу трёхзвенного конвейера в пару строк под новостями, и каждый раз
    числа из разных очередей и разных моментов времени оказывались рядом —
    их складывали, они не сходились, и читалось это как ошибка. Верный ответ
    был не в формулировках: сообщение с новостями должно быть про новости.
    """
    lines = []
    if overflow:
        lines.append(f"⏳ Ещё {overflow} "
                     f"{plural(overflow, 'новость придёт', 'новости придут', 'новостей придут')} "
                     f"следующим выпуском")

    runs = (filter_funnel or {}).get("runs_list") or []
    if any(run["error"] for run in runs) and waiting:
        lines.append(f"⚠️ Последний отбор не удался — ИИ не ответил. "
                     f"{waiting} {plural(waiting, 'новость ждёт', 'новости ждут', 'новостей ждут')} "
                     f"следующего окна")
    if funnel and funnel.get("failed_runs"):
        n = funnel["failed_runs"]
        lines.append(f"⚠️ Сбор падал {n} {plural(n, 'раз', 'раза', 'раз')}")

    if not lines:
        return ""
    return "\n\n" + "\n".join(f"<i>{line}</i>" for line in lines)


def render_diagnostics(run, pool, sent_count, models):
    """Служебное сообщение, которое уходит отдельно от дайджеста.

    Показывает обе стадии отбора так, чтобы каждую строку можно было проверить
    глазами: новых столько-то → интересных столько-то; интересные плюс очередь
    дают число кандидатов; из кандидатов взято и оставлено. Ни одно число здесь
    не относится к другому периоду, чем соседнее, — именно это ломало прежние
    футеры.

      run         — последний заход фильтра (строка filter_runs);
      pool        — (новых, в очереди) прямо сейчас;
      sent_count  — сколько ушло этим выпуском;
      models      — сводка по цепочке: model, asked, answered.
    """
    when = now_local().strftime("%d.%m в %H:%M")
    lines = [f"🔧 <b>Диагностика</b> · {when}", ""]

    if not run:
        lines.append("Отбор ещё ни разу не запускался.")
    elif run.get("error"):
        ran = to_local(datetime.fromisoformat(run["ran_at"])).strftime("%H:%M")
        lines.append(f"Отбор в {ran}: <b>не состоялся</b> — {esc(str(run['error']))}")
        lines.append(f"Потрачено запросов: {run.get('attempts') or 0}")
    else:
        ran = to_local(datetime.fromisoformat(run["ran_at"])).strftime("%H:%M")
        new_seen = run.get("new_seen") or 0
        interesting = run.get("interesting_new") or 0
        from_queue = run.get("queued_seen") or 0
        took = run.get("selected") or 0
        left = run.get("left_queued") or 0
        # Кандидаты конкурса — интересные из новых плюс вся очередь. Исходов
        # у кандидата три, а не два: модель может и выбросить статью, которую
        # в прошлый раз сочла интересной. Без этого третьего числа строка не
        # сходилась бы, и диагностика сама себе противоречила.
        rivals = interesting + from_queue
        dropped = max(0, rivals - took - left)
        tail = f"→ взял {took}, оставил ждать {left}"
        if dropped:
            tail += f", выбыло {dropped}"
        lines += [
            f"<b>Отбор в {ran}</b>",
            f"  новых статей: {new_seen} → интересных {interesting} "
            f"({new_seen - interesting} мимо)",
            f"  в очереди ждало: {from_queue}",
            f"  конкурс: {interesting} + {from_queue} = {rivals} {tail}",
        ]

    lines += ["", f"<b>Отправлено в выпуске:</b> {sent_count}"]
    fresh, queued = pool
    lines.append(f"<b>Ждут следующего отбора:</b> {fresh + queued} "
                 f"(новых {fresh}, из очереди {queued})")

    if models:
        lines += ["", "<b>Модели, за всё время</b>"]
        for i, m in enumerate(models, 1):
            asked, answered = m.get("asked", 0), m.get("answered", 0)
            if asked == 0:
                mark, verdict = "·", "ещё не спрашивали"
            elif answered == 0:
                mark, verdict = "❌", "молчит"
            elif answered >= asked * 0.8:
                mark, verdict = "✅", "отвечает"
            else:
                mark, verdict = "⚠️", "через раз"
            short = m["model"].split("/")[-1].replace(":free", "")
            vendor = m["model"].split("/")[0]
            row = (f"{mark} {i}. {esc(short)} ({esc(vendor)}) — {verdict}, "
                   f"обращений {asked} · ответила {answered}")
            # Время ответа показываем только тем, кто вообще отвечал: иначе
            # строка врала бы прочерком там, где модель молчит по-настоящему.
            if m.get("avg_ok"):
                # Быстрый ответ округлялся до «0 с» и выглядел поломкой.
                fmt = lambda s: f"{s:.0f} с" if s >= 10 else f"{s:.1f} с"
                row += (f"\n      думает {fmt(m['avg_ok'])} в среднем, "
                        f"дольше всего {fmt(m['max_ok'])}")
            lines.append(row)

    if run and not run.get("error") and run.get("model"):
        short = run["model"].split("/")[-1].replace(":free", "")
        tail = (f"<i>Этот отбор: {run.get('attempts') or 0} "
                f"{plural(run.get('attempts') or 0, 'запрос', 'запроса', 'запросов')}, "
                f"ответила {esc(short)}")
        if run.get("seconds"):
            tail += f" за {run['seconds']:.0f} с"
        lines += ["", tail + "</i>"]
    return "\n".join(lines)


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
      total      — сколько новостей уходит этой отправкой (для строки 📬).
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
