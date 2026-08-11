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
from pathlib import Path

import httpx
from dotenv import load_dotenv

from collect import collect
from netcfg import EGRESS_PROXY

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
INTERESTS_FILE = BASE_DIR / "interests.md"
TIMEOUT = 120.0

# Отобраны боевым прогоном bench_models.py 2026-07-24: все три отсекли
# контрольную запись, которую interests.md запрещает. Провайдеры РАЗНЫЕ —
# чтобы 429 одного не валил всю цепочку.
#
# Отвергнуты (пропускают запрещённое в interests.md):
#   ling-3.0-flash · gpt-oss-20b · nemotron-nano-9b-v2 · north-mini-code
#   nemotron-3-nano-30b · nemotron-3-nano-omni-30b
# Отвергнуты (429 на прогоне): gemma-4-31b-it · laguna-s-2.1 · laguna-m.1
OPENROUTER_MODELS_PRIORITY = [
    "nvidia/nemotron-3-super-120b-a12b:free",  # Nvidia   — чисто, боевая у бота
    "poolside/laguna-xs-2.1:free",             # Poolside — чисто, 9с
    "google/gemma-4-26b-a4b-it:free",          # Google   — чисто, 6с
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
        "Тебе дан нумерованный список новостей. Отбери только те, что подходят "
        "под интересы выше. Остальные молча отбрось — лучше вернуть мало и по делу, "
        "чем много и мимо. Раздел «Не нужно» — жёсткий запрет, а не пожелание.\n\n"
        "Ответь СТРОГО в формате JSON, без пояснений и без markdown:\n"
        '{"selected": [{"n": <номер из списка>, "summary": "<выжимка 1-2 предложения>"}]}\n'
        'Если подходящего нет — верни {"selected": []}.'
    )


def parse_response(raw):
    """Достаёт JSON из ответа модели, даже если он завёрнут в ```-блок."""
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)


def ask_model(model, system_prompt, user_prompt, api_key):
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
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"]
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
             "attempts": 0, "error": None}

    if not items:
        return [], stats

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        stats["error"] = "нет OPENROUTER_API_KEY"
        return [], stats

    system_prompt = build_system_prompt()
    user_prompt = build_prompt(items)

    verdict = None
    for candidate in chain:
        stats["attempts"] += 1
        try:
            verdict = ask_model(candidate, system_prompt, user_prompt, api_key)
            stats["model"] = candidate
            break
        except Exception as exc:
            stats["failed"].append(f"{candidate}: {type(exc).__name__}")

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
