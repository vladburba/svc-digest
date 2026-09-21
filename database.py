#!/usr/bin/env python3
"""База — точка развязки трёх работ конвейера.

Сборщик ПИШЕТ: свежие записи ленты ложатся сюда со статусом new. ИИ их ещё
  не видел — сборщик его больше не зовёт вообще.
Фильтр ЧИТАЕТ new и ПЕРЕВОДИТ: pending (отобрано) или rejected (забраковано).
  Работает дважды в сутки, пакетом по всему накопленному.
Отправитель ЧИТАЕТ: в 8:30 берёт всё pending, шлёт одним дайджестом, метит sent.

Статусы записи:
  new      — собрана, ИИ ещё не видел
  queued   — ИИ признал интересной, но места в выпуске не хватило; ждёт
             следующего отбора и конкурирует там со свежими на равных
  pending  — отобрана в ближайший выпуск
  sent     — отправлена, есть расписка Telegram
  rejected — мимо интересов, выбывает навсегда
  expired  — ждала дольше окна сбора и уже не новость

rejected и expired хранятся, чтобы сборщик не тащил одно и то же по кругу.

База своя, отдельная от vladburba-bot.db: у SQLite один писатель (решение 6.04),
и PII заявок/платежей дайджесту рядом не нужны.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clock import human
from dedup import dedup_key, title_key

DB_PATH = Path(__file__).resolve().parent / "digest.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL UNIQUE,   -- guid ленты, иначе link
    dedup_key     TEXT,                   -- «та же статья»: habr.com:1068692
    title         TEXT NOT NULL,
    link          TEXT NOT NULL,
    published     TEXT,                   -- ISO 8601 с зоной
    summary       TEXT,                   -- текст из ленты: его читает фильтр
    ai_summary    TEXT,                   -- выжимка от ИИ: её читает отправитель
    status        TEXT NOT NULL,          -- new | queued | pending | sent | rejected | expired
    first_seen_at TEXT NOT NULL,          -- когда сборщик обработал запись
    sent_at       TEXT,                   -- когда Telegram подтвердил приём
    message_id    INTEGER                 -- расписка Telegram
);
CREATE INDEX IF NOT EXISTS idx_news_status ON news(status);
-- ВНИМАНИЕ: индекс по dedup_key здесь НЕ создаётся. На боевой базе таблица
-- news уже существует, CREATE TABLE IF NOT EXISTS её пропускает, и колонку
-- добавляет только ALTER ниже — а индекс, объявленный тут, упал бы на «no
-- such column» раньше, чем ALTER успеет отработать. Поэтому он создаётся в
-- init_db ПОСЛЕ миграции. Поймано на копии прода 2026-08-12.

-- Журнал заходов сборщика: воронка каждого прогона. Нужен, чтобы отправитель
-- в 08:30 собрал полную статистику за сутки и вложил её в сообщение
-- (сам отправитель сбора не видит — работы развязаны).
--
-- Колонки selected/rejected остались от прежней схемы, где ИИ-отбор жил внутри
-- сбора. Теперь отбор делает filter, и у новых записей здесь всегда NULL.
-- Столбцы не удалены намеренно: в них лежит история до 2026-08-11, а DROP
-- COLUMN ради двух неиспользуемых полей — риск без выгоды.
CREATE TABLE IF NOT EXISTS collect_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at        TEXT NOT NULL,       -- ISO 8601 с зоной
    total_in_feed INTEGER,             -- сколько записей в ленте
    in_window     INTEGER,             -- из них попали в окно сбора
    already_seen  INTEGER,             -- дедуп отсеял (старые, уже в базе)
    new_items     INTEGER,             -- новых записано со статусом new
    selected      INTEGER,             -- НЕ ИСПОЛЬЗУЕТСЯ с 2026-08-11 (см. filter_runs)
    rejected      INTEGER,             -- НЕ ИСПОЛЬЗУЕТСЯ с 2026-08-11 (см. filter_runs)
    error         TEXT                 -- текст ошибки, если заход упал
);

-- Журнал заходов фильтра: по одной строке на каждое окно (08:00 и 20:00).
-- Отдельно от collect_runs, потому что это отдельная работа со своим ритмом:
-- сборщик ходит 24 раза в сутки и не тратит квоту, фильтр — дважды и тратит.
-- attempts здесь — главная цифра надзора: сколько запросов реально ушло в
-- OpenRouter (цепочка фолбэка на 429 делает их больше одного за заход).
CREATE TABLE IF NOT EXISTS filter_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at        TEXT NOT NULL,       -- ISO 8601 с зоной
    in_batch      INTEGER,             -- сколько записей ушло в модель одним пакетом
    selected      INTEGER,             -- ИИ отобрал в выпуск
    rejected      INTEGER,             -- ИИ забраковал как мимо интересов
    expired       INTEGER,             -- протухло на этом заходе (не дождалось фильтра)
    model         TEXT,                -- какая модель цепочки ответила
    attempts      INTEGER,             -- запросов к OpenRouter за заход = расход квоты
    error         TEXT                 -- текст ошибки, если заход упал
);

-- Журнал обращений к моделям: по строке на КАЖДЫЙ запрос, включая неудачные.
-- Нужен, чтобы честно отвечать на вопрос «какая модель работает, а какая
-- всегда молчит». Из filter_runs это не вывести: там виден только итог захода,
-- а состав цепочки со временем меняется (2026-09-20 третье звено заменено),
-- и восстановленная задним числом статистика приписала бы новой модели чужие
-- отказы. Копим за всё время, без скользящих окон — так решено 2026-09-21.
CREATE TABLE IF NOT EXISTS model_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at        TEXT NOT NULL,       -- когда был заход фильтра
    position      INTEGER,             -- место в цепочке на момент запроса
    model         TEXT NOT NULL,
    ok            INTEGER NOT NULL,    -- 1 ответила, 0 промолчала
    error         TEXT                 -- краткая причина отказа
);
CREATE INDEX IF NOT EXISTS idx_attempts_model ON model_attempts(model);
"""


