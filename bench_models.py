#!/usr/bin/env python3
"""Стенд: гоняет БОЕВУЮ задачу отбора на разных моделях и сравнивает результат.

Тест = тот же код, что пойдёт в прод (filter_ai.select), только с подменой
модели. Проверяем не «отвечает ли», а «решает ли нашу задачу».
"""

import time

import filter_ai
from collect import collect

CANDIDATES = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "openai/gpt-oss-20b:free",
    "inclusionai/ling-3.0-flash:free",
    "cohere/north-mini-code:free",
    "poolside/laguna-s-2.1:free",
    "poolside/laguna-xs-2.1:free",
    "poolside/laguna-m.1:free",
]

SAMPLE = 12          # сколько записей отдаём модели
filter_ai.TIMEOUT = 90.0

# Позиции, которые interests.md обязан отсечь, — маркер качества отбора.
TRAPS = ("WH40K", "казахск", "1975")


def main():
    items, cstats = collect()
    items = items[:SAMPLE]
    print(f"выборка: {len(items)} записей из {cstats['total']} в ленте\n")

    print(f"{'МОДЕЛЬ':<50} {'ВРЕМЯ':>7} {'ОТБОР':>7}  ЗАМЕЧАНИЕ")
    print("-" * 100)

    results = []
    for model in CANDIDATES:
        started = time.time()
        selected, stats = filter_ai.select(items, model=model)
        elapsed = time.time() - started

        if stats["error"]:
            note = stats["error"][:52]
            picked = "—"
        else:
            titles = " ".join(i["title"] for i in selected)
            hits = [t for t in TRAPS if t in titles]
            note = ("пропустил мусор: " + ", ".join(hits)) if hits else "мусор отсечён"
            picked = f"{stats['out']}/{len(items)}"
            results.append((model, elapsed, stats["out"], not hits))

        print(f"{model:<50} {elapsed:>6.1f}с {picked:>7}  {note}")

    print("\n--- пригодные, по скорости ---")
    for model, elapsed, out, clean in sorted(results, key=lambda r: r[1]):
        mark = "чисто" if clean else "с мусором"
        print(f"  {elapsed:>5.1f}с  отобрал {out:>2}  {mark:<10} {model}")


if __name__ == "__main__":
    main()
