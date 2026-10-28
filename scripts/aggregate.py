#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACGN 每日资讯聚合器
==================
每日由 GitHub Actions 定时运行：
  1. 读取 feeds.yml 中的 RSS 源与结构化 API 适配器
  2. 并发抓取 → 规范化 → 关键词分类 → 标题指纹去重 → 打分排序 → 机翻 → 同题归并
  3. 输出 data/latest.json（前端读取）、data/archive/YYYY-MM-DD.json（历史归档）、
     feed.xml（对外 RSS 输出，让别人也能订阅本站）与 llms.txt（给 AI Agent 的数据说明）

依赖: pip install requests feedparser pyyaml
"""

import hashlib
import html
import itertools
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import requests
import feedparser
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "feeds.yml").read_text(encoding="utf-8"))
TZ = timezone(timedelta(hours=8))  # Asia/Shanghai
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")
UA = {"User-Agent": "ACGN-Daily-Aggregator/1.0 (+https://github.com/drgg/acgn-daily)"}
TIMEOUT = 20

# 带重试的会话：429/5xx 自动退避重试，显著降低 Jikan 等公共 API 的瞬时失败率
SESSION = requests.Session()
SESSION.headers.update(UA)
SESSION.mount("https://", HTTPAdapter(
    max_retries=Retry(total=3, backoff_factor=1.5,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET", "POST"]),
    pool_maxsize=16,
))

# 每源抓取健康记录：{name, count, error}，写入输出供人工巡检
HEALTH = []


def report(name, count, error=None):
    entry = {"name": name, "count": count, "error": error}
    for i, h in enumerate(HEALTH):     # 重试后覆盖旧记录
        if h["name"] == name:
            HEALTH[i] = entry
            break
    else:
        HEALTH.append(entry)
    if error:
        print(f"[WARN] {name}: {error}", file=sys.stderr)
    elif count == 0:
        print(f"[WARN] {name}: 返回 0 条（源可能已失效，建议巡检）", file=sys.stderr)


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def norm_title(t: str) -> str:
    """标题归一化，用于跨源去重：去空白/标点/大小写差异"""
    t = re.sub(r"[\s　]+", "", t or "")
    t = re.sub(r"[「」『』【】\[\]（）()《》<>!！?？:：、,，.。·~～\-—_|｜]", "", t)
    return t.lower()


def fingerprint(title: str) -> str:
    return hashlib.md5(norm_title(title).encode("utf-8")).hexdigest()[:12]


# 预编译分类关键词（小写），避免每条目每关键词重复 lower()
_CLASSIFIER = [(cat, [kw.lower() for kw in kws])
               for cat, kws in CONFIG.get("classifier", {}).items()]


def classify(title: str, default: str) -> str:
    """当源分类为 general 时，按关键词表兜底归类"""
    if default != "general":
        return default
    low = (title or "").lower()
    for cat, kws in _CLASSIFIER:
        if any(kw in low for kw in kws):
            return cat
    return "general"


def clean_text(s: str, limit=None) -> str:
    """去 HTML 标签、反转义实体、压缩空白"""
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] if limit else s


def make_item(title, url, source, category, lang, weight, published=None, summary=""):
    title = clean_text(title)
    return {
        "id": fingerprint(title),
        "title": title,
        "url": url,
        "source": source,
        "category": category,
        "lang": lang,
        "weight": weight,
        "published": published or datetime.now(TZ).isoformat(timespec="seconds"),
        "summary": clean_text(summary, 300),
    }


# ----------------------------------------------------------------------
# RSS 抓取
# ----------------------------------------------------------------------
def fetch_rss(feed_cfg, limit):
    items = []
    try:
        # 个别源按 UA 拒绝抓取器（如 natalie 405），允许在 feeds.yml 里按源覆盖 UA
        headers = {"User-Agent": feed_cfg["ua"]} if feed_cfg.get("ua") else None
        resp = SESSION.get(feed_cfg["url"], headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()  # HTTP 错误页不再被静默解析成 0 条
        parsed = feedparser.parse(resp.content)
        for e in parsed.entries[:limit]:
            title = e.get("title", "")
            if not title.strip():
                continue
            pub = None
            for key in ("published_parsed", "updated_parsed"):
                if getattr(e, key, None):
                    pub = datetime.fromtimestamp(
                        time.mktime(getattr(e, key)), tz=TZ
                    ).isoformat(timespec="seconds")
                    break
            cat = classify(title, feed_cfg["category"])
            items.append(make_item(
                title, e.get("link"), feed_cfg["name"], cat,
                feed_cfg.get("lang", "zh"), feed_cfg.get("weight", 5),
                pub, e.get("summary", ""),
            ))
        report(feed_cfg["name"], len(items))
    except Exception as exc:  # 单源失败不影响整体
        report(feed_cfg["name"], 0, f"{type(exc).__name__}: {exc}")
    return items


# ----------------------------------------------------------------------
# 结构化 API 适配器
# ----------------------------------------------------------------------
def api_anilist_airing(limit=20):
    """AniList GraphQL：抓取今日放送的动画集数"""
    now = int(time.time())
    query = """
    query ($start:Int,$end:Int){
      Page(perPage:%d){
        airingSchedules(airingAt_greater:$start, airingAt_lesser:$end, sort:TIME){
          airingAt episode
          media{ title{ native romaji } siteUrl }
        }
      }
    }""" % limit
    r = SESSION.post(
        "https://graphql.anilist.co",
        json={"query": query, "variables": {"start": now - 43200, "end": now + 43200}},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for s in r.json()["data"]["Page"]["airingSchedules"]:
        t = s["media"]["title"]["native"] or s["media"]["title"]["romaji"]
        at = datetime.fromtimestamp(s["airingAt"], tz=TZ).strftime("%H:%M")
        out.append(make_item(
            f"【今日放送 {at}】{t} 第{s['episode']}集",
            s["media"]["siteUrl"], "AniList 放送表", "anime", "ja", 6,
        ))
    return out


def api_jikan_season_now(limit=10):
    """Jikan（MyAnimeList 非官方 API）：本季新番热度榜"""
    r = SESSION.get(
        f"https://api.jikan.moe/v4/seasons/now?limit={limit}&sfw=true",
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for a in r.json().get("data", []):
        score = a.get("score")
        out.append(make_item(
            f"【本季热番】{a['title']}" + (f"（MAL {score}分）" if score else ""),
            a["url"], "MyAnimeList 季度榜", "anime", "en", 5,
            summary=(a.get("synopsis") or "")[:300],
        ))
    return out


def api_bangumi_calendar(limit=15):
    """Bangumi 番组计划：每日放送（含中文译名）"""
    r = SESSION.get("https://api.bgm.tv/calendar", timeout=TIMEOUT)
    r.raise_for_status()
    weekday = datetime.now(TZ).isoweekday()  # 1-7
    out = []
    for day in r.json():
        if day["weekday"]["id"] != weekday:
            continue
        for it in day["items"][:limit]:
            name = it.get("name_cn") or it.get("name")
            out.append(make_item(
                f"【今日更新】{name}",
                f"https://bgm.tv/subject/{it['id']}",
                "Bangumi 每日放送", "anime", "zh", 6,
            ))
    return out


def api_mangadex_latest(limit=15):
    """MangaDex：最新更新章节（includes=manga 以带出作品名）"""
    r = SESSION.get(
        "https://api.mangadex.org/chapter",
        params={"limit": limit, "order[readableAt]": "desc",
                "translatedLanguage[]": ["zh", "zh-hk", "en"],
                "includes[]": "manga"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for ch in r.json().get("data", []):
        attr = ch["attributes"]
        manga = ""
        for rel in ch.get("relationships", []):
            if rel.get("type") == "manga":
                titles = rel.get("attributes", {}).get("title", {})
                manga = titles.get("zh") or titles.get("ja") or titles.get("en") \
                    or next(iter(titles.values()), "")
                break
        chap = f"第 {attr.get('chapter') or '?'} 话"
        if attr.get("title"):
            chap += f"「{attr['title']}」"
        label = f"{manga} {chap}" if manga else chap
        out.append(make_item(
            f"【漫画更新】{label}",
            f"https://mangadex.org/chapter/{ch['id']}",
            "MangaDex", "comic", "zh", 4,
        ))
    return out


ADAPTERS = {
    "anilist_airing": api_anilist_airing,
    "jikan_season_now": api_jikan_season_now,
    "bangumi_calendar": api_bangumi_calendar,
    "mangadex_latest": api_mangadex_latest,
}


def fetch_api(api_cfg):
    fn = ADAPTERS.get(api_cfg["adapter"])
    if not fn:
        report(api_cfg["adapter"], 0, "未知适配器")
        return []
    try:
        items = fn()
        report(api_cfg["adapter"], len(items))
        return items
    except Exception as exc:
        report(api_cfg["adapter"], 0, f"{type(exc).__name__}: {exc}")
        return []


# ----------------------------------------------------------------------
# 机器翻译：标题/摘要译成中日英，供前端三语切换
# 免费接口 + 指纹缓存（同一条目只翻一次）+ 熔断（接口不可用则整体放弃，
# 前端自动回退原文），翻译失败不影响主流水线
# ----------------------------------------------------------------------
GT_LANG = {"zh": "zh-CN", "ja": "ja", "en": "en"}
_gt_stats = {"ok": 0, "fail": 0}


def gt_translate(text, src, tgt):
    if (_gt_stats["fail"] >= 8 and _gt_stats["ok"] == 0) or _gt_stats["fail"] >= 40:
        return None  # 熔断：接口不可用或失败过多，不再逐条尝试（下次运行续翻）
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": GT_LANG.get(src, "auto"),
                    "tl": GT_LANG[tgt], "dt": "t", "q": text},
            headers=UA, timeout=10,
        )
        r.raise_for_status()
        out = "".join(seg[0] for seg in r.json()[0] if seg and seg[0]).strip()
        _gt_stats["ok"] += 1
        return out or None
    except Exception:
        _gt_stats["fail"] += 1
        return None


def translate_items(items):
    tcfg = CONFIG.get("translate", {})
    if not tcfg.get("enabled"):
        return
    targets = [l for l in tcfg.get("targets", ["zh", "ja", "en"]) if l in GT_LANG]
    cache_file = ROOT / "data" / "i18n_cache.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        cache = {}

    # 只翻缓存里没有的（标题必备；有摘要的还需摘要）
    jobs = []
    for it in items:
        for tgt in targets:
            if tgt == it["lang"]:
                continue
            have = cache.get(it["id"], {}).get(tgt) or {}
            if have.get("title") and (have.get("summary") or not it["summary"]):
                continue
            jobs.append((it, tgt))

    def work(job):
        it, tgt = job
        title = gt_translate(it["title"], it["lang"], tgt)
        summary = gt_translate(it["summary"], it["lang"], tgt) if it["summary"] else ""
        time.sleep(0.1)  # 对免费接口保持克制
        return it["id"], tgt, title, summary

    with ThreadPoolExecutor(max_workers=4) as pool:
        for fut in as_completed([pool.submit(work, j) for j in jobs]):
            iid, tgt, title, summary = fut.result()
            if title is None:
                continue  # 失败条目下次运行自动重试
            entry = cache.setdefault(iid, {}).setdefault(tgt, {})
            entry["title"] = title
            if summary:
                entry["summary"] = summary

    # 挂到条目上（仅非原文语言且缓存命中的）
    for it in items:
        tr = {t: cache[it["id"]][t] for t in targets
              if t != it["lang"] and cache.get(it["id"], {}).get(t)}
        if tr:
            it["i18n"] = tr

    # 缓存修剪：当前条目优先保留，其余按最近插入保留至上限
    cache_max = tcfg.get("cache_max", 4000)
    pruned = {it["id"]: cache[it["id"]] for it in items if it["id"] in cache}
    for k, v in reversed(list(cache.items())):
        if len(pruned) >= cache_max:
            break
        pruned.setdefault(k, v)
    cache_file.write_text(json.dumps(pruned, ensure_ascii=False), encoding="utf-8")
    print(f"[I18N] 待翻 {len(jobs)} 项，成功 {_gt_stats['ok']} 次请求，"
          f"失败 {_gt_stats['fail']} 次，缓存 {len(pruned)} 条")


# ----------------------------------------------------------------------
# 同题归并与热度：不同来源报道同一件事时并成一条，按独立来源数算热度
# 思路借鉴 AIHOT（KKKKhazix/AIHOT 的 events/recall.ts 与 events/hot.ts）：
#   - 比较标题的字符二元组重合度，跨语言时用机翻后的中文标题；
#     两边都有作品名（「」『』《》内）时，作品名也必须对上；
#   - 每个来源只算一次，按发布时间以半衰期衰减后相加即热度；
#   - 合集、榜单、「在哪看」这类多话题或常青条目不参与。
# 本站不调模型，所以阈值用 eval/cluster_gold.jsonl 的人工标注校准：
# 改了 feeds.yml 的 cluster 参数后跑 python scripts/eval_cluster.py 看查准率和查全率
# ----------------------------------------------------------------------
_PUNCT = re.compile(r"[\s　「」『』【】\[\]（）()《》<>!！?？:：、,，.。·~～\-—_|｜\"'“”‘’…/★☆＆&#＃%％+＋=＝*＊]")
_NOISE = re.compile(r"[\d年月日時分号號期第回話话集]+")  # 只由日期、序号组成的二元组不算重合
_ENTITY = re.compile(r"[「『《“\"]([^」』》”\"]{2,})[」』》”\"]")
CLUSTER = CONFIG.get("cluster", {})
_ROUNDUP = re.compile("|".join(map(re.escape, CLUSTER.get("roundup", []))) or r"(?!)")
_RSS_NAMES = {f["name"] for f in CONFIG.get("rss", [])}


def _grams(s):
    c = _PUNCT.sub("", (s or "").lower())
    return {g for g in (c[i:i + 2] for i in range(len(c) - 1)) if not _NOISE.fullmatch(g)}


def overlap(a, b):
    """字符二元组重合度：交集 / 较短一方的二元组数"""
    g, h = _grams(a), _grams(b)
    return len(g & h) / min(len(g), len(h)) if g and h else 0.0


def zh_title(it):
    """比较用的中文标题：中文源用原文，其余用机翻（没翻出来时为空）"""
    return it["title"] if it["lang"] == "zh" else it.get("i18n", {}).get("zh", {}).get("title", "")


def pair_features(a, b):
    """(标题重合度, 作品名重合度)；任一边没有作品名时后者为 None。同语言时原文也比，取高者"""
    texts = [(zh_title(a), zh_title(b))]
    if a["lang"] == b["lang"]:
        texts.append((a["title"], b["title"]))
    title = max(overlap(x, y) for x, y in texts)
    ents = [overlap(e, f) for x, y in texts
            for e in _ENTITY.findall(x) for f in _ENTITY.findall(y)]
    return title, (max(ents) if ents else None)


def story_candidate(it):
    """参与归并的条目：RSS 新闻源，且不是合集/榜单类"""
    return it["source"] in _RSS_NAMES and not _ROUNDUP.search(it["title"])


def same_story(a, b, cfg=CLUSTER):
    hours = abs((datetime.fromisoformat(a["published"])
                 - datetime.fromisoformat(b["published"])).total_seconds()) / 3600
    if hours > cfg.get("window_hours", 72):
        return False
    title, ent = pair_features(a, b)
    if ent is None:
        return title >= cfg.get("bare_title_min", 0.55)
    return ent >= cfg.get("entity_min", 0.8) and title >= cfg.get("title_min", 0.3)


def cluster_items(items, now):
    """同一事件的多源报道并成一组：代表条目带 story，其余条目带 dup_of。返回组数"""
    for it in items:  # 当日归档并回来的旧结果作废，每次重算
        it.pop("story", None)
        it.pop("dup_of", None)
    if not CLUSTER.get("enabled", True):
        return 0
    pool = [it for it in items if story_candidate(it)]
    parent = list(range(len(pool)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in itertools.combinations(range(len(pool)), 2):
        if pool[i]["source"] != pool[j]["source"] and same_story(pool[i], pool[j]):
            parent[find(i)] = find(j)
    groups = {}
    for i, it in enumerate(pool):
        groups.setdefault(find(i), []).append(it)

    half_life = CLUSTER.get("half_life_hours", 24)
    stories = 0
    for members in groups.values():
        latest = {}  # 来源 -> 该来源在组内最新一篇的发布时间
        for it in members:
            ts = datetime.fromisoformat(it["published"])
            latest[it["source"]] = max(latest.get(it["source"], ts), ts)
        if len(latest) < 2:
            continue
        # 代表条目：权重最高的来源里最早发的那篇
        rep = max(members, key=lambda x: (x["weight"], -datetime.fromisoformat(x["published"]).timestamp()))
        heat = sum(0.5 ** (max((now - ts).total_seconds(), 0) / 3600 / half_life) for ts in latest.values())
        rep["story"] = {"sources": len(latest), "heat": round(heat, 2),
                        "members": [it["id"] for it in members if it is not rep]}
        for it in members:
            if it is not rep:
                it["dup_of"] = rep["id"]
        stories += 1
    return stories


# ----------------------------------------------------------------------
# RSS 输出：把聚合结果吐成 feed.xml，让别人能订阅本站
# ----------------------------------------------------------------------
def write_feed_xml(items, site):
    base = site.get("base_url", "").rstrip("/")
    entries = []
    for it in items[:50]:
        entries.append(
            "  <item>\n"
            f"    <title>{xml_escape(it['title'])}</title>\n"
            f"    <link>{xml_escape(it['url'] or '')}</link>\n"
            f"    <guid isPermaLink=\"false\">{it['id']}</guid>\n"
            f"    <pubDate>{format_datetime(datetime.fromisoformat(it['published']))}</pubDate>\n"
            f"    <source url=\"{xml_escape(it['url'] or '')}\">{xml_escape(it['source'])}</source>\n"
            + (f"    <description>{xml_escape(it['summary'])}</description>\n" if it["summary"] else "")
            + "  </item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n<channel>\n'
        "  <title>ACGN 日報</title>\n"
        f"  <link>{xml_escape(base or 'https://example.github.io/acgn-daily')}</link>\n"
        "  <description>每日自动聚合动画·漫画·游戏·轻小说资讯（标题+链接+出处，点击回原站）</description>\n"
        "  <language>zh-cn</language>\n"
        f"  <lastBuildDate>{format_datetime(datetime.now(TZ))}</lastBuildDate>\n"
        + "\n".join(entries) + "\n</channel>\n</rss>\n"
    )
    (ROOT / "feed.xml").write_text(xml, encoding="utf-8")


# ----------------------------------------------------------------------
# llms.txt：给 AI Agent 的数据说明（llmstxt.org 约定），只列真实存在的文件和源，
# 由 feeds.yml 生成，增删源后自动跟着变
# ----------------------------------------------------------------------
def write_llms_txt(site, payload):
    base = site.get("base_url", "").rstrip("/")
    cats = "anime 动画 / comic 漫画 / game 游戏 / novel 轻小说 / general 综合"
    lines = [
        "# ACGN 日報", "",
        "> 每日自动聚合动画、漫画、游戏、轻小说资讯的静态站。只收标题、链接、出处和不超过 300 字的摘要，点击回原站阅读。", "",
        "每天北京时间 6:00 和 18:00 各更新一次。下列文件都是静态 JSON / XML，匿名可读，不需要 API Key。", "",
        "## 数据", "",
        f"- [今日数据]({base}/data/latest.json): 当天全部条目，字段见下节",
        f"- [往期索引]({base}/data/archive_index.json): 有归档的日期列表（新到旧），保留最近 {site.get('archive_days', 90)} 天",
        f"- [某日归档]({base}/data/archive/{payload['date']}.json): 与今日数据同结构，把日期换成往期索引里的任一天",
        f"- [RSS]({base}/feed.xml): 最新 50 条，同一事件只出代表条目",
        "", "## 字段", "",
        "- `items[]`: `id`（标题指纹）、`title`、`url`（原文）、`source`、`category`、`lang`（zh/ja/en）、"
        "`published`（ISO 8601，北京时间）、`summary`",
        f"- `items[].category`: {cats}",
        "- `items[].i18n`: 机器翻译的标题和摘要，键为目标语言；仅供参考，引用时以原文为准",
        "- `items[].story`: 多家来源报道同一事件时出现在代表条目上：`sources` 独立来源数，"
        f"`heat` 热度（每个来源只算一次，按发布时间以 {CLUSTER.get('half_life_hours', 24)} 小时半衰期衰减后相加），"
        "`members` 同组其他条目的 id",
        "- `items[].dup_of`: 该条目已并入某事件，值为代表条目的 id；列新闻时通常跳过",
        "- `categories`: 各领域条数（不含 dup_of 条目）；`stories`: 多源事件组数；`sources`: 每个源本次的抓取健康状况",
        "", "## 信息源", "",
    ]
    lines += [f"- {f['name']}（{f.get('lang', 'zh')}，{f['category']}）" for f in CONFIG.get("rss", [])]
    lines += [f"- {a['adapter']}（结构化 API，{a['category']}）" for a in CONFIG.get("apis", []) if a.get("enabled")]
    lines += ["", "## 使用约定", "",
              "转述或引用时请注明原始出处并链接原文；不要转载全文或搬运图片。商用或扩大抓取前请先看各源的服务条款。", ""]
    (ROOT / "llms.txt").write_text("\n".join(lines), encoding="utf-8", newline="\n")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main():
    t0 = time.time()
    site = CONFIG.get("site", {})
    per_src = site.get("max_items_per_source", 15)
    max_total = site.get("max_total_items", 120)

    # 并发抓取：各源域名不同，并发不会对单一站点造成压力；
    # 相比逐源串行+sleep，整体耗时从分钟级降到十几秒。
    # 并发度过高时部分网络环境会重置 TLS 握手，故默认保守取 4，
    # 且任务错峰提交，可在 feeds.yml 的 site.concurrency 调整
    all_items = []
    jobs = {}   # name -> 无参抓取闭包，便于失败后补捞
    for feed in CONFIG.get("rss", []):
        jobs[feed["name"]] = (lambda f=feed: fetch_rss(f, per_src))
    for api in CONFIG.get("apis", []):
        if api.get("enabled"):
            jobs[api["adapter"]] = (lambda a=api: fetch_api(a))

    workers = site.get("concurrency", 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = []
        for fn in jobs.values():
            tasks.append(pool.submit(fn))
            time.sleep(0.4)
        for fut in as_completed(tasks):
            all_items += fut.result()

    # 补捞：并发阶段失败的源冷却后串行重试一轮，
    # 兜住瞬时故障（5xx、限流、不稳网络下的 TLS 重置）
    failed = [h["name"] for h in HEALTH if h["error"]]
    if failed:
        print(f"[INFO] {len(failed)} 个源失败，冷却后串行补捞……", file=sys.stderr)
        time.sleep(site.get("retry_cooldown", 12))
        for name in failed:
            all_items += jobs[name]()
            time.sleep(2)

    # 同日累积：并入当日已有归档，让早晚两次运行互相补充而非覆盖
    # （早间条目晚间可能已滚出源 feed；单源瞬时失败也能靠上次结果兜底）
    today_file = ROOT / "data" / "archive" / f"{TODAY}.json"
    if today_file.exists():
        try:
            all_items += json.loads(today_file.read_text(encoding="utf-8")).get("items", [])
        except Exception as exc:
            print(f"[WARN] 读取当日归档失败: {exc}", file=sys.stderr)

    # 去重：标题指纹 + URL 双重判重，跨源同题只保留权重最高者
    seen, seen_url = {}, set()
    for it in sorted(all_items, key=lambda x: -x["weight"]):
        if not it["title"]:
            continue
        if it["url"] and it["url"] in seen_url:
            continue
        if it["id"] in seen:
            continue
        seen[it["id"]] = it
        if it["url"]:
            seen_url.add(it["url"])
    items = list(seen.values())

    # 排序：权重 desc → 发布时间 desc（元组一次排序即可）
    items.sort(key=lambda x: (x["weight"], x["published"]), reverse=True)

    # 截断总量时给每个分类保底配额，避免低权重源所在的分类被整体挤掉
    # （否则漫画/综合等区会因全局按权重截断而空栏）
    if len(items) > max_total:
        min_per_cat = site.get("min_per_category", 10)
        by_cat = {}
        for it in items:
            by_cat.setdefault(it["category"], []).append(it)
        reserved_ids = {it["id"] for lst in by_cat.values() for it in lst[:min_per_cat]}
        reserved = [it for it in items if it["id"] in reserved_ids]
        others = [it for it in items if it["id"] not in reserved_ids]
        items = reserved + others[:max(max_total - len(reserved), 0)]
        items.sort(key=lambda x: (x["weight"], x["published"]), reverse=True)

    # 只对最终入选条目做翻译（截断后再翻，节省请求）
    translate_items(items)

    # 同题归并要用机翻后的中文标题跨语言比较，所以放在翻译之后
    stories = cluster_items(items, datetime.now(TZ))
    shown = [it for it in items if "dup_of" not in it]

    payload = {
        "date": TODAY,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "count": len(shown),
        "stories": stories,
        "categories": {c: sum(1 for i in shown if i["category"] == c)
                       for c in ["anime", "comic", "game", "novel", "general"]},
        "sources": HEALTH,   # 每源健康状况，供巡检
        "items": items,      # 含 dup_of 条目，前端把它们挂到代表条目下
    }

    data_dir = ROOT / "data"
    archive_dir = data_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (archive_dir / f"{TODAY}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # 清理过期归档
    keep = site.get("archive_days", 90)
    cutoff = (datetime.now(TZ) - timedelta(days=keep)).strftime("%Y-%m-%d")
    for f in archive_dir.glob("*.json"):
        if f.stem < cutoff:
            f.unlink()

    # 维护归档索引，便于前端做"往期回顾"
    index = sorted([f.stem for f in archive_dir.glob("*.json")], reverse=True)
    (data_dir / "archive_index.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")

    # 对外 RSS 输出（同一事件只出代表条目）与给 Agent 的说明
    write_feed_xml(shown, site)
    write_llms_txt(site, payload)

    ok = sum(1 for h in HEALTH if not h["error"] and h["count"] > 0)
    print(f"[OK] {TODAY} 共 {len(shown)} 条（{stories} 组多源事件并入 {len(items) - len(shown)} 条），"
          f"源健康 {ok}/{len(HEALTH)}，耗时 {time.time()-t0:.1f}s，"
          f"分类统计 {payload['categories']}")


if __name__ == "__main__":
    main()
