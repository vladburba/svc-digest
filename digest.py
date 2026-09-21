#!/usr/bin/env python3
"""svc-digest — три развязанные работы, общаются через базу.

    python digest.py collect   СБОРЩИК     — раз в час (cron :05, весь день)
    python digest.py filter    ФИЛЬТР      — дважды в сутки (cron 08:00 и 20:00)
    python digest.py send      ОТПРАВИТЕЛЬ — раз в сутки (cron 08:30)

Развязка даёт главное: отправка в 8:30 не зависит от того, живы ли RSS и ИИ
в этот момент — дайджест уже лежит готовым в базе. Сборщик весь день
ретраится сам собой: лёг провайдер сейчас — подберёт через час.

Почему ИИ вынесен из сборщика в отдельную работу (2026-08-11). Раньше отбор
жил внутри сбора, и модель звали каждый час — по факту ради одной-двух статей,
а то и впустую. Это давало 7–16 запросов в сутки при дневной квоте аккаунта 50,
общей с vladburba-bot, и заодно било по качеству: модель судила статью в
одиночку, не имея с чем сравнить. Теперь сбор бесплатен и част, а отбор редок
и пакетен — 2 запроса в сутки, и модель выбирает лучшее из всего дня.

Три сквозных правила:
  · ЛОГ НА КАЖДОМ СТЫКЕ — видно, где схлопнулось.
  · СИГНАЛ В ЛЮБОМ ИСХОДЕ — но только у ОТПРАВИТЕЛЯ (он общается с человеком).
    Сборщик и фильтр — молчаливые рабочие: шумят в лог, человека не дёргают.
    Провал фильтра человек увидит строкой в футере утреннего дайджеста.
  · РАСХОД КВОТЫ ВИДЕН — каждый заход фильтра пишет в журнал число запросов
    к OpenRouter, включая неудачные попытки цепочки: их квота тоже списывает.
"""

import logging
import sys
import time
import traceback
from pathlib import Path

import clock
import database
import render
import telegram
from clock import LocalFormatter
from collect import WINDOW_HOURS, collect
from filter_ai import OPENROUTER_MODELS_PRIORITY, select
from render import MAX_ITEMS, render_digest, render_failure

# Потолок сообщений за один заход отправителя. Было 5 (до 25 новостей за раз) —
# и утром это прилетало простынёй в четыре сообщения, которую с утра никто не
# читает. Стало 2 (до 10 новостей): отправок теперь две в сутки, а всё сверх
# потолка остаётся pending и придёт следующей — очередь не теряется, а
# размазывается. Порог сознательно жёсткий: лучше недосказать, чем завалить.
MAX_MESSAGES = 2

# Потолок пакета для одного захода фильтра. При обычных 5-10 новостях в сутки
# не срабатывает никогда; нужен на возврат из простоя, чтобы в модель не ушла
# сотня статей одним промптом (длинный вход = выше шанс обрыва и хуже отбор).
# Остаток разберёт следующее окно, а совсем старое отсечёт expire_old.
BATCH_LIMIT = 40

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "digest.log"

# Формат времени — местный (MSK) и с явной зоной: иначе в digest.log было
# время контейнера (UTC), а в syslog хоста — MSK, и одно событие выглядело
# как два разных момента.
_formatter = LocalFormatter("%(asctime)s %(levelname)-7s %(message)s")
_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8"),
             logging.StreamHandler(sys.stdout)]
for _handler in _handlers:
    _handler.setFormatter(_formatter)

logging.basicConfig(level=logging.INFO, handlers=_handlers)
log = logging.getLogger("digest")

# КРИТИЧНО: httpx на INFO пишет полный URL, а у Telegram Bot API токен лежит
# ПРЯМО В ПУТИ (/bot<TOKEN>/sendMessage). Глушим, чтобы токен не тёк в лог.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def run_collect():
    """СБОРЩИК: складывает свежее из ленты в базу со статусом new.

    ИИ здесь не участвует — значит заход ничего не стоит и может повторяться
    хоть каждый час. Человека не трогает, только лог.

    Каждый заход пишет свою воронку в collect_runs — отправитель соберёт из
    них статистику за сутки и вложит в сообщение (сам сбора не видит).
    """
    database.init_db()
    funnel = {"total_in_feed": None, "in_window": None, "already_seen": None,
              "new_items": 0, "error": None}

    items, cs = collect()
    funnel["total_in_feed"] = cs["total"]
    funnel["in_window"] = cs["fresh"]
    log.info("сбор: HTTP %s | в ленте %s | свежих за %sч %s",
             cs["http"], cs["total"], cs["window_hours"], cs["fresh"])
    if cs.get("error"):
        funnel["error"] = cs["error"]
        database.record_run(funnel)
        log.warning("сбор не удался (%s) — базу не трогаем, подберём в следующий заход",
                    cs["error"])
        return 1

    unseen, skipped = database.filter_unseen(items)
    funnel["already_seen"] = skipped
    funnel["new_items"] = len(unseen)
    log.info("дедуп: было %s | уже в базе %s | новых %s",
             len(items), skipped, len(unseen))
    if not unseen:
        database.record_run(funnel)
        log.info("новых нет — базу не трогаем")
        return 0

    written = database.record(unseen, "new")
    funnel["new_items"] = written
    database.record_run(funnel)

    log.info("записано new: %s (ждут фильтра, всего в очереди %s)",
             written, database.count_new())
    return 0


