#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACGN 每日资讯聚合器
==================
每日由 GitHub Actions 定时运行：
  1. 读取 feeds.yml 中的 RSS 源与结构化 API 适配器
  2. 并发抓取 → 规范化 → 关键词分类 → 标题指纹去重 → 打分排序
  3. 输出 data/latest.json（前端读取）、data/archive/YYYY-MM-DD.json（历史归档）
     与 feed.xml（对外 RSS 输出，让别人也能订阅本站）

依赖: pip install requests feedparser pyyaml
"""

import hashlib
import html
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
        "summary": clean_text(summary, 160),
    }


# ----------------------------------------------------------------------
# RSS 抓取
# ----------------------------------------------------------------------
def fetch_rss(feed_cfg, limit):
    items = []
    try:
        resp = SESSION.get(feed_cfg["url"], timeout=TIMEOUT)
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
            summary=(a.get("synopsis") or "")[:160],
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

    # 排序：权重 desc → 发布时间 desc（元组一次排序即可）；截断总量
    items.sort(key=lambda x: (x["weight"], x["published"]), reverse=True)
    items = items[:max_total]

    payload = {
        "date": TODAY,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "count": len(items),
        "categories": {c: sum(1 for i in items if i["category"] == c)
                       for c in ["anime", "comic", "game", "novel", "general"]},
        "sources": HEALTH,   # 每源健康状况，供巡检
        "items": items,
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

    # 对外 RSS 输出
    write_feed_xml(items, site)

    ok = sum(1 for h in HEALTH if not h["error"] and h["count"] > 0)
    print(f"[OK] {TODAY} 共 {len(items)} 条，"
          f"源健康 {ok}/{len(HEALTH)}，耗时 {time.time()-t0:.1f}s，"
          f"分类统计 {payload['categories']}")


if __name__ == "__main__":
    main()