def now_iso():
    """Метка для базы — СТРОГО UTC, всегда с хвостом +00:00.

    Не местное время: SQL сравнивает эти метки как СТРОКИ (MAX(sent_at),
    WHERE ran_at > ?). Смешаешь зоны — «02:00+03:00» станет лексикографически
    больше «01:00+00:00», хотя случилось на два часа раньше, и воронка в
    футере дайджеста посчитает не тот период. Человеку время показываем в
    MSK — этим занимается clock.py, а хранение остаётся однородным.
    """
    return datetime.now(timezone.utc).isoformat()


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Идемпотентно: зовётся на старте каждой работы.

    CREATE TABLE IF NOT EXISTS накрывает пустую базу, но НЕ доливает колонки в
    уже существующую — для боевой базы нужен ALTER. Отсюда миграции ниже:
    каждая обёрнута в try/except на «duplicate column», чтобы повторный запуск
    (а он бывает каждый час) проходил молча.
    """
    with connect() as conn:
        conn.executescript(SCHEMA)
        # 2026-08-11: сборщик перестал звать ИИ, и текст новости из ленты
        # теперь обязан пережить паузу между сбором и фильтром — значит живёт
        # в базе. До этого он существовал только в памяти одного прогона.
        try:
            conn.execute("ALTER TABLE news ADD COLUMN summary TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        # 2026-08-12: дедуп переехал с адреса статьи на её номер — Habr меняет
        # URL при переносе между блогами, и статья приходила дважды.
        try:
            conn.execute("ALTER TABLE news ADD COLUMN dedup_key TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

        # Доливаем ключи старым записям. Считаем в Python, а не в SQL: правило
        # нормализации одно на весь проект и живёт в dedup.py — раздваивать его
        # на SQL-выражение значит гарантированно рассинхронить.
        stale = conn.execute(
            "SELECT id, key, link FROM news WHERE dedup_key IS NULL"
        ).fetchall()
        if stale:
            conn.executemany(
                "UPDATE news SET dedup_key = ? WHERE id = ?",
                [(dedup_key(row["key"], row["link"]), row["id"]) for row in stale],
            )

        # 2026-09-21: отбор стал двухшаговым (фильтр «интересно/мимо», затем
        # конкурс за места в выпуске), и диагностика показывает обе стадии.
        # Эти числа знает только сам заход — значит их надо сохранять.
        for column in ("new_seen INTEGER",        # новых статей в пакете
                       "queued_seen INTEGER",     # сколько пришло из очереди
                       "interesting_new INTEGER", # новых признано интересными
                       "left_queued INTEGER"):    # осталось ждать после конкурса
            try:
                conn.execute(f"ALTER TABLE filter_runs ADD COLUMN {column}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        # Только теперь, когда колонка гарантированно есть. Индекс НЕ уникальный:
        # в базе лежат четыре пары дублей, приехавших до 2026-08-12, и UNIQUE не
        # дал бы мигрировать. Уникальность тут и не нужна — дедуп проверяет
        # наличие ключа сам, до вставки.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_news_dedup ON news(dedup_key)")
        conn.commit()


def filter_unseen(items):
    """Оставляет то, чего в базе ещё НЕТ (любой статус). → (новые, отсеяно).

    Дедуп по всей базе: new + pending + sent + rejected + expired. Если запись
    уже есть в любом статусе — мы её видели, второй раз не заводим.

    Дедуп двухуровневый, и оба уровня появились от реальных дублей:
      1) НОМЕР статьи вместо адреса — Habr переносит статью между блогами,
         URL меняется целиком, номер остаётся (поймано 11.08.2026);
      2) ЗАГОЛОВОК за последнюю неделю — один и тот же текст выходит под двумя
         разными номерами, и первый уровень тут бессилен (поймано 18.08.2026).

    Внутри одной пачки проверяем тоже, иначе лента, отдавшая статью дважды в
    одном ответе, протащила бы обе.
    """
    if not items:
        return [], 0

    # Окно для второго уровня: заголовки сравниваем только за последнюю неделю.
    # По всей базе сравнивать нельзя — за годы накопятся законные совпадения
    # вроде «Дайджест новостей», и мы начнём глушить свежие статьи.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with connect() as conn:
        rows = conn.execute("SELECT dedup_key FROM news").fetchall()
        recent = conn.execute(
            "SELECT title FROM news WHERE COALESCE(published, first_seen_at) > ?",
            (cutoff,),
        ).fetchall()
    seen = {row["dedup_key"] for row in rows if row["dedup_key"]}
    seen_titles = {title_key(row["title"]) for row in recent}

    unseen = []
    for item in items:
        marker = item.get("dedup_key") or dedup_key(item["key"], item.get("link", ""))
        if marker in seen:
            continue
        # Второй уровень: тот же текст под другим номером статьи. Поймано
        # 18.08.2026 — «DSL позволяют надежно использовать LLM» пришло дважды
        # под номерами 1071572 и 1071582, номерной дедуп такое пропускает.
        tkey = title_key(item.get("title", ""))
        if tkey and tkey in seen_titles:
            continue
        seen.add(marker)
        seen_titles.add(tkey)
        unseen.append(item)
    return unseen, len(items) - len(unseen)


def record(items, status):
    """Пишет записи с заданным статусом. INSERT OR IGNORE — повтор безвреден."""
    if not items:
        return 0
    stamp = now_iso()
    rows = [
        (
            item["key"],
            item.get("dedup_key") or dedup_key(item["key"], item.get("link", "")),
            item["title"],
            item["link"],
            item["published"].isoformat() if item.get("published") else None,
            item.get("summary", ""),
            item.get("ai_summary", ""),
            status,
            stamp,
        )
        for item in items
    ]
    with connect() as conn:
        cursor = conn.executemany(
            "INSERT OR IGNORE INTO news "
            "(key, dedup_key, title, link, published, summary, ai_summary, status, first_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return cursor.rowcount


def expire_old(window_hours):
    """Всё, что ждёт отбора дольше окна сбора → expired. → сколько протухло.

    Чистит и новые, и очередь: «вечная конкуренция» на практике ограничена
    неделей жизни новости, иначе хороший, но невезучий материал крутился бы
    в пакете бесконечно, занимая место и токены.

    Зачем: запись выпадает из RSS-ленты через неделю, и если фильтр её к тому
    моменту не разобрал (лежал ИИ, стоял сервер), новость уже неактуальна.
    Без этого она вечно занимала бы место в пакете, вытесняя свежее.

    Возраст считаем по дате публикации, а не по first_seen_at: неделю назад
    вышедшая статья стара, даже если мы увидели её вчера. Нет даты — судим по
    моменту, когда её записал сборщик.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE news SET status='expired' "
            "WHERE status IN ('new','queued') AND COALESCE(published, first_seen_at) < ?",
            (cutoff,),
        )
        return cursor.rowcount


