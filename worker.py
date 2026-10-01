#!/usr/bin/env python3
"""Headless cloud worker for Xueqiu watchlist changes."""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tracker import db_init, fetch_watchlist, store_snapshot  # noqa: E402

LOG = logging.getLogger("xueqiu-cloud")
STOP = False


@dataclass(frozen=True)
class Target:
    user_id: str
    name: str
    pid: str


def parse_targets() -> list[Target]:
    raw = os.getenv("XUEQIU_USERS", "")
    default_pid = os.getenv("XUEQIU_PID", "-5").strip() or "-5"
    targets = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|", 2)]
        uid = parts[0]
        if not uid.isdigit():
            raise ValueError(f"XUEQIU_USERS 中用户 ID 无效：{uid!r}。格式为 user_id|备注名|pid")
        name = parts[1] if len(parts) > 1 and parts[1] else uid
        pid = parts[2] if len(parts) > 2 and parts[2] else default_pid
        targets.append(Target(uid, name, pid))
    if not targets:
        raise ValueError("请在 XUEQIU_USERS 中至少配置一个用户，每行格式：user_id|备注名|pid")
    return targets


def push_bark(session: requests.Session, push_url: str, title: str, body: str) -> None:
    parsed = urlsplit(push_url.strip())
    pieces = [unquote(piece) for piece in parsed.path.split("/") if piece]
    if parsed.scheme not in ("https", "http") or not parsed.netloc or not pieces:
        raise ValueError("BARK_PUSH_URL 格式无效；请填写 Bark 完整推送地址")
    server = f"{parsed.scheme}://{parsed.netloc}"
    device_key = pieces[0]
    response = session.post(
        server + "/push",
        json={"device_key": device_key, "title": title, "body": body, "group": "雪球自选变化"},
        timeout=15,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        return
    if payload.get("code") not in (None, 200):
        raise RuntimeError(payload.get("message") or payload.get("msg") or f"Bark 返回 code={payload.get('code')}")


def notify_changes(session: requests.Session, target: Target, changes: list[dict], push_url: str) -> None:
    for change in changes:
        action = "新增" if change["action"] == "added" else "移除"
        item = change.get("item") or {}
        name = change.get("name") or ""
        price = item.get("current") or item.get("price")
        percent = item.get("percent")
        if percent in (None, ""):
            percent = item.get("percentage")
        price_text = f"{price:.2f}" if isinstance(price, (int, float)) else (str(price) if price not in (None, "") else "未知")
        if isinstance(percent, (int, float)):
            percent_text = f"{percent:+.2f}%"
        else:
            percent_text = str(percent) if percent not in (None, "") else "未知"
        body = f"{target.name}：{action} {name}（{change['symbol']}）\n现价 {price_text}｜涨跌 {percent_text}"
        try:
            push_bark(session, push_url, "雪球自选股变化", body)
            LOG.info("Bark 已推送：%s %s %s", target.name, action, change["symbol"])
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            LOG.error("Bark 推送失败：%s", exc)


def handle_signal(_signum, _frame) -> None:
    global STOP
    STOP = True


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    targets = parse_targets()
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    if interval < 30:
        raise ValueError("POLL_INTERVAL_SECONDS 不能低于 30 秒")
    push_url = os.getenv("BARK_PUSH_URL", "").strip()
    if not push_url:
        raise ValueError("请配置 BARK_PUSH_URL（Bark 完整推送地址）")
    database = Path(os.getenv("DATABASE_PATH", "/data/xueqiu_watchlist.sqlite3"))
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(database), timeout=30)
    db_init(conn)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; XueqiuWatchlistCloud/1.0)", "Referer": "https://xueqiu.com/"})
    cookie = os.getenv("XUEQIU_COOKIE", "").strip()
    run_once = os.getenv("RUN_ONCE", "0").strip() == "1"
    if cookie:
        session.headers["Cookie"] = cookie
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    if run_once:
        LOG.info("GitHub Actions 单次采集启动：%d 个用户，数据库 %s", len(targets), database)
    else:
        LOG.info("云端监测启动：%d 个用户，轮询间隔 %d 秒，数据库 %s", len(targets), interval, database)
    try:
        while not STOP:
            cycle_start = time.monotonic()
            for target in targets:
                if STOP:
                    break
                try:
                    stocks, _payload = fetch_watchlist(session, target.user_id, target.pid, 1000, 20)
                    sid, changes = store_snapshot(conn, target.user_id, target.pid, stocks, _payload)
                    LOG.info("%s (%s) 快照 #%d：%d 只，变化 %d 条", target.name, target.user_id, sid, len(stocks), len(changes))
                    if changes:
                        notify_changes(session, target, changes, push_url)
                except (requests.RequestException, ValueError, RuntimeError, sqlite3.Error) as exc:
                    LOG.error("%s (%s) 采集失败：%s", target.name, target.user_id, exc)
            if run_once:
                break
            elapsed = time.monotonic() - cycle_start
            wait = max(0.0, interval - elapsed)
            if wait and not STOP:
                time.sleep(wait)
    finally:
        conn.close()
        session.close()
        LOG.info("云端监测已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
