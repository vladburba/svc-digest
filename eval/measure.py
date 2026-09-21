#!/usr/bin/env python3
"""Считает, насколько отбор бота совпадает с оценками человека.

    python eval/measure.py 2026-09-21
    python eval/measure.py 2026-09-21 2026-09-28   # сравнить два замера

Сети не требует и квоту не тратит: сравнивает уже принятые ботом решения
(что он отправил и что забраковал) с ручной разметкой.

Две ошибки считаются РАЗДЕЛЬНО, и это главное в отчёте:
  · зря прислал — потратил внимание читателя;
  · зря выбросил — читатель не увидел нужного и даже не знает об этом.
Вторая дороже и незаметнее, поэтому выводится отдельной строкой со списком.
Оценки «не уверен» в счёт не идут — на шатком учиться нельзя.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def measure(tag):
    sample = {r["n"]: r for r in json.loads((HERE / f"{tag}-sample.json").read_text(encoding="utf-8"))}
    labels = json.loads((HERE / f"{tag}-labels.json").read_text(encoding="utf-8"))["labels"]
    sure = {int(n): v for n, v in labels.items() if v in "+-"}
    if not sure:
        print(f"{tag}: разметки нет"); return None

    agree = [n for n, v in sure.items() if (v == "+") == (sample[n]["bot_verdict"] == "sent")]
    extra = [n for n, v in sure.items() if v == "-" and sample[n]["bot_verdict"] == "sent"]
    lost = [n for n, v in sure.items() if v == "+" and sample[n]["bot_verdict"] == "rejected"]

    print(f"\n=== {tag} ===")
    print(f"размечено {len(labels)} · уверенных {len(sure)} · "
          f"совпало {len(agree)} = {len(agree)/len(sure)*100:.0f}%")
    print(f"  зря прислал:  {len(extra)}")
    for n in extra:
        print(f"      {n}. {sample[n]['title'][:64]}")
    print(f"  зря выбросил: {len(lost)}")
    for n in lost:
        print(f"      {n}. {sample[n]['title'][:64]}")
    return {"tag": tag, "sure": len(sure), "agree": len(agree),
            "extra": len(extra), "lost": len(lost)}


def main():
    tags = sys.argv[1:] or ["2026-09-21"]
    results = [r for r in (measure(t) for t in tags) if r]
    if len(results) > 1:
        print("\n=== сравнение ===")
        print(f"{'замер':14} {'согласие':>9} {'зря прислал':>12} {'зря выбросил':>13}")
        for r in results:
            print(f"{r['tag']:14} {r['agree']/r['sure']*100:8.0f}% "
                  f"{r['extra']:12} {r['lost']:13}")


if __name__ == "__main__":
    main()