def get_pool(limit):
    """Кандидаты на ближайший выпуск: и новые, и ждущие в очереди.

    Очередь здесь полноправный участник, а не отстойник. Статья, признанная
    интересной, но не попавшая в пятёрку, остаётся в игре и в следующем окне
    конкурирует со свежими на равных: в урожайный день сильный материал
    подождёт, в пустой — выйдет вперёд (решение Влада 2026-09-21).

    Свежее сверху: если пул перерос потолок пакета, в модель уходит актуальное,
    а хвост доберёт следующее окно либо протухнет в expire_old — для новости
    старше недели это и правильно.

    В каждой записи есть is_new: 1 — статья попала к ИИ впервые, 0 — пришла из
    очереди. На этом различии стоит вся диагностика («новых 20, из очереди 6»).
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, title, link, published, summary, "
            "       (status = 'new') AS is_new "
            "FROM news WHERE status IN ('new', 'queued') "
            "ORDER BY COALESCE(published, first_seen_at) DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def count_pool():
    """Сколько ждёт ближайшего отбора: (новых, из очереди)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT SUM(status='new') AS n, SUM(status='queued') AS q FROM news"
        ).fetchone()
    return (row["n"] or 0), (row["q"] or 0)


def count_new():
    """Сколько всего ждёт отбора — новых плюс очередь."""
    fresh, queued = count_pool()
    return fresh + queued


