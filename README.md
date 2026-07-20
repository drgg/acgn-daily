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

## 目录结构

```
├── index.html                  # 站点前端（单文件，无依赖）
├── feeds.yml                   # 信息源与站点配置（核心资产）
├── requirements.txt            # Python 依赖（Actions 用它做 pip 缓存）
├── feed.xml                    # 聚合结果的 RSS 输出（供他人订阅本站）
├── scripts/aggregate.py        # 聚合脚本：并发抓取→分类→去重→输出
├── data/
│   ├── latest.json             # 今日数据（前端读取，含每源健康状况）
│   ├── archive/YYYY-MM-DD.json # 每日归档（默认保留90天）
│   └── archive_index.json      # 归档索引（前端"往期"下拉框）
└── .github/workflows/daily.yml # 每日定时任务
```

## 合规提示

本站仅聚合标题、链接与不超过 300 字的摘要（默认折叠，点击展开），并明确标注出处、点击跳转原文，不转载全文、不搬运图片。标题与摘要的中日英译文由机器翻译生成、仅供参考，条目上有"机翻"标记并可悬停查看原文。抓取频率为每源每日 2 次并设置了 UA 标识与请求间隔。如需商用或扩大抓取范围，请先阅读各源的服务条款与 robots.txt。
