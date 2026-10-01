#!/usr/bin/env python3
"""Poll a Xueqiu user's watchlist and store snapshots/diffs in SQLite."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

API = "https://stock.xueqiu.com/v5/stock/portfolio/stock/list.json"
QUOTE_API = "https://stock.xueqiu.com/v5/stock/batch/quote.json"
HOME = "https://xueqiu.com/"
USER_SEARCH = "https://xueqiu.com/users/search.json"
USER_SEARCH_FALLBACK = "https://xueqiu.com/search/user.json"
DEFAULT_DB = Path(r"D:\xueqiu_watchlist_tracker\xueqiu_watchlist.sqlite3")
OLD_DB = Path(__file__).with_name("xueqiu_watchlist.sqlite3")
STOP = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_init(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS snapshots (
      id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, pid TEXT NOT NULL,
      captured_at TEXT NOT NULL, item_count INTEGER NOT NULL, raw_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_snapshots_user_pid ON snapshots(user_id,pid,captured_at);
    CREATE TABLE IF NOT EXISTS watchlist_items (
      snapshot_id INTEGER NOT NULL REFERENCES snapshots(id), symbol TEXT NOT NULL,
      name TEXT, market TEXT, raw_json TEXT NOT NULL,
      PRIMARY KEY(snapshot_id,symbol)
    );
    CREATE TABLE IF NOT EXISTS changes (
      id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, pid TEXT NOT NULL,
      observed_at TEXT NOT NULL, action TEXT NOT NULL, symbol TEXT NOT NULL,
      name TEXT, details_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_changes_user_pid ON changes(user_id,pid,observed_at);
    CREATE TABLE IF NOT EXISTS tracked_users (
      user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tracker_settings (
      setting TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    """)


