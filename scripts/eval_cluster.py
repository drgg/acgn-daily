#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同题归并的阈值校准
==================
用 eval/cluster_gold.jsonl 里人工标注的报道对，评测 aggregate.py 的 same_story() 判得准不准。
每行一对报道：a / b（source、lang、title、zh 中文标题、published）、same（是否同一事件）、
split（dev 开发集 / holdout 留出集，按日期切：10-19 之后的归留出集）。

  python scripts/eval_cluster.py          用 feeds.yml 的 cluster 参数，列出判错的样本
  python scripts/eval_cluster.py --grid   在开发集上扫阈值组合，再到留出集上看最好的几组

标注口径：同一次发生（同一发布、同一公告、同一预告片），以及围绕这次发生的报道
（发售当天的评测、同一场试玩会的稿子）算同一事件；同一作品的不同新闻、多话题合集算不同。
"""
import itertools
import json
import sys
from pathlib import Path

import aggregate as ag

GOLD = Path(__file__).resolve().parent.parent / "eval" / "cluster_gold.jsonl"


def as_item(x):
    return {"source": x["source"], "lang": x["lang"], "title": x["title"],
            "published": x["published"], "i18n": {"zh": {"title": x["zh"]}}}


def predict(row, cfg):
    a, b = as_item(row["a"]), as_item(row["b"])
    return ag.story_candidate(a) and ag.story_candidate(b) and ag.same_story(a, b, cfg)


def score(rows, cfg):
    tp = fp = fn = 0
    wrong = []
    for r in rows:
        y, yhat = r["same"], predict(r, cfg)
        tp += y and yhat
        fp += (not y) and yhat
        fn += y and not yhat
        if y != yhat:
            wrong.append(r)
    p = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * rc / (p + rc) if p + rc else 0.0
    return p, rc, f, wrong


def line(name, rows, cfg):
    p, r, f, _ = score(rows, cfg)
    pos = sum(x["same"] for x in rows)
    return f"{name:8s} {len(rows):4d} 对（同题 {pos:3d}）  查准 {p:.2f}  查全 {r:.2f}  F1 {f:.2f}"


def main():
    rows = [json.loads(s) for s in GOLD.read_text(encoding="utf-8").splitlines() if s.strip()]
    dev = [r for r in rows if r["split"] == "dev"]
    hold = [r for r in rows if r["split"] == "holdout"]

    if "--grid" in sys.argv:
        grid = itertools.product([0.2, 0.25, 0.3, 0.35, 0.4], [0.5, 0.6, 0.7, 0.8, 0.9],
                                 [0.5, 0.55, 0.6, 0.7, 0.8], [24, 48, 72, 10 ** 6])
        results = []
        for t, e, bare, w in grid:
            cfg = dict(ag.CLUSTER, title_min=t, entity_min=e, bare_title_min=bare, window_hours=w)
            p, r, f, _ = score(dev, cfg)
            results.append((p >= 0.9, r, p, cfg))
        results.sort(key=lambda x: x[:3], reverse=True)
        print("开发集上查准 ≥0.9 时查全最高的 5 组，及其在留出集上的表现：")
        for _, _, _, cfg in results[:5]:
            print(f"  title_min={cfg['title_min']} entity_min={cfg['entity_min']} "
                  f"bare_title_min={cfg['bare_title_min']} window_hours={cfg['window_hours']}")
            print("   ", line("开发集", dev, cfg))
            print("   ", line("留出集", hold, cfg))
        return

    cfg = ag.CLUSTER
    print(f"参数：{ {k: v for k, v in cfg.items() if k != 'roundup'} }")
    for name, part in (("开发集", dev), ("留出集", hold), ("全部", rows)):
        print(line(name, part, cfg))
    _, _, _, wrong = score(rows, cfg)
    print(f"\n判错 {len(wrong)} 对（错并 = 标注为不同却并了；漏并 = 标注为同题却没并）：")
    for r in wrong:
        a, b = as_item(r["a"]), as_item(r["b"])
        t, e = ag.pair_features(a, b)
        kind = "漏并" if r["same"] else "错并"
        print(f"  [{kind}] 标题 {t:.2f} 作品名 {'-' if e is None else f'{e:.2f}'} | "
              f"{r['a']['source']}：{r['a']['title'][:40]} ‖ {r['b']['source']}：{r['b']['title'][:40]}")


if __name__ == "__main__":
    main()