def run_filter():
    """ФИЛЬТР: раз в окно (08:00 и 20:00) отдаёт накопленное ИИ одним пакетом.

    Единственная работа, которая тратит квоту OpenRouter — и тратит её
    предсказуемо: не больше одного пакета за заход, то есть максимум длины
    цепочки моделей (сейчас 3 попытки при полном невезении).

    В чат не пишет: молчаливый рабочий, как и сборщик. Если заход провалился,
    записи остаются в new — их подберёт следующее окно, а человек узнает о
    провале строкой в футере утреннего дайджеста.
    """
    database.init_db()
    row = {"in_batch": 0, "selected": 0, "rejected": 0, "expired": 0,
           "model": None, "attempts": 0, "error": None,
           "new_seen": 0, "queued_seen": 0, "interesting_new": 0, "left_queued": 0}

    # Сначала выносим протухшее: незачем занимать место в пакете новостью,
    # которая уже выпала из недельного окна ленты.
    row["expired"] = database.expire_old(WINDOW_HOURS)
    if row["expired"]:
        log.info("протухло: %s (ждали дольше %sч)", row["expired"], WINDOW_HOURS)

    fresh, queued = database.count_pool()
    batch = database.get_pool(BATCH_LIMIT)
    row["in_batch"] = len(batch)
    row["new_seen"] = sum(1 for item in batch if item.get("is_new"))
    row["queued_seen"] = len(batch) - row["new_seen"]
    log.info("фильтр: ждут отбора %s (новых %s, из очереди %s) | берём в пакет %s",
             fresh + queued, fresh, queued, len(batch))

    if not batch:
        database.record_filter_run(row)
        log.info("фильтровать нечего — ИИ не тревожим")
        return 0

    selected, ai = select(batch)
    row["attempts"] = ai["attempts"]
    row["model"] = ai["model"]
    database.record_model_attempts(ai["tries"])
    for failure in ai["failed"]:
        log.warning("фолбэк: %s", failure)

    if ai["error"]:
        row["error"] = ai["error"]
        database.record_filter_run(row)
        log.warning("ИИ недоступен (%s), запросов потрачено %s — статусы не трогаем, "
                    "пакет ждёт следующего окна", ai["error"], ai["attempts"])
        return 1

    pool_keys = [item["key"] for item in batch]
    row["selected"], row["rejected"], row["left_queued"] = database.apply_filter(
        selected, ai["rejected_keys"], pool_keys)

    # «Интересные из новых» — те новые, что модель НЕ назвала мимо интересов.
    # Это первая строка диагностики: сколько свежего вообще стоило внимания.
    rejected_set = set(ai["rejected_keys"])
    row["interesting_new"] = sum(1 for item in batch
                                 if item.get("is_new") and item["key"] not in rejected_set)
    database.record_filter_run(row)

    log.info("отбор: новых %s → интересных %s | из очереди %s | "
             "конкурс %s → взял %s, оставил ждать %s | модель %s, запросов %s",
             row["new_seen"], row["interesting_new"], row["queued_seen"],
             row["interesting_new"] + row["queued_seen"], row["selected"],
             row["left_queued"], ai["model"], ai["attempts"])
    st = database.stats()
    log.info("база: new %s · queued %s · pending %s · sent %s · rejected %s · expired %s",
             st["new"] or 0, st["queued"] or 0, st["pending"] or 0, st["sent"] or 0,
             st["rejected"] or 0, st["expired"] or 0)
    return 0


