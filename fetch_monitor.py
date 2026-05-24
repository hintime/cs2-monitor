#!/usr/bin/env python3
"""
cs2-monitor - GitHub Actions 后端
定时拉取 ECOSteam 价格，检测异动，生成 monitor_data.json 推送到 gh-pages。
运行环境: GitHub Actions (ubuntu-latest)
依赖: requests, pycryptodome
"""
import os
import sys
import json
import time
import base64
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager

import requests

try:
    from Crypto.Hash import SHA256
    from Crypto.Signature import pkcs1_15
    from Crypto.PublicKey import RSA
except ImportError:
    from Cryptodome.Hash import SHA256
    from Cryptodome.Signature import pkcs1_15
    from Cryptodome.PublicKey import RSA

# -- Config -------------------------------------------------------
ECO_BASE = "https://openapi.ecosteam.cn"
ECO_PARTNER_ID = os.environ.get("ECO_PARTNER_ID", "")
ECO_PRIVATE_KEY_B64 = os.environ.get("ECO_PRIVATE_KEY_B64", "")
GAME_ID = "730"
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "5.0"))
DATA_DIR = Path(__file__).parent / "docs"
DB_PATH = DATA_DIR / "monitor.db"

MONITOR_ITEMS = [
    "AK-47 | Redline (Field-Tested)",
    "AWP | Asiimov (Field-Tested)",
    "AWP | Asiimov (Battle-Scarred)",
    "M4A1-S | Printstream (Field-Tested)",
    "USP-S | Printstream (Field-Tested)",
    "Desert Eagle | Printstream (Field-Tested)",
    "Glock-18 | Gamma Doppler (Factory New)",
    "M4A4 | The Emperor (Factory New)",
    "Desert Eagle | Blaze (Factory New)",
    "AK-47 | Vulcan (Field-Tested)",
    "AK-47 | Bloodsport (Field-Tested)",
    "AK-47 | Fuel Injector (Field-Tested)",
    "M4A1-S | Cyrex (Field-Tested)",
    "SSG 08 | Blood in the Water (Field-Tested)",
    "P250 | Splash (Field-Tested)",
    "P2000 | Ocean Foam (Field-Tested)",
    "Five-SeveN | Case Hardened (Field-Tested)",
    "USP-S | Kill Confirmed (Field-Tested)",
    "AWP | Containment Breach (Field-Tested)",
    "AWP | Wildfire (Field-Tested)",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cs2-monitor")

# -- ECO Sign -----------------------------------------------------
_private_key = None

def load_private_key() -> RSA.RsaKey:
    global _private_key
    if _private_key is None:
        raw = ECO_PRIVATE_KEY_B64
        pem = '-----BEGIN RSA PRIVATE KEY-----\n' + raw + '\n-----END RSA PRIVATE KEY-----'
        _private_key = RSA.import_key(pem)
    return _private_key

def eco_sign(params: dict) -> str:
    sorted_items = sorted(params.items(), key=lambda x: x[0].lower())
    parts = []
    for k, v in sorted_items:
        if v is None or v == "":
            continue
        if isinstance(v, (list, dict)):
            v = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
        parts.append(f"{k}={v}")
    sign_str = "&".join(parts)
    h = SHA256.new(sign_str.encode("utf-8"))
    sig = pkcs1_15.new(load_private_key()).sign(h)
    return base64.b64encode(sig).decode()

def eco_post(endpoint: str, body: dict) -> dict:
    body.setdefault("GameID", GAME_ID)
    body["PartnerId"] = ECO_PARTNER_ID
    body["Timestamp"] = str(int(time.time()))
    body["Sign"] = eco_sign(body)
    for attempt in range(3):
        try:
            resp = requests.post(f"{ECO_BASE}{endpoint}", json=body, timeout=15)
            data = resp.json()
            rc = data.get("ResultCode")
            if str(rc) == "0":
                return data
            logger.warning("ECO error %s: %s", rc, data.get("ResultMsg"))
        except Exception as e:
            logger.warning("ECO attempt %d failed: %s", attempt + 1, e)
            time.sleep(2)
    return {"ResultCode": -1, "ResultMsg": "All retries failed"}

# -- DB -----------------------------------------------------------
@contextmanager
def db_conn():
    """数据库连接上下文管理器"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_conn() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS prices (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                hash_name   TEXT    NOT NULL,
                price       REAL    NOT NULL,
                sell_total  INTEGER,
                recorded_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_prices_nt
                ON prices(hash_name, recorded_at);

            CREATE TABLE IF NOT EXISTS alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                hash_name   TEXT    NOT NULL,
                old_price   REAL    NOT NULL,
                new_price   REAL    NOT NULL,
                change_pct  REAL    NOT NULL,
                direction   TEXT    NOT NULL,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_time
                ON alerts(created_at);
        """)

def insert_prices_batch(records: list[tuple]):
    """批量插入价格记录"""
    with db_conn() as db:
        db.executemany(
            "INSERT INTO prices (hash_name, price, sell_total) VALUES (?,?,?)",
            records,
        )

def get_previous_price(db, hash_name) -> dict | None:
    """获取上一次价格（倒数第二条）"""
    row = db.execute(
        "SELECT * FROM prices WHERE hash_name=? ORDER BY recorded_at DESC LIMIT 1 OFFSET 1",
        (hash_name,),
    ).fetchone()
    return dict(row) if row else None

def insert_alert(db, hash_name, old_price, new_price, change_pct, direction):
    db.execute(
        "INSERT INTO alerts (hash_name, old_price, new_price, change_pct, direction) "
        "VALUES (?,?,?,?,?)",
        (hash_name, old_price, new_price, change_pct, direction),
    )

