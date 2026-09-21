#!/usr/bin/env python3
"""Шаг 2 конвейера: думающий ИИ-этап.

Берёт собранные записи, отдаёт их в OpenRouter с инструкцией из interests.md
и возвращает только релевантное, с выжимками.

ИИ работает не собеседником, а рабочим на конвейере: получил детали —
отобрал годные — обработал — положил дальше.

Модели идут ЦЕПОЧКОЙ с фолбэком: бесплатный тариф отдаёт 429 по воле
провайдера, а дайджест стартует по расписанию без человека — некому нажать
«повтори». Порядок — по надёжности, не по скорости: в 8:30 никто не ждёт.
"""

import json
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from collect import collect
from netcfg import EGRESS_PROXY

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
INTERESTS_FILE = BASE_DIR / "interests.md"

# Сколько ждать ОДНУ модель. Было 120с; поднято до 600с 2026-09-21 по мысли
# читателей репозитория: рассуждающие модели думают долго, а спешить нам
# некуда — между отбором (08:00) и отправкой (08:30) полчаса простоя.
# Замер за всё время: медиана удачного захода 29с, максимум 159с, таймаут не
# срабатывал ни разу. То есть запас берётся на будущее, а не по нужде.
TIMEOUT = 600.0

# Потолок на ВЕСЬ заход, а не на запрос — вот что важнее числа выше. Моделей в
# цепочке три, и 600с на каждую в худшем случае дали бы полчаса с лишним: отбор
# закончился бы уже после отправки, а дайджест ушёл бы без свежих новостей.
# 1500с (25 минут) гарантируют, что заход уложится в окно до 08:30 при любом
# раскладе. Приём тот же, что в vladburba-bot, где стоит общий бюджет на ответ.
TOTAL_BUDGET = 1500.0

# Если бюджета осталось меньше этого, новую попытку не начинаем: успеть она
# всё равно не успеет, а запрос из дневной квоты спишется.
MIN_ATTEMPT = 30.0

# Провайдеры РАЗНЫЕ намеренно: 429 у одного не должен валить всю цепочку.
# Диверсификация именно по провайдеру, а не по вендору модели — OpenRouter
# может маршрутизировать разные модели на один и тот же перегруженный бэкенд.
#
# Состав пересмотрен 2026-09-20 по журналу за месяц: google/gemma-4-26b-a4b-it
# не ответила НИ РАЗУ за всё время (58 успешных заходов у nemotron, 10 у laguna,
# 0 у gemma) и стабильно отдавала 429 — то есть третье звено просто жгло запрос
# квоты, ни разу никого не выручив. Заменена на Nex AGI.
#
# Проверено живым прогоном на 20 новостях (2026-09-20, через прокси сервера):
#   nex-agi/nex-n2.5-mini     14с, выбрал 5, выжимки по-русски   → взят
#   dots-studio/dots-3-note   54с, выбрал 5, тоже чисто          → запасной
#   google/gemma-4-31b-it     Provider returned error
#   qwen/qwen3.8-27b          Provider returned error
#   thinkingmachines/inkling  доступна только через agentic harness
#
# Отвергнуты раньше (пропускали запрещённое в interests.md):
#   ling-3.0-flash · gpt-oss-20b · nemotron-nano-9b-v2 · north-mini-code
#   nemotron-3-nano-30b · nemotron-3-nano-omni-30b
OPENROUTER_MODELS_PRIORITY = [
    "nvidia/nemotron-3-super-120b-a12b:free",  # Nvidia   — основная рабочая
    "poolside/laguna-xs-2.1:free",             # Poolside — быстрая подстраховка
    "nex-agi/nex-n2.5-mini:free",              # Nex AGI  — третье звено с 2026-09-20
]


def build_prompt(items):
    """Собирает нумерованный список записей для модели."""
    lines = []
    for i, item in enumerate(items, 1):
        lines.append(f"[{i}] {item['title']}\n{item['summary'][:300]}")
    return "\n\n".join(lines)


