#!/usr/bin/env python3
"""svc-digest — две развязанные работы, общаются через базу.

    python digest.py collect   СБОРЩИК   — часто (cron ~ раз в час, весь день)
    python digest.py send      ОТПРАВИТЕЛЬ — один раз (cron 09:00)

Развязка даёт главное: отправка в 9:00 не зависит от того, живы ли RSS и ИИ
в этот момент — дайджест уже лежит готовым в базе. Сборщик весь день
ретраится сам собой: лёг провайдер сейчас — подберёт через час.

Два сквозных правила урока 6.09:
  · ЛОГ НА КАЖДОМ СТЫКЕ — видно, где схлопнулось.
  · СИГНАЛ В ЛЮБОМ ИСХОДЕ — но только у ОТПРАВИТЕЛЯ (он общается с человеком).
    Сборщик — молчаливый рабочий: шумит только в лог, человека не дёргает,
    иначе при ретрае раз в час завалил бы уведомлениями.
"""

import logging
import sys
import traceback
from pathlib import Path

import database
import telegram
from collect import collect
from filter_ai import select
from render import MAX_ITEMS, render_digest, render_failure

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "digest.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("digest")

# КРИТИЧНО: httpx на INFO пишет полный URL, а у Telegram Bot API токен лежит
# ПРЯМО В ПУТИ (/bot<TOKEN>/sendMessage). Глушим, чтобы токен не тёк в лог.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def run_collect():
    """СБОРЩИК: пополняет базу. Человека не трогает — только лог.

    Каждый заход пишет свою воронку в collect_runs — отправитель соберёт из
    них статистику за сутки и вложит в сообщение (сам сбора не видит).
    """
    database.init_db()
    funnel = {"total_in_feed": None, "in_window": None, "already_seen": None,
              "new_items": 0, "selected": 0, "rejected": 0, "error": None}

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

    selected, ai = select(unseen)
    for failure in ai["failed"]:
        log.warning("фолбэк: %s", failure)
    if ai["error"]:
        funnel["error"] = ai["error"]
        database.record_run(funnel)
        log.warning("ИИ недоступен (%s) — новых не пишем, подберём в следующий заход",
                    ai["error"])
        return 1

    picked = {item["key"] for item in selected}
    rejected = [item for item in unseen if item["key"] not in picked]
    funnel["selected"] = database.record(selected, "pending")
    funnel["rejected"] = database.record(rejected, "rejected")
    database.record_run(funnel)

    log.info("отбор: %s новых → pending %s · rejected %s · модель %s",
             len(unseen), funnel["selected"], funnel["rejected"], ai["model"])
    st = database.stats()
    log.info("база: pending %s · sent %s · rejected %s",
             st["pending"] or 0, st["sent"] or 0, st["rejected"] or 0)
    return 0


def run_send():
    """ОТПРАВИТЕЛЬ: раз в 09:00. Шлёт накопленное, сигнал в любом исходе."""
    database.init_db()
    try:
        pending = database.get_pending()

        # Воронка за сутки для футера сообщения: агрегат заходов сборщика
        # с прошлой отправки (или за всё время, если отправки ещё не было).
        since = database.last_sent_at() or "1970-01-01T00:00:00+00:00"
        funnel = database.funnel_since(since)

        if not pending:
            # Heartbeat: молчание неотличимо от «контейнер не стартовал».
            telegram.send_message(render_digest([], funnel))
            log.info("отправка: pending пуст — послан сигнал «нового нет»")
            return 0

        # Показываем не больше MAX_ITEMS (читаемость + лимит Telegram).
        # Метим sent ТОЛЬКО показанное — остаток остаётся pending и придёт
        # в следующий дайджест (outbox: не показал → не потерял).
        batch = pending[:MAX_ITEMS]
        overflow = len(pending) - len(batch)

        text = render_digest(batch, funnel, overflow)
        message_id = telegram.send_message(text)
        marked = database.mark_sent([p["key"] for p in batch], message_id)
        log.info("отправка: показано %s (в очереди осталось %s) → message_id=%s · помечено sent %s",
                 len(batch), overflow, message_id, marked)
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


USAGE = "запуск: python digest.py [collect|send]"

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "collect":
        sys.exit(run_collect())
    elif mode == "send":
        sys.exit(run_send())
    else:
        print(USAGE)
        sys.exit(2)