def get_recent_alerts(db, limit=50) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]

def get_price_history(db, hash_name, hours=24) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = db.execute(
        "SELECT price, recorded_at FROM prices WHERE hash_name=? AND recorded_at>=? ORDER BY recorded_at",
        (hash_name, cutoff),
    ).fetchall()
    return [{"price": r["price"], "time": r["recorded_at"]} for r in rows]

def get_stats(db) -> dict:
    item_count = len(MONITOR_ITEMS)
    alert_count = db.execute(
        "SELECT COUNT(*) FROM alerts WHERE created_at >= date('now')"
    ).fetchone()[0]
    last_poll = db.execute("SELECT MAX(recorded_at) FROM prices").fetchone()[0]
    return {"item_count": item_count, "alert_count": alert_count, "last_poll": last_poll or "N/A"}

def get_today_price_changes(db) -> list[dict]:
    """获取今日所有价格变化"""
    rows = db.execute("""
        SELECT p1.hash_name, p1.price as current_price, p2.price as prev_price
        FROM prices p1
        JOIN prices p2 ON p1.hash_name = p2.hash_name
        WHERE p1.recorded_at = (SELECT MAX(recorded_at) FROM prices WHERE hash_name = p1.hash_name)
        AND p2.recorded_at = (
            SELECT MAX(recorded_at) FROM prices 
            WHERE hash_name = p1.hash_name AND recorded_at < p1.recorded_at
        )
        AND date(p1.recorded_at) = date('now')
    """).fetchall()
    
    changes = []
    for r in rows:
        if r["prev_price"] and r["prev_price"] > 0:
            change_pct = (r["current_price"] - r["prev_price"]) / r["prev_price"] * 100
            changes.append({
                "hash_name": r["hash_name"],
                "change_pct": round(change_pct, 2),
                "direction": "up" if change_pct > 0 else "down" if change_pct < 0 else "flat"
            })
    return changes

# -- Main Logic ---------------------------------------------------
def poll_and_detect():
    if not ECO_PARTNER_ID or not ECO_PRIVATE_KEY_B64:
        logger.error("ECO credentials not set, aborting")
        sys.exit(1)

    logger.info("Fetching ECO price data...")
    result = eco_post("/Api/Market/GetHashNameAndPriceList", {})
    items = result.get("ResultData", [])
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    new_alerts = 0
    prices_snapshot = []
    price_records = []

    if not items:
        logger.warning("Empty ResultData, writing empty snapshot")
    else:
        price_map = {it["HashName"]: it for it in items if "HashName" in it}

        with db_conn() as db:
            for name in MONITOR_ITEMS:
                eco_item = price_map.get(name)
                if not eco_item:
                    continue

                current_price = float(eco_item.get("MarketComprePrice", 0))
                sell_total = int(eco_item.get("SellingTotal", 0))
                if current_price <= 0:
                    continue

                price_records.append((name, current_price, sell_total))

                snap = {
                    "hash_name": name,
                    "price": current_price,
                    "sell_total": sell_total,
                }

                # 检测异动
                prev = get_previous_price(db, name)
                if prev and prev["price"] > 0:
                    change_pct = (current_price - prev["price"]) / prev["price"] * 100
                    snap["change_pct"] = round(change_pct, 2)
                    if abs(change_pct) >= ALERT_THRESHOLD_PCT:
                        direction = "up" if change_pct > 0 else "down"
                        insert_alert(db, name, prev["price"], current_price, round(change_pct, 2), direction)
                        new_alerts += 1
                        emoji = "🔺" if direction == "up" else "🔻"
                        logger.info("%s ALERT %s: ¥%.2f → ¥%.2f (%+.2f%%)",
                                    emoji, name, prev["price"], current_price, change_pct)
                else:
                    snap["change_pct"] = None

                # 获取24小时价格历史
                history = get_price_history(db, name, hours=24)
                snap["history"] = history

                prices_snapshot.append(snap)

            # 批量插入价格记录
            if price_records:
                insert_prices_batch(price_records)

            # 获取统计数据
            alerts = get_recent_alerts(db, limit=100)
            stats = get_stats(db)
            
            # 计算涨跌分布
            changes = get_today_price_changes(db)
            up_count = sum(1 for c in changes if c["direction"] == "up")
            down_count = sum(1 for c in changes if c["direction"] == "down")
            flat_count = len(changes) - up_count - down_count
            
            avg_change = sum(c["change_pct"] for c in changes) / len(changes) if changes else 0
            
            # 找出最大涨跌
            max_up = max((c for c in changes if c["direction"] == "up"), key=lambda x: x["change_pct"], default=None)
            max_down = min((c for c in changes if c["direction"] == "down"), key=lambda x: x["change_pct"], default=None)

            stats.update({
                "up_count": up_count,
                "down_count": down_count,
                "flat_count": flat_count,
                "avg_change_pct": round(avg_change, 2),
                "max_up": max_up,
                "max_down": max_down,
            })

    # 生成 monitor_data.json
    output = {
        "updated_at": now_str,
        "stats": stats,
        "prices": prices_snapshot,
        "alerts": alerts,
        "threshold_pct": ALERT_THRESHOLD_PCT,
        "monitor_interval_min": 10,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "monitor_data.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Written %s (%d bytes)", out_path, out_path.stat().st_size)
    logger.info("Done: %d prices, %d new alerts", len(prices_snapshot), new_alerts)

# -- Entry --------------------------------------------------------
if __name__ == "__main__":
    init_db()
    logger.info("cs2-monitor starting...")
    poll_and_detect()