def build_system_prompt():
    """Инструкция для ИИ: интересы из файла + требование формата."""
    interests = INTERESTS_FILE.read_text(encoding="utf-8")
    return (
        f"{interests}\n\n"
        "Тебе дан нумерованный список новостей. Правила отбора — целиком выше, "
        "в них же сказано, сколько штук брать: это ПОТОЛОК, а не цель. Раздел "
        "«Что не нужно никогда» — жёсткий запрет. Порядок в «Что важнее при "
        "прочих равных» — шкала ранжирования: когда подходящего больше потолка, "
        "выигрывает то, что выше по списку.\n\n"
        "Отбор двухшаговый, и в ответе нужны ОБА шага:\n"
        "  1. selected — те, что берём в выпуск сейчас. Не больше потолка, "
        "по убыванию важности.\n"
        "  2. rejected — те, что мимо интересов и не пригодятся никогда.\n"
        "Всё, что ты не назвал ни там, ни там, останется в очереди и будет "
        "участвовать в следующем отборе наравне со свежими новостями. Именно "
        "туда относи хорошее, что просто не поместилось в выпуск.\n\n"
        "Ответь СТРОГО в формате JSON, без пояснений и без markdown:\n"
        '{"selected": [{"n": <номер>, "summary": "<выжимка 1-2 предложения>"}], '
        '"rejected": [<номер>, <номер>]}\n'
        'Если брать нечего — верни {"selected": [], "rejected": [...]}.'
    )


