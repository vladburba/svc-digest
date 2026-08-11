#!/usr/bin/env python3
"""Единая точка правды про время: ХРАНИМ в UTC, ПОКАЗЫВАЕМ в MSK.

Почему не «везде местное». База сравнивает ISO-строки ЛЕКСИКОГРАФИЧЕСКИ
(MAX(sent_at), WHERE ran_at > ?, ORDER BY ran_at). Пока все метки в одной
зоне — работает. Стоит смешать зоны, и сравнение поедет:

    "2026-07-27T02:00:00+03:00"   реально 2026-07-26T23:00 UTC
    "2026-07-27T01:00:00+00:00"   реально 2026-07-27T01:00 UTC  ← позже

как строки первая БОЛЬШЕ, хотя случилась раньше. Поэтому в базу пишем строго
UTC (database.now_iso), а человеку показываем MSK — логи, дайджест, CLI.

Почему не TZ=Europe/Moscow в контейнере: это чинит показ, но чинит его через
окружение — забудешь переменную при переносе на другой хост, и время молча
разъедется опять. Зона прибита здесь, в коде, и не зависит от того, где
контейнер запущен.
"""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Прибито гвоздями намеренно: сервис личный, один часовой пояс — Владов.
# Москва без перехода на летнее время, так что смещение всегда +03:00.
LOCAL_TZ = ZoneInfo("Europe/Moscow")
HUMAN_FMT = "%Y-%m-%d %H:%M:%S %Z"


def now_local():
    """Текущий момент в MSK — для показа человеку."""
    return datetime.now(LOCAL_TZ)


def to_local(moment):
    """Любой aware-datetime → MSK. None остаётся None."""
    return moment.astimezone(LOCAL_TZ) if moment else None


def human(moment_iso):
    """ISO-строка из базы (она в UTC) → читаемое местное время."""
    if not moment_iso:
        return None
    return to_local(datetime.fromisoformat(moment_iso)).strftime(HUMAN_FMT)


def day_start_iso():
    """Начало текущих МОСКОВСКИХ суток, но в UTC — для сравнения с базой.

    Нужно вечернему дайджесту: он подводит итог за сутки, а сутки у человека
    календарные и московские. В базе же метки в UTC, поэтому 00:00 MSK надо
    отдать как «21:00 предыдущего дня UTC» — иначе сравнение строк в SQL
    отрежет не тот кусок.
    """
    midnight_msk = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_msk.astimezone(timezone.utc).isoformat()


class LocalFormatter(logging.Formatter):
    """Время в логах — в MSK, а не в зоне контейнера.

    Штатный logging берёт time.localtime, а внутри контейнера это UTC: одно
    и то же событие оказывалось 23:33 в syslog хоста и 20:33 в digest.log.
    Суффикс %Z печатает саму зону («MSK») — время самоописательно, гадать
    при разборе логов больше не надо.
    """

    def formatTime(self, record, datefmt=None):
        moment = datetime.fromtimestamp(record.created, LOCAL_TZ)
        return moment.strftime(datefmt or HUMAN_FMT)