def apply_filter(selected, rejected_keys, pool_keys):
    """Вердикт ИИ на пакет — три исхода вместо прежних двух.

      взят       → pending, с выжимкой, уйдёт ближайшим выпуском;
      мимо       → rejected, выбывает навсегда;
      остальные  → queued, ждут следующего конкурса.

    Третий исход появился 2026-09-21. До него всё, что не взято, считалось
    отвергнутым — и статья, которая была хороша, но не влезла в пятёрку,
    выбывала насовсем. Теперь модель явно называет только мусор, а всё
    неназванное остаётся в игре.

    Условие по текущему статусу во всех UPDATE делает повтор безвредным:
    запустится фильтр дважды (руками после сбоя, наложились окна) — второй
    заход не тронет уже переведённое и не перепишет отправленное.
    """
    n_selected = n_rejected = n_queued = 0
    chosen = {item["key"] for item in selected}
    rejected_set = set(rejected_keys)
    leftover = [k for k in pool_keys if k not in chosen and k not in rejected_set]

    with connect() as conn:
        for item in selected:
            cursor = conn.execute(
                "UPDATE news SET status='pending', ai_summary=? "
                "WHERE key=? AND status IN ('new','queued')",
                (item.get("ai_summary", ""), item["key"]),
            )
            n_selected += cursor.rowcount

        if rejected_set:
            marks = ",".join("?" * len(rejected_set))
            cursor = conn.execute(
                f"UPDATE news SET status='rejected' "
                f"WHERE key IN ({marks}) AND status IN ('new','queued')",
                list(rejected_set),
            )
            n_rejected = cursor.rowcount

        if leftover:
            marks = ",".join("?" * len(leftover))
            cursor = conn.execute(
                f"UPDATE news SET status='queued' "
                f"WHERE key IN ({marks}) AND status='new'",
                leftover,
            )
            n_queued = cursor.rowcount

    return n_selected, n_rejected, n_queued


