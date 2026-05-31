#!/usr/bin/env python3
"""
cs2-monitor - GitHub Actions backend
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
from functools import lru_cache

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
            CREATE INDEX IF NOT EXISTS idx_prices_time
                ON prices(recorded_at);

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

def get_previous_prices(db, hash_names: list) -> dict:
    """批量获取上一次价格，使用单次查询"""
    placeholders = ','.join('?' * len(hash_names))
    rows = db.execute(f"""
        SELECT hash_name, price, recorded_at FROM prices
        WHERE hash_name IN ({placeholders})
        AND recorded_at < (SELECT MAX(recorded_at) FROM prices)
        ORDER BY recorded_at DESC
    """, hash_names).fetchall()
    
    result = {}
    for row in rows:
        if row["hash_name"] not in result:
            result[row["hash_name"]] = dict(row)
    return result

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

def get_price_histories(db, hash_names: list, hours=24) -> dict:
    """批量获取价格历史，返回 {hash_name: [price1, price2, ...]}"""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    placeholders = ','.join('?' * len(hash_names))
    rows = db.execute(f"""
        SELECT hash_name, price FROM prices 
        WHERE hash_name IN ({placeholders}) AND recorded_at>=?
        ORDER BY hash_name, recorded_at
    """, hash_names + [cutoff]).fetchall()
    
    result = {name: [] for name in hash_names}
    for row in rows:
        result[row["hash_name"]].append(round(row["price"], 2))
    return result

def get_stats(db) -> dict:
    item_count = len(MONITOR_ITEMS)
    alert_count = db.execute(
        "SELECT COUNT(*) FROM alerts WHERE created_at >= date('now')"
    ).fetchone()[0]
    last_poll = db.execute("SELECT MAX(recorded_at) FROM prices").fetchone()[0]
    return {"item_count": item_count, "alert_count": alert_count, "last_poll": last_poll or "N/A"}

def get_today_changes_batch(db) -> list[dict]:
    """高效获取今日所有价格变化，单次查询"""
    rows = db.execute("""
        WITH latest AS (
            SELECT hash_name, price, recorded_at,
                   ROW_NUMBER() OVER (PARTITION BY hash_name ORDER BY recorded_at DESC) as rn
            FROM prices
            WHERE date(recorded_at) = date('now')
        ),
        previous AS (
            SELECT hash_name, price, recorded_at,
                   ROW_NUMBER() OVER (PARTITION BY hash_name ORDER BY recorded_at DESC) as rn
            FROM prices
            WHERE date(recorded_at) = date('now')
        )
        SELECT l.hash_name, l.price as current_price, p.price as prev_price
        FROM latest l
        JOIN previous p ON l.hash_name = p.hash_name
        WHERE l.rn = 1 AND p.rn = 2
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

def get_price_stats(db, hash_name: str) -> dict:
    """获取指定饰品的价格统计信息"""
    row = db.execute("""
        SELECT 
            MIN(price) as min_price,
            MAX(price) as max_price,
            AVG(price) as avg_price,
            COUNT(*) as data_points
        FROM prices 
        WHERE hash_name = ? AND recorded_at >= datetime('now', '-7 days')
    """, (hash_name,)).fetchone()
    
    if not row or row["data_points"] == 0:
        return {}
    
    return {
        "min_7d": round(row["min_price"], 2),
        "max_7d": round(row["max_price"], 2),
        "avg_7d": round(row["avg_price"], 2),
        "data_points": row["data_points"]
    }