def parse_response(raw):
    """Достаёт JSON из ответа модели, что бы модель вокруг него ни написала.

    Три попытки, от строгой к терпимой:
      1. весь ответ целиком — так отвечает послушная модель;
      2. содержимое ```-блока — в него заворачивают JSON по привычке из чатов;
      3. кусок от первой «{» до последней «}» — спасает, когда модель сначала
         рассуждает вслух («Хорошо, пользователь просит…»), а JSON кладёт следом.

    Третий случай не выдумка: рассуждающие модели вроде nemotron ведут себя так
    регулярно, и раньше такой ответ терялся целиком вместе с потраченным
    запросом квоты.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("модель вернула пустой ответ")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])

    raise ValueError(f"в ответе нет JSON: {text[:120]}")


def ask_model(model, system_prompt, user_prompt, api_key, timeout=TIMEOUT):
    """Один запрос к одной модели. Бросает исключение при любой беде."""
    response = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        },
        proxy=EGRESS_PROXY,
        timeout=timeout,
    )
    response.raise_for_status()

    # HTTP 200 у OpenRouter ещё не значит «ответ есть». Отказ провайдера
    # приезжает внутри тела как {"error": ...}, а пока модель думает, в поток
    # сыплются keep-alive пробелы — и если соединение оборвётся, тело окажется
    # из одних пробелов. raise_for_status оба случая пропускает, поэтому
    # конверт разбираем руками и говорим в лог, что именно пришло.
    try:
        body = response.json()
    except ValueError:
        head = (response.text or "").strip()[:120]
        raise RuntimeError(f"тело ответа не JSON: {head or 'пусто (обрыв потока)'}") from None

    if "choices" not in body:
        err = (body.get("error") or {})
        raise RuntimeError("провайдер вернул ошибку: "
                           f"{err.get('message') or str(body)[:120]}")

    message = body["choices"][0].get("message") or {}
    raw = message.get("content")
    if not raw:
        # У рассуждающих моделей содержательная часть иногда уезжает в
        # reasoning, а content приходит пустым — пробуем достать оттуда.
        raw = message.get("reasoning") or message.get("reasoning_content")
    if not raw:
        raise RuntimeError("в ответе нет текста: "
                           f"{str(message)[:120] or 'пустой message'}")

    return parse_response(raw)


def select(items, model=None):
    """Отбор через цепочку моделей с фолбэком.

    model=None  — идём по OPENROUTER_MODELS_PRIORITY до первого успеха.
    model="..." — принудительно одна модель (используется стендом bench_models).

    Возвращает (отобранные записи с выжимками, счётчики стыка).
    """
    chain = [model] if model else OPENROUTER_MODELS_PRIORITY
    # attempts — сколько запросов реально ушло в OpenRouter. Считаем ВСЕ, включая
    # провальные: дневная квота аккаунта списывает и их, а квота у нас общая с
    # vladburba-bot. Эта цифра — единственный честный расход конвейера.
    stats = {"in": len(items), "out": 0, "model": None, "failed": [],
             "attempts": 0, "error": None,
             # tries — по строке на каждое обращение: (позиция, модель, ok, причина).
             # Отсюда база узнаёт, какая модель работает, а какая всегда молчит.
             "tries": [], "rejected_keys": [], "seconds": None}

    if not items:
        return [], stats

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        stats["error"] = "нет OPENROUTER_API_KEY"
        return [], stats

    system_prompt = build_system_prompt()
    user_prompt = build_prompt(items)

    verdict = None
    started = time.monotonic()
    for position, candidate in enumerate(chain, 1):
        left = TOTAL_BUDGET - (time.monotonic() - started)
        if left < MIN_ATTEMPT:
            # Бюджет захода вышел. Молча прекращаем: следующая попытка не
            # успеет ответить до отправки, но запрос квоты спишет.
            stats["failed"].append(f"{candidate}: бюджет захода исчерпан, "
                                   f"осталось {left:.0f}с — не спрашиваем")
            break

        stats["attempts"] += 1
        attempt_started = time.monotonic()
        try:
            verdict = ask_model(candidate, system_prompt, user_prompt, api_key,
                                timeout=min(TIMEOUT, left))
            spent = time.monotonic() - attempt_started
            stats["model"] = candidate
            stats["seconds"] = round(spent, 1)
            stats["tries"].append((position, candidate, True, None, round(spent, 1)))
            break
        except Exception as exc:
            spent = time.monotonic() - attempt_started
            # Пишем и текст, а не только класс: «JSONDecodeError» в логе не
            # отличает обрыв соединения от болтливой модели, и месяц срывов
            # пришлось разбирать вручную, повторяя запросы к провайдеру.
            reason = str(exc).replace("\n", " ")[:130] or type(exc).__name__
            stats["tries"].append((position, candidate, False, reason, round(spent, 1)))
            stats["failed"].append(f"{candidate}: {type(exc).__name__} за {spent:.0f}с: {reason}")

    if verdict is None:
        stats["error"] = "вся цепочка моделей недоступна"
        return [], stats

    selected = []
    for row in verdict.get("selected", []):
        idx = row.get("n")
        # Модель возвращает НОМЕР, а не ссылку — ссылку берём из оригинала.
        # Так модель физически не может подсунуть выдуманный URL.
        if isinstance(idx, int) and 1 <= idx <= len(items):
            item = dict(items[idx - 1])
            item["ai_summary"] = (row.get("summary") or "").strip()
            selected.append(item)

    # Отвергнутые модель называет номерами — превращаем в ключи записей.
    for idx in verdict.get("rejected", []):
        if isinstance(idx, int) and 1 <= idx <= len(items):
            stats["rejected_keys"].append(items[idx - 1]["key"])

    stats["out"] = len(selected)
    return selected, stats


def main():
    items, collect_stats = collect()
    print(f"стык 1 — сбор:   HTTP {collect_stats['http']} | "
          f"в ленте {collect_stats['total']} | "
          f"свежих за {collect_stats['window_hours']}ч: {collect_stats['fresh']}")

    selected, ai_stats = select(items)
    print(f"стык 2 — отбор:  на входе {ai_stats['in']} → отобрано {ai_stats['out']} | "
          f"модель: {ai_stats['model'] or '—'}")
    for failure in ai_stats["failed"]:
        print(f"  ~ фолбэк, не ответила: {failure}")
    if ai_stats["error"]:
        print(f"  ! ошибка ИИ-этапа: {ai_stats['error']}")
    print("-" * 70)

    for i, item in enumerate(selected, 1):
        print(f"\n{i}. {item['title']}")
        print(f"   {item['ai_summary']}")
        print(f"   {item['key']}")

    if not selected and not ai_stats["error"]:
        print("\nПодходящего под интересы сегодня не нашлось.")


if __name__ == "__main__":
    main()