def get_pending():
    """Накопленное к отправке: всё pending, свежее сверху."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, title, link, published, ai_summary FROM news "
            "WHERE status = 'pending' "
            "ORDER BY COALESCE(published, first_seen_at) DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def mark_sent(keys, message_id):
    """После доставки: pending → sent + расписка Telegram."""
    if not keys:
        return 0
    stamp = now_iso()
    placeholders = ",".join("?" * len(keys))
    with connect() as conn:
        cursor = conn.execute(
            f"UPDATE news SET status='sent', sent_at=?, message_id=? "
            f"WHERE key IN ({placeholders}) AND status='pending'",
            [stamp, message_id, *keys],
        )
        return cursor.rowcount


def record_run(funnel):
    """Пишет воронку одного захода сборщика в журнал collect_runs."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO collect_runs "
            "(ran_at, total_in_feed, in_window, already_seen, new_items, selected, rejected, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), funnel.get("total_in_feed"), funnel.get("in_window"),
             funnel.get("already_seen"), funnel.get("new_items"),
             funnel.get("selected"), funnel.get("rejected"), funnel.get("error")),
        )


def record_filter_run(row):
    """Пишет итог одного захода фильтра в журнал filter_runs."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO filter_runs "
            "(ran_at, in_batch, selected, rejected, expired, model, attempts, error, "
            " new_seen, queued_seen, interesting_new, left_queued) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), row.get("in_batch"), row.get("selected"), row.get("rejected"),
             row.get("expired"), row.get("model"), row.get("attempts"), row.get("error"),
             row.get("new_seen"), row.get("queued_seen"),
             row.get("interesting_new"), row.get("left_queued")),
        )


def record_model_attempts(attempts):
    """Пишет каждое обращение к модели: [(позиция, модель, ok, причина), ...].

    Пишем ВСЕ попытки, включая неудачные — иначе не ответить на вопрос «какая
    модель вообще работает». Одна строка = один потраченный запрос квоты.
    """
    if not attempts:
        return
    stamp = now_iso()
    with connect() as conn:
        conn.executemany(
            "INSERT INTO model_attempts (ran_at, position, model, ok, error) "
            "VALUES (?, ?, ?, ?, ?)",
            [(stamp, pos, model, 1 if ok else 0, err) for pos, model, ok, err in attempts],
        )


def model_stats(chain=None):
    """Сводка по моделям за ВСЁ время: сколько раз спросили и сколько ответила.

    Без скользящего окна — решение Влада 2026-09-21: нужна не «форма за
    неделю», а простой ответ, работает модель или нет.

    chain — текущий состав цепочки; модели из него показываются всегда, даже
    если их ещё ни разу не спрашивали (иначе новичок молча отсутствовал бы в
    диагностике). Выбывшие из цепочки модели, наоборот, не показываем: их
    история интересна при разборе, а не в ежедневном отчёте.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT model, COUNT(*) AS asked, "
            "       COALESCE(SUM(ok), 0) AS answered, MAX(ran_at) AS last_seen "
            "FROM model_attempts GROUP BY model"
        ).fetchall()
    known = {r["model"]: dict(r) for r in rows}
    if chain is None:
        return sorted(known.values(), key=lambda r: -r["asked"])
    return [known.get(m, {"model": m, "asked": 0, "answered": 0, "last_seen": None})
            for m in chain]