def run_send(quiet_if_empty=False):
    """ОТПРАВИТЕЛЬ: 08:30 и 20:30. Шлёт накопленное, сигнал в любом исходе.

    quiet_if_empty — режим вечернего захода: если слать нечего, промолчать.
    Утренний заход при пустой очереди всё равно подаёт голос: это heartbeat,
    иначе тишина неотличима от «сервис умер». Вечером такой страховки уже не
    нужно — утро её только что дало, а два «нового нет» в день превращают
    полезный сигнал в шум, который перестают замечать.
    """
    database.init_db()
    try:
        pending = database.get_pending()

        # Период футера зависит от того, какой это выпуск, и это не украшение,
        # а смысл: отбор в 20:00 — последний за сутки, после него цифры дня
        # уже не изменятся. Поэтому вечерний дайджест подводит ИТОГ ЗА СУТКИ
        # (с 00:00 MSK), а утренний отчитывается за ночь — с прошлого выпуска.
        evening = not render.is_morning()
        if evening:
            since = clock.day_start_iso()
        else:
            since = database.last_sent_at() or "1970-01-01T00:00:00+00:00"
        funnel = database.funnel_since(since)
        filter_funnel = database.filter_funnel_since(since)
        waiting = database.count_new()

        if not pending:
            if quiet_if_empty:
                log.info("отправка: pending пуст, вечерний заход — молчим")
                return 0
            # Heartbeat: молчание неотличимо от «контейнер не стартовал».
            telegram.send_message(render_digest([], funnel, filter_funnel=filter_funnel,
                                                waiting=waiting))
            log.info("отправка: pending пуст — послан сигнал «нового нет»")
            send_diagnostics(0)
            return 0

        # Много новостей → несколько сообщений по MAX_ITEMS. Потолок MAX_MESSAGES
        # за один заход (страховка от аномального навала); остаток сверх него
        # остаётся pending и придёт в следующий раз.
        chunks = [pending[i:i + MAX_ITEMS] for i in range(0, len(pending), MAX_ITEMS)]
        chunks = chunks[:MAX_MESSAGES]
        going = sum(len(c) for c in chunks)          # сколько уедет этой отправкой
        overflow = len(pending) - going              # сколько останется ждать
        parts = len(chunks)

        # Каждый кусок метится sent ПОСЛЕ своей отправки: упади 2-е сообщение —
        # 1-е уже доставлено и помечено, остальное останется pending (retry).
        sent_total = 0
        for idx, chunk in enumerate(chunks, 1):
            is_last = idx == parts
            text = render_digest(
                chunk,
                funnel=funnel if is_last else None,        # сводка — в последнем
                filter_funnel=filter_funnel if is_last else None,
                waiting=waiting if is_last else 0,
                overflow=overflow if is_last else 0,
                total=going if is_last else 0,
                part=idx, parts=parts,
                start_num=(idx - 1) * MAX_ITEMS + 1,
            )
            message_id = telegram.send_message(text)
            database.mark_sent([c["key"] for c in chunk], message_id)
            sent_total += len(chunk)
            if not is_last:
                time.sleep(1)  # вежливо к Telegram между сообщениями в одну беседу

        log.info("отправка: %s новостей в %s сообщ. (в очереди осталось %s)",
                 sent_total, parts, overflow)
        send_diagnostics(sent_total)
        return 0

    except Exception as exc:
        log.error("отправка упала: %s", exc)
        log.debug(traceback.format_exc())
        # pending остаётся pending → завтрашняя отправка подхватит (at-least-once).
        try:
            telegram.send_message(render_failure("отправка", exc))
            log.info("уведомление о сбое отправлено")
        except Exception as notify_exc:
            log.critical("не смогли даже доложить о сбое: %s", notify_exc)
        return 1


def send_diagnostics(sent_count):
    """Служебное сообщение вслед за дайджестом: как отработал отбор.

    Отдельным сообщением, а не футером — так дайджест остаётся про новости, а
    диагностика не заставляет связывать свои числа с заголовками выше.
    Падение здесь не должно ронять отправку: дайджест уже доставлен, и потеря
    служебной сводки — мелочь по сравнению с паникой в чате.
    """
    try:
        text = render.render_diagnostics(
            run=database.last_filter_run(),
            pool=database.count_pool(),
            sent_count=sent_count,
            models=database.model_stats(OPENROUTER_MODELS_PRIORITY),
        )
        telegram.send_message(text)
        log.info("диагностика отправлена")
    except Exception as exc:
        log.warning("диагностику отправить не удалось: %s", exc)


USAGE = ("запуск: python digest.py [collect|filter|send]\n"
         "  collect                 — собрать ленту в базу (cron :05 каждый час)\n"
         "  filter                  — отдать накопленное ИИ (cron 08:00 и 20:00)\n"
         "  send                    — утренняя отправка, молчания не бывает (cron 08:30)\n"
         "  send --quiet-if-empty   — вечерняя отправка, при пустой очереди молчит (cron 20:30)")

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    flags = sys.argv[2:]
    if mode == "collect":
        sys.exit(run_collect())
    elif mode == "filter":
        sys.exit(run_filter())
    elif mode == "send":
        sys.exit(run_send(quiet_if_empty="--quiet-if-empty" in flags))
    else:
        print(USAGE)
        sys.exit(2)
