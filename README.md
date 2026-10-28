# ACGN 日報 · 每日自动更新的二次元资讯站

一个零服务器成本、每日自动更新的 ACGN（动画 / 漫画 / 游戏 / 轻小说）资讯聚合站。GitHub Actions 定时抓取公开 RSS 与 API，生成 JSON 数据，静态页面直接渲染，托管在 GitHub Pages 上。

## 快速部署（约 10 分钟）

第一步，把本项目推送到你的 GitHub 仓库（公开仓库可免费使用 Actions 与 Pages）。

第二步，进入仓库 Settings → Pages，将 Source 设为 "GitHub Actions"。

第三步，进入 Actions 页签，手动运行一次 "每日聚合 ACGN 资讯" 工作流（workflow_dispatch），确认 `data/latest.json` 生成并且站点可访问。

之后无需任何操作：工作流会在每天北京时间 6:00 与 18:00 自动抓取、提交数据并重新发布站点。

## 本地调试

```bash
pip install -r requirements.txt
python scripts/aggregate.py        # 生成 data/latest.json 与 feed.xml
python -m http.server 8000         # 打开 http://localhost:8000 预览
```

脚本并发抓取全部源（约 30-40 秒跑完），失败源会在冷却后自动串行补捞一轮；每源抓取健康状况写入 `data/latest.json` 的 `sources` 字段，巡检时看它即可。本地网络对海外站点不稳时，可把 `feeds.yml` 中 `site.concurrency` 降为 1-2。

未生成数据时，页面会自动展示内置示例数据（带"示例数据"标记），方便先看效果再接数据。

## 自定义

信息源全部集中在 `feeds.yml`：增删 RSS 只需加一段配置；结构化 API（AniList / Jikan / Bangumi / MangaDex）通过 `apis` 段开关；`classifier` 段的关键词表决定综合类条目如何自动归入四大领域；`translate` 段控制中日英机翻（免费接口，无需密钥，结果缓存在 `data/i18n_cache.json`）；`site` 段控制条数上限与归档保留天数。

前端是单文件 `index.html`，无构建步骤，改完即生效。四个领域的频道色定义在 CSS 变量 `--a / --c / --g / --n` 中。

## 同题归并与热度

不同来源报道同一件事时（比如 4Gamer 和 AUTOMATON 都发了某款游戏停服），页面只出一张卡，下面列出「另有 N 家报道」。头条改为当天热度最高的多源事件；没有多源事件时，仍按源权重选头条。做法借鉴了 [AIHOT](https://github.com/KKKKhazix/AIHOT) 的归组和热度规则，但本站不调模型：

- **判断是不是同一事件**：比较标题的字符二元组重合度，跨语言时用机翻后的中文标题比；两边标题里都有作品名（「」『』《》内）时，作品名也必须对上；两篇发布时间相差超过 72 小时的不算。合集、榜单、「在哪看」这类条目不参与。
- **热度**：每个来源只算一次，按发布时间以 24 小时半衰期衰减后相加。
- **数据**：代表条目带 `story`（来源数、热度、同组条目 id），其余条目带 `dup_of`，仍保留在 `items` 里，所以早晚两次运行合并当日归档时不会丢条目。

阈值在 `feeds.yml` 的 `cluster` 段，用 `eval/cluster_gold.jsonl` 里 269 对人工标注的报道校准（2026-10-28：查准 0.91、查全 0.69）。宁可漏并也别错并，因为错并会把另一条新闻藏起来。改阈值后运行：

```bash
python scripts/eval_cluster.py          # 当前参数的查准率、查全率，以及每条判错的样本
python scripts/eval_cluster.py --grid   # 在开发集上扫阈值组合，再看留出集
```

标注由 Claude 根据近 30 天的归档起草，建议抽查后再用来调参；新发现的判错样本可以直接追加到 jsonl 里。

## 给 AI Agent 的说明

每次运行会生成 `llms.txt`（[llmstxt.org](https://llmstxt.org) 约定），列出数据文件、字段含义、信息源和使用约定，由 `feeds.yml` 生成，增删源后会自动跟着更新。

## 目录结构

```
├── index.html                  # 站点前端（单文件，无依赖）
├── feeds.yml                   # 信息源与站点配置（核心资产）
├── requirements.txt            # Python 依赖（Actions 用它做 pip 缓存）
├── feed.xml                    # 聚合结果的 RSS 输出（供他人订阅本站）
├── llms.txt                    # 给 AI Agent 的数据说明（每次运行生成）
├── scripts/aggregate.py        # 聚合脚本：并发抓取→分类→去重→翻译→同题归并→输出
├── scripts/eval_cluster.py     # 同题归并的阈值校准
├── eval/cluster_gold.jsonl     # 人工标注的报道对（是否同一事件）
├── data/
│   ├── latest.json             # 今日数据（前端读取，含每源健康状况）
│   ├── archive/YYYY-MM-DD.json # 每日归档（默认保留90天）
│   └── archive_index.json      # 归档索引（前端"往期"下拉框）
└── .github/workflows/daily.yml # 每日定时任务
```

## 合规提示

本站仅聚合标题、链接与不超过 300 字的摘要（默认折叠，点击展开），并明确标注出处、点击跳转原文，不转载全文、不搬运图片。标题与摘要的中日英译文由机器翻译生成、仅供参考，条目上有"机翻"标记并可悬停查看原文。抓取频率为每源每日 2 次并设置了 UA 标识与请求间隔。如需商用或扩大抓取范围，请先阅读各源的服务条款与 robots.txt。