def fetch_watchlist(session: requests.Session, user_id: str, pid: str, size: int, timeout: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = {"uid": user_id, "pid": pid, "category": 1, "size": size}
    # Xueqiu may reject a cold API request until a site cookie has been issued.
    if not session.cookies:
        session.get(HOME, timeout=timeout)
    response = session.get(API, params=params, timeout=timeout)
    if response.status_code >= 400:
        detail = response.text[:800].replace("\n", " ")
        raise RuntimeError(f"雪球 HTTP {response.status_code}；响应：{detail}。若提示 cookie invalid，请使用本人有权访问的有效 Cookie；若提示权限/参数错误，请确认该用户的自选列表对当前账号可见且 pid 正确。")
    payload = response.json()
    data = payload.get("data") or {}
    stocks = data.get("stocks")
    if not isinstance(stocks, list):
        raise RuntimeError(f"雪球返回结构异常或需要登录：{json.dumps(payload, ensure_ascii=False)[:800]}")
    missing = [s for s in stocks if not any(s.get(k) not in (None, "") for k in ("current", "price"))]
    symbols = [str(s.get("symbol") or "").strip() for s in missing if s.get("symbol")]
    if symbols:
        try:
            quote_response = session.get(QUOTE_API, params={"symbol": ",".join(symbols)}, timeout=timeout)
            if quote_response.ok:
                quote_data = quote_response.json().get("data") or {}
                quotes = {}
                for entry in quote_data.get("items") or []:
                    quote = entry.get("quote") or entry
                    quote_symbol = str(quote.get("symbol") or "").strip()
                    if quote_symbol:
                        quotes[quote_symbol] = quote
                for stock in missing:
                    quote = quotes.get(str(stock.get("symbol") or "").strip(), {})
                    for key in ("current", "price", "percent", "percentage", "chg", "last_close"):
                        if stock.get(key) in (None, "") and quote.get(key) not in (None, ""):
                            stock[key] = quote[key]
        except (requests.RequestException, ValueError):
            pass
    return stocks, payload


def search_users(session: requests.Session, keyword: str, timeout: int = 20) -> list[dict[str, Any]]:
    if not session.cookies:
        session.get(HOME, timeout=timeout)
    errors: list[str] = []
    for url in (USER_SEARCH, USER_SEARCH_FALLBACK):
        try:
            response = session.get(url, params={"q": keyword, "count": 20, "page": 1}, timeout=timeout)
            if response.status_code >= 400:
                if response.status_code == 404 and "text/html" in response.headers.get("Content-Type", "").lower():
                    errors.append(f"HTTP 404：此备用搜索地址已失效（{url}）")
                    continue
                try:
                    error_payload = response.json()
                    reason = error_payload.get("error_description") or error_payload.get("error") or response.text[:180]
                    code = error_payload.get("error_code")
                    suffix = f"（错误码 {code}）" if code else ""
                except ValueError:
                    reason = response.text[:180]
                    suffix = ""
                errors.append(f"HTTP {response.status_code}{suffix}: {reason}")
                continue
            payload = response.json()
            data = payload.get("data") or {}
            users = payload.get("users") or data.get("users") or data.get("list") or payload.get("list") or []
            if isinstance(users, dict):
                users = users.get("list") or users.get("users") or []
            if isinstance(users, list):
                matches = [u for u in users if isinstance(u, dict) and (u.get("id") or u.get("user_id"))]
                if matches:
                    return matches
                if any(key in payload for key in ("users", "list")) or any(key in data for key in ("users", "list")):
                    return []
                errors.append(f"{url}: 接口返回成功，但未找到可识别的用户列表；{json.dumps(payload, ensure_ascii=False)[:300]}")
            else:
                errors.append(f"{url}: 返回格式异常；{json.dumps(payload, ensure_ascii=False)[:300]}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("雪球用户搜索失败（已尝试两个接口）。请确认 Cookie/访问权限；详情：" + " | ".join(errors))


def store_snapshot(conn: sqlite3.Connection, user_id: str, pid: str, stocks: list[dict[str, Any]], payload: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    captured = utc_now()
    # A first snapshot establishes a baseline and is not reported as additions.
    previous_rows = conn.execute("""
      SELECT symbol,name,market,raw_json FROM watchlist_items
      WHERE snapshot_id=(SELECT id FROM snapshots WHERE user_id=? AND pid=? ORDER BY id DESC LIMIT 1)
    """, (user_id, pid)).fetchall()
    previous = {}
    for row in previous_rows:
        try:
            item = json.loads(row[3])
        except (TypeError, ValueError):
            item = {}
        previous[row[0]] = {**item, "symbol": row[0], "name": row[1], "market": row[2]}
    current: dict[str, dict[str, Any]] = {}
    for item in stocks:
        symbol = str(item.get("symbol") or item.get("code") or "").strip()
        if symbol:
            current[symbol] = item
    has_previous = conn.execute("SELECT 1 FROM snapshots WHERE user_id=? AND pid=? LIMIT 1", (user_id, pid)).fetchone() is not None
    changes: list[dict[str, Any]] = []
    if has_previous:
        for symbol in sorted(current.keys() - previous.keys()):
            changes.append({"action": "added", "symbol": symbol, "name": current[symbol].get("name"), "item": current[symbol]})
        for symbol in sorted(previous.keys() - current.keys()):
            changes.append({"action": "removed", "symbol": symbol, "name": previous[symbol].get("name"), "item": previous[symbol]})
    with conn:
        cur = conn.execute("INSERT INTO snapshots(user_id,pid,captured_at,item_count,raw_json) VALUES(?,?,?,?,?)",
                           (user_id, pid, captured, len(current), json.dumps(payload, ensure_ascii=False)))
        sid = int(cur.lastrowid)
        conn.executemany("INSERT INTO watchlist_items(snapshot_id,symbol,name,market,raw_json) VALUES(?,?,?,?,?)",
                         [(sid, s, str(v.get("name") or ""), str(v.get("exchange") or v.get("market") or ""), json.dumps(v, ensure_ascii=False)) for s, v in current.items()])
        conn.executemany("INSERT INTO changes(user_id,pid,observed_at,action,symbol,name,details_json) VALUES(?,?,?,?,?,?,?)",
                         [(user_id, pid, captured, c["action"], c["symbol"], c.get("name"), json.dumps(c["item"], ensure_ascii=False)) for c in changes])
    return sid, changes


def on_signal(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def main() -> int:
    parser = argparse.ArgumentParser(description="定期记录雪球用户自选股快照及变化（只读）")
    parser.add_argument("--user-id", required=True, help="雪球用户数字 ID")
    parser.add_argument("--pid", default="-5", help="自选分组 ID；常见 -5 为沪深，使用目标用户公开的分组 ID")
    parser.add_argument("--interval", type=int, default=3600, help="轮询间隔秒数，默认 3600；建议使用 300 秒以上")
    parser.add_argument("--size", type=int, default=1000, help="单次最多读取条数")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--once", action="store_true", help="只抓取一次后退出")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 文件路径")
    parser.add_argument("--cookie", default=os.getenv("XUEQIU_COOKIE", ""), help="可选；也可通过 XUEQIU_COOKIE 环境变量传入")
    args = parser.parse_args()
    if args.interval < 30 and not args.once:
        parser.error("轮询间隔不能低于 30 秒")
    if args.size < 1 or args.size > 5000:
        parser.error("--size 必须在 1 到 5000 之间")
    db_path = Path(args.db).expanduser()
    if db_path == DEFAULT_DB and not db_path.exists() and OLD_DB.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{OLD_DB.as_posix()}?mode=ro", uri=True, timeout=30) as source:
            with sqlite3.connect(str(db_path), timeout=30) as destination:
                source.backup(destination)
    db_path.resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    db_init(conn)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; WatchlistChangeTracker/1.0)", "Referer": "https://xueqiu.com/"})
    if args.cookie:
        session.headers["Cookie"] = args.cookie
    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_signal)
    print(f"开始监测 user_id={args.user_id}, pid={args.pid}; 数据库={db_path}")
    try:
        while not STOP:
            try:
                stocks, payload = fetch_watchlist(session, args.user_id, args.pid, args.size, args.timeout)
                sid, changes = store_snapshot(conn, args.user_id, args.pid, stocks, payload)
                print(f"{utc_now()} 快照 #{sid}: {len(stocks)} 只；变化 {len(changes)} 条")
                for c in changes:
                    print(f"  {c['action']:7} {c['symbol']} {c.get('name') or ''}")
            except (requests.RequestException, ValueError, RuntimeError, sqlite3.Error) as exc:
                print(f"{utc_now()} 采集失败：{exc}", file=sys.stderr)
                if args.once:
                    return 1
            if args.once:
                break
            # Interruptible wait.
            for _ in range(args.interval):
                if STOP:
                    break
                time.sleep(1)
    finally:
        conn.close()
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