def last_filter_run():
    """Последний заход фильтра целиком — из него строится диагностика."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM filter_runs ORDER BY ran_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def funnel_since(since_iso):
    """Воронка СБОРА за период после since_iso.

    Новых — сумма по заходам; лента/окно — снимок ПОСЛЕДНЕГО захода (складывать
    40×24 бессмысленно, это одна и та же страница ленты).

    Заходы с ошибкой в сумму не идут: при провале сбора записи в базу не
    попадают вовсе, а их new_items учёлся бы дважды — сначала здесь, потом в
    удачном заходе, который те же записи наконец заведёт.
    """
    with connect() as conn:
        agg = conn.execute(
            "SELECT COUNT(*) runs, "
            "COALESCE(SUM(new_items),0) new_items "
            "FROM collect_runs WHERE ran_at > ? AND error IS NULL",
            (since_iso,),
        ).fetchone()
        failed = conn.execute(
            "SELECT COUNT(*) n FROM collect_runs WHERE ran_at > ? AND error IS NOT NULL",
            (since_iso,),
        ).fetchone()
        last = conn.execute(
            "SELECT total_in_feed, in_window, already_seen FROM collect_runs "
            "WHERE ran_at > ? AND error IS NULL ORDER BY ran_at DESC LIMIT 1",
            (since_iso,),
        ).fetchone()
    out = dict(agg)
    out["failed_runs"] = failed["n"]
    out["last_feed"] = last["total_in_feed"] if last else None
    out["last_window"] = last["in_window"] if last else None
    out["last_seen"] = last["already_seen"] if last else None
    return out


def filter_funnel_since(since_iso):
    """Итог работы ФИЛЬТРА за период после since_iso.

    Возвращает суммы (рассмотрено/отобрано/отсеяно/протухло/запросов) и,
    отдельно, список самих заходов — из него футер собирает строку про окна.
    Провалившиеся заходы здесь НУЖНЫ: молчание о них и есть то, что мы чиним.

    in_batch — сколько записей ушло в модель. Именно на нём строится футер:
    внутри одного захода in_batch = selected + rejected всегда, проверено на
    боевом журнале. А вот число собранных за период (collect_runs.new_items)
    с этими цифрами НЕ сопоставимо — модель разбирает накопленную очередь, куда
    входит собранное раньше. Их сложение в одну фразу и было той ложью, из-за
    которой футер показывал «собрано 4, отобрано 5».
    """
    with connect() as conn:
        agg = conn.execute(
            "SELECT COUNT(*) runs, "
            "COALESCE(SUM(in_batch),0) in_batch, "
            "COALESCE(SUM(selected),0) selected, "
            "COALESCE(SUM(rejected),0) rejected, "
            "COALESCE(SUM(expired),0) expired, "
            "COALESCE(SUM(attempts),0) attempts "
            "FROM filter_runs WHERE ran_at > ?",
            (since_iso,),
        ).fetchone()
        runs = conn.execute(
            "SELECT ran_at, in_batch, selected, attempts, model, error FROM filter_runs "
            "WHERE ran_at > ? ORDER BY ran_at",
            (since_iso,),
        ).fetchall()
    out = dict(agg)
    out["runs_list"] = [dict(row) for row in runs]
    return out


def last_sent_at():
    """Момент прошлой отправки (для окна агрегации воронки). None если не было."""
    with connect() as conn:
        row = conn.execute("SELECT MAX(sent_at) AS ts FROM news WHERE status='sent'").fetchone()
    return row["ts"]


def stats():
    """Сводка по статусам — для диагностики и логов."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(status='new')      AS new, "
            "SUM(status='queued')   AS queued, "
            "SUM(status='pending')  AS pending, "
            "SUM(status='sent')     AS sent, "
            "SUM(status='rejected') AS rejected, "
            "SUM(status='expired')  AS expired, "
            "MAX(sent_at) AS last_sent FROM news"
        ).fetchone()
    return dict(row)


if __name__ == "__main__":
    init_db()
    s = stats()
    print(f"база: {DB_PATH}")
    print(f"всего {s['total']} | new {s['new'] or 0} | pending {s['pending'] or 0} | "
          f"sent {s['sent'] or 0} | rejected {s['rejected'] or 0} | "
          f"expired {s['expired'] or 0}")
    print(f"последняя отправка: {human(s['last_sent']) or '—'}")
