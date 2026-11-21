#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSSHub 每日采集 + 统一处理脚本

用法:
    python collector.py run        采集 + 生成当日日报（推荐，配合定时任务）
    python collector.py collect    只采集，写入本地 SQLite
    python collector.py process    只对今天已采集的数据生成日报

依赖:
    pip install feedparser PyYAML
    可选（启用 LLM 智能日报）: pip install anthropic
"""

import argparse
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import feedparser
import yaml

# 统一用 UTF-8 输出，避免 Windows 控制台中文乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def apply_proxy(settings: dict) -> None:
    """把 config 里的 proxy 写入环境变量，让 feedparser/urllib 走代理。"""
    proxy = (settings.get("proxy") or "").strip()
    if proxy:
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            os.environ[var] = proxy
        print(f"[proxy] 使用代理 {proxy}")


def build_proxy_handlers(settings: dict):
    """构造 urllib 代理 handler，让 feedparser 显式走代理（比环境变量更可靠）。"""
    proxy = (settings.get("proxy") or "").strip()
    if not proxy:
        return []
    return [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"[错误] 找不到配置文件: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def entry_time(entry):
    """把 feedparser 的 struct_time 转成本地 datetime；解析不了返回 None。"""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    try:
        return datetime.fromtimestamp(time.mktime(t))
    except (ValueError, OSError, OverflowError):
        return None


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT UNIQUE,
            title TEXT,
            category TEXT,
            feed_url TEXT,
            published TEXT,
            collect_date TEXT,
            collected_at TEXT
        )
        """
    )
    return conn


def collect(cfg: dict) -> None:
    feeds = cfg.get("feeds") or {}
    settings = cfg.get("settings") or {}
    db_path = BASE_DIR / settings.get("db_path", "data/items.db")
    lookback_hours = int(settings.get("lookback_hours", 24))

    if not feeds:
        print("[错误] config.yaml 里没有配置任何 feeds。", file=sys.stderr)
        sys.exit(1)

    now = datetime.now()
    cutoff = now - timedelta(hours=lookback_hours)
    conn = init_db(db_path)
    total_new = 0
    handlers = build_proxy_handlers(settings)

    for category, urls in feeds.items():
        for url in urls:
            try:
                parsed = feedparser.parse(url, agent=USER_AGENT, handlers=handlers)
            except Exception as e:  # 单个源失败不影响其它源
                print(f"[错误] 拉取异常 {url}: {e}")
                continue

            status = parsed.get("status")
            if parsed.get("bozo"):
                print(f"[错误] {url} 解析失败 (HTTP {status}): {parsed.get('bozo_exception')}")

            if not parsed.entries:
                print(f"[提示] {url} 无条目 (HTTP {status})")
                continue

            for entry in parsed.entries:
                link = entry.get("link") or entry.get("id")
                if not link:
                    continue
                t = entry_time(entry)
                if lookback_hours > 0 and t is not None and t < cutoff:
                    continue  # 太旧，跳过
                title = (entry.get("title") or "").strip()
                published = t.isoformat() if t else None
                try:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO items "
                        "(link, title, category, feed_url, published, collect_date, collected_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            link,
                            title,
                            category,
                            url,
                            published,
                            date.today().isoformat(),
                            now.isoformat(),
                        ),
                    )
                    total_new += cur.rowcount
                except sqlite3.Error as e:
                    print(f"[警告] 入库失败 {link}: {e}")

    conn.commit()
    conn.close()
    print(f"[collect] 完成，新增 {total_new} 条。")


def query_today(conn: sqlite3.Connection):
    """取出今天采集到的条目，按分类分组返回 {category: [(title, link, published), ...]}"""
    rows = conn.execute(
        "SELECT category, title, link, published FROM items "
        "WHERE collect_date = ? ORDER BY category, published DESC",
        (date.today().isoformat(),),
    ).fetchall()

    grouped: dict = {}
    for category, title, link, published in rows:
        grouped.setdefault(category, []).append((title, link, published))
    return grouped


def build_payload(grouped: dict, cap_per_cat: int = 100) -> str:
    parts = []
    for cat, items in grouped.items():
        parts.append(f"【{cat}】")
        for title, link, _published in items[:cap_per_cat]:
            parts.append(f"- {title}  {link}")
    return "\n".join(parts)


def llm_digest(grouped: dict, settings: dict, date_str: str):
    """调用 Claude 生成智能日报；失败返回 None。"""
    model = settings.get("llm_model", "claude-opus-5-5")
    try:
        import anthropic
    except ImportError:
        print("[提示] 未安装 anthropic，退回本地汇总（pip install anthropic）。")
        return None

    client = anthropic.Anthropic()  # 从环境变量 ANTHROPIC_API_KEY 读取密钥
    payload = build_payload(grouped)

    prompt = (
        f"以下是 {date_str} 一天内收集到的中文资讯，按分类列出。请帮我统一处理，输出一份 Markdown 日报：\n\n"
        "要求：\n"
        "1. 每个分类挑出最值得关注的 3~8 条，用一行中文概括每条，并保留原链接。\n"
        "2. 最后给一个「今日重点」，列出 3~5 条全天最值得看的信息。\n"
        "3. 直接输出 Markdown 正文，不要输出分析过程，不要用代码块包裹。\n\n"
        f"资讯列表：\n{payload}"
    )

    resp = client.messages.create(
        model=model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system="你是中文信息摘要助手，输出简洁、准确、不夸大。",
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return None


def local_digest(grouped: dict, settings: dict, date_str: str) -> str:
    top_n = int(settings.get("top_n_per_category", 20))
    lines = [f"# 每日信息摘要 {date_str}", ""]
    lines.append("> 由 RSSHub 采集 + 本地汇总生成（未启用 LLM 智能摘要）")
    lines.append("")
    for cat, items in grouped.items():
        lines.append(f"## {cat}（{len(items)} 条）")
        lines.append("")
        for title, link, published in items[:top_n]:
            t = (published or "")[:16].replace("T", " ")
            suffix = f" - {t}" if t else ""
            lines.append(f"- [{title}]({link}){suffix}")
        lines.append("")
    return "\n".join(lines)


def process(cfg: dict) -> None:
    settings = cfg.get("settings") or {}
    db_path = BASE_DIR / settings.get("db_path", "data/items.db")
    if not db_path.exists():
        print("[错误] 数据库不存在，请先运行 collect。", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    grouped = query_today(conn)
    conn.close()

    if not grouped:
        print("[process] 今天还没有采集到数据。")
        return

    date_str = date.today().isoformat()
    digest = None
    llm_mode = settings.get("llm_mode", "auto")

    if llm_mode != "off" and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            digest = llm_digest(grouped, settings, date_str)
        except Exception as e:
            print(f"[警告] LLM 摘要失败，退回本地汇总: {e}")

    if digest is None:
        digest = local_digest(grouped, settings, date_str)

    digest_dir = BASE_DIR / settings.get("digest_dir", "digest")
    digest_dir.mkdir(parents=True, exist_ok=True)
    out = digest_dir / f"{date_str}.md"
    out.write_text(digest, encoding="utf-8")
    print(f"[process] 日报已生成: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="RSSHub 每日采集 + 统一处理")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "collect", "process"],
        help="run=采集+生成日报（默认）；collect=只采集；process=只生成日报",
    )
    args = parser.parse_args()

    cfg = load_config()
    apply_proxy(cfg.get("settings") or {})
    if args.command in ("run", "collect"):
        collect(cfg)
    if args.command in ("run", "process"):
        process(cfg)


if __name__ == "__main__":
    main()