def get_volatility_ranking(db, limit=10) -> list[dict]:
    """获取波动率排行（标准差/均值）"""
    rows = db.execute("""
        SELECT 
            hash_name,
            AVG(price) as avg_price,
            SQRT(AVG(price*price) - AVG(price)*AVG(price)) as std_dev
        FROM prices 
        WHERE recorded_at >= datetime('now', '-24 hours')
        GROUP BY hash_name
        HAVING COUNT(*) >= 3
        ORDER BY (std_dev / avg_price) DESC
        LIMIT ?
    """, (limit,)).fetchall()
    
    result = []
    for r in rows:
        if r["avg_price"] and r["avg_price"] > 0:
            volatility = (r["std_dev"] / r["avg_price"]) * 100 if r["std_dev"] else 0
            result.append({
                "hash_name": r["hash_name"],
                "volatility_pct": round(volatility, 2),
                "avg_price": round(r["avg_price"], 2)
            })
    return result

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
        found_items = []
        
        for name in MONITOR_ITEMS:
            eco_item = price_map.get(name)
            if not eco_item:
                continue
            current_price = float(eco_item.get("MarketComprePrice", 0))
            if current_price <= 0:
                continue
            sell_total = int(eco_item.get("SellingTotal", 0))
            found_items.append(name)
            price_records.append((name, current_price, sell_total))

        with db_conn() as db:
            # 批量获取历史数据（减少查询次数）
            prev_prices = get_previous_prices(db, found_items)
            price_histories = get_price_histories(db, found_items, hours=24)
            
            for name in found_items:
                eco_item = price_map[name]
                current_price = float(eco_item.get("MarketComprePrice", 0))
                sell_total = int(eco_item.get("SellingTotal", 0))
                
                snap = {
                    "hash_name": name,
                    "price": round(current_price, 2),
                    "sell_total": sell_total,
                }

                # 检测异动
                prev = prev_prices.get(name)
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

                # 只保留价格数组，减少输出体积
                history = price_histories.get(name, [])
                if history:
                    history.append(round(current_price, 2))  # 包含当前价格
                snap["history"] = history

                prices_snapshot.append(snap)

            # 批量插入价格记录
            if price_records:
                insert_prices_batch(price_records)

            # 获取统计数据
            alerts = get_recent_alerts(db, limit=100)
            stats = get_stats(db)
            
            # 计算涨跌分布
            changes = get_today_changes_batch(db)
            up_count = sum(1 for c in changes if c["direction"] == "up")
            down_count = sum(1 for c in changes if c["direction"] == "down")
            flat_count = len(changes) - up_count - down_count
            
            avg_change = sum(c["change_pct"] for c in changes) / len(changes) if changes else 0
            
            # 找出最大涨跌
            max_up = max((c for c in changes if c["direction"] == "up"), 
                        key=lambda x: x["change_pct"], default=None)
            max_down = min((c for c in changes if c["direction"] == "down"), 
                          key=lambda x: x["change_pct"], default=None)
            
            # 获取波动率排行
            volatility_ranking = get_volatility_ranking(db, limit=5)
            
            # 为每个价格添加统计信息
            for snap in prices_snapshot:
                stats_info = get_price_stats(db, snap["hash_name"])
                if stats_info:
                    snap["stats_7d"] = stats_info

            stats.update({
                "up_count": up_count,
                "down_count": down_count,
                "flat_count": flat_count,
                "avg_change_pct": round(avg_change, 2),
                "max_up": max_up,
                "max_down": max_down,
                "volatility_ranking": volatility_ranking,
            })

    # 生成 monitor_data.json（使用紧凑格式减少体积）
    output = {
        "updated_at": now_str,
        "stats": stats,
        "prices": prices_snapshot,
        "alerts": alerts,
        "threshold_pct": ALERT_THRESHOLD_PCT,
        "monitor_interval_min": 10,
        "version": "2.0",
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "monitor_data.json"
    
    # 使用 separators 减少空格，节省约 20% 体积
    json_str = json.dumps(output, ensure_ascii=False, separators=(',', ':'))
    out_path.write_text(json_str, encoding="utf-8")
    
    logger.info("Written %s (%d bytes, %d prices, %d new alerts)", 
                out_path, out_path.stat().st_size, len(prices_snapshot), new_alerts)

# -- Entry --------------------------------------------------------
if __name__ == "__main__":
    init_db()
    logger.info("cs2-monitor starting...")
    poll_and_detect()
