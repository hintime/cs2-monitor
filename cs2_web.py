"""
CS2 Monitor — CS2 饰品价格异动监控服务
=========================================
持续轮询 ECOSteam 开放平台，记录价格历史，检测异动并生成告警。
设计为 Heroku Worker 部署，也支持本地 `python cs2_web.py` 运行。

环境变量:
  ECO_PARTNER_ID       ECOSteam PartnerId（必需）
  ECO_PRIVATE_KEY_B64  RSA 私钥 PEM 的 Base64（必需）
  MONITOR_ITEMS        逗号分隔的 HashName 列表（可选，默认内置列表）
  POLL_INTERVAL_MIN    轮询间隔分钟（默认 5）
  ALERT_THRESHOLD_PCT  异动告警阈值 %（默认 5.0）
  DB_PATH              SQLite 路径（默认 monitor.db）
"""
import os
import sys
import json
import time
import base64
import hashlib
import sqlite3
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template_string, request, g
from apscheduler.schedulers.background import BackgroundScheduler
from Crypto.Hash import SHA256
from Crypto.Signature import pkcs1_15
from Crypto.PublicKey import RSA

# ──────────────────────────── Config ────────────────────────────
ECO_BASE = "https://openapi.ecosteam.cn"
ECO_PARTNER_ID = os.environ.get("ECO_PARTNER_ID", "")
ECO_PRIVATE_KEY_B64 = os.environ.get("ECO_PRIVATE_KEY_B64", "")
GAME_ID = "730"
POLL_INTERVAL_MIN = int(os.environ.get("POLL_INTERVAL_MIN", "5"))
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "5.0"))
DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent / "monitor.db"))

# 默认监控列表（HashName）
DEFAULT_MONITOR = [
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
MONITOR_ITEMS = [
    x.strip() for x in os.environ.get("MONITOR_ITEMS", "").split(",") if x.strip()
] or DEFAULT_MONITOR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cs2-monitor")

# ──────────────────────────── ECO Signing ────────────────────────────
_private_key = None

def _load_private_key() -> RSA.RsaKey:
    global _private_key
    if _private_key is None:
        pem = base64.b64decode(ECO_PRIVATE_KEY_B64).decode()
        _private_key = RSA.import_key(pem)
    return _private_key

def eco_sign(params: dict) -> str:
    """SHA256withRSA 签名"""
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
    sig = pkcs1_15.new(_load_private_key()).sign(h)
    return base64.b64encode(sig).decode()

def eco_post(endpoint: str, body: dict) -> dict:
    """POST 到 ECOSteam API"""
    body.setdefault("GameID", GAME_ID)
    body["PartnerId"] = ECO_PARTNER_ID
    body["Timestamp"] = str(int(time.time()))
    body["Sign"] = eco_sign(body)
    url = f"{ECO_BASE}{endpoint}"
    for attempt in range(3):
        try:
            resp = requests.post(url, json=body, timeout=15)
            data = resp.json()
            if data.get("ResultCode") == 0:
                return data
            logger.warning("ECO API error %s: %s", data.get("ResultCode"), data.get("ResultMsg"))
        except Exception as e:
            logger.warning("ECO request attempt %d failed: %s", attempt + 1, e)
            time.sleep(2)
    return {"ResultCode": -1, "ResultMsg": "All retries failed"}

# ──────────────────────────── Database ────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS prices (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            hash_name   TEXT    NOT NULL,
            price       REAL    NOT NULL,
            sell_total  INTEGER,
            recorded_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_prices_name_time
            ON prices(hash_name, recorded_at);

        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            hash_name   TEXT    NOT NULL,
            old_price   REAL    NOT NULL,
            new_price   REAL    NOT NULL,
            change_pct  REAL    NOT NULL,
            direction   TEXT    NOT NULL,  -- 'up' or 'down'
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_time
            ON alerts(created_at);
    """)
    db.commit()
    db.close()

def insert_price(hash_name: str, price: float, sell_total: int = 0):
    db = sqlite3.connect(DB_PATH)
    db.execute(
        "INSERT INTO prices (hash_name, price, sell_total) VALUES (?, ?, ?)",
        (hash_name, price, sell_total),
    )
    db.commit()
    db.close()

def get_last_price(hash_name: str) -> dict | None:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT * FROM prices WHERE hash_name=? ORDER BY recorded_at DESC LIMIT 1",
        (hash_name,),
    ).fetchone()
    db.close()
    return dict(row) if row else None

def insert_alert(hash_name: str, old_price: float, new_price: float,
                 change_pct: float, direction: str):
    db = sqlite3.connect(DB_PATH)
    db.execute(
        "INSERT INTO alerts (hash_name, old_price, new_price, change_pct, direction) "
        "VALUES (?, ?, ?, ?, ?)",
        (hash_name, old_price, new_price, change_pct, direction),
    )
    db.commit()
    db.close()

def get_recent_alerts(limit: int = 50) -> list[dict]:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]

def get_price_history(hash_name: str, hours: int = 24) -> list[dict]:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = db.execute(
        "SELECT * FROM prices WHERE hash_name=? AND recorded_at>=? ORDER BY recorded_at",
        (hash_name, cutoff),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]

# ──────────────────────────── Polling ────────────────────────────
lock = threading.Lock()

def poll_prices():
    """轮询 ECO 全量价格，匹配监控列表并检测异动"""
    if not ECO_PARTNER_ID or not ECO_PRIVATE_KEY_B64:
        logger.warning("ECO credentials not configured, skipping poll")
        return

    logger.info("Polling ECO prices for %d monitored items...", len(MONITOR_ITEMS))
    try:
        result = eco_post("/Api/Market/GetHashNameAndPriceList", {})
        items = result.get("ResultData", [])
        if not items:
            logger.warning("Poll: empty ResultData")
            return
    except Exception as e:
        logger.error("Poll failed: %s", e)
        return

    # 构建 HashName → 价格 映射
    price_map = {
        it["HashName"]: it for it in items if "HashName" in it
    }

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    new_alerts = 0

    with lock:
        for name in MONITOR_ITEMS:
            eco_item = price_map.get(name)
            if not eco_item:
                continue

            current_price = float(eco_item.get("MarketComprePrice", 0))
            sell_count = int(eco_item.get("SellingTotal", 0))
            if current_price <= 0:
                continue

            # 记录价格
            last = get_last_price(name)
            insert_price(name, current_price, sell_count)

            # 检测异动
            if last and last.get("price", 0) > 0:
                change_pct = (current_price - last["price"]) / last["price"] * 100
                if abs(change_pct) >= ALERT_THRESHOLD_PCT:
                    direction = "up" if change_pct > 0 else "down"
                    insert_alert(name, last["price"], current_price, round(change_pct, 2), direction)
                    new_alerts += 1
                    emoji = "🔺" if direction == "up" else "🔻"
                    logger.info(
                        "%s ALERT %s: ¥%.2f → ¥%.2f (%+.2f%%)",
                        emoji, name, last["price"], current_price, change_pct,
                    )

    logger.info("Poll done: %d prices recorded, %d new alerts", len(MONITOR_ITEMS), new_alerts)

# ──────────────────────────── Flask App ────────────────────────────
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

# ── Pages ──
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CS2 Monitor — 饰品价格异动监控</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1923;color:#e0e0e0;min-height:100vh}
.header{background:linear-gradient(135deg,#1a2a3a,#0f1923);padding:2rem;text-align:center;border-bottom:2px solid #2a4a6a}
.header h1{font-size:2rem;color:#66b3ff}
.header p{color:#8899aa;margin-top:0.5rem}
.container{max-width:1200px;margin:0 auto;padding:1.5rem}
.status-row{display:flex;gap:1rem;margin-bottom:1.5rem;flex-wrap:wrap}
.card{background:#1a2a3a;border-radius:8px;padding:1.2rem;flex:1;min-width:180px;border:1px solid #2a4a6a}
.card .label{font-size:0.8rem;color:#8899aa;text-transform:uppercase}
.card .value{font-size:1.8rem;font-weight:700;margin-top:0.3rem}
.card .value.up{color:#4caf50}.card .value.down{color:#f44336}
table{width:100%;border-collapse:collapse;background:#1a2a3a;border-radius:8px;overflow:hidden;border:1px solid #2a4a6a}
th{background:#243447;padding:0.8rem;text-align:left;font-weight:600;color:#8899aa;font-size:0.85rem}
td{padding:0.8rem;border-bottom:1px solid #243447}
.price-up{color:#4caf50}.price-down{color:#f44336}
.alert-row{animation:fadeIn 0.5s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(-10px)}}
.refresh-info{text-align:center;color:#556677;font-size:0.8rem;margin-top:1rem}
a{color:#66b3ff;text-decoration:none}
.section-title{font-size:1.2rem;margin:1.5rem 0 1rem;color:#66b3ff}
</style>
</head>
<body>
<div class="header">
  <h1>🔫 CS2 Monitor</h1>
  <p>实时饰品价格监控 &amp; 异动告警</p>
</div>
<div class="container">
  <div class="status-row">
    <div class="card">
      <div class="label">监控物品</div>
      <div class="value">{{ stats.item_count }}</div>
    </div>
    <div class="card">
      <div class="label">今日告警</div>
      <div class="value {{ 'up' if stats.alert_count else '' }}">{{ stats.alert_count }}</div>
    </div>
    <div class="card">
      <div class="label">最后轮询</div>
      <div class="value" style="font-size:1rem">{{ stats.last_poll }}</div>
    </div>
  </div>

  <h2 class="section-title">⏱ 最近告警</h2>
  <table>
    <thead>
      <tr><th>时间</th><th>物品</th><th>旧价格</th><th>新价格</th><th>变化</th></tr>
    </thead>
    <tbody>
    {% for a in alerts %}
      <tr class="alert-row">
        <td>{{ a.created_at }}</td>
        <td>{{ a.hash_name }}</td>
        <td>¥{{ "%.2f"|format(a.old_price) }}</td>
        <td>¥{{ "%.2f"|format(a.new_price) }}</td>
        <td class="{{ 'price-up' if a.direction=='up' else 'price-down' }}">
          {{ "%+.2f"|format(a.change_pct) }}%
        </td>
      </tr>
    {% else %}
      <tr><td colspan="5" style="text-align:center;color:#556677">暂无告警</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <h2 class="section-title">📊 当前价格</h2>
  <table>
    <thead>
      <tr><th>物品</th><th>当前价格</th><th>在售数量</th><th>24h 变化</th></tr>
    </thead>
    <tbody>
    {% for p in prices %}
      <tr>
        <td>{{ p.hash_name }}</td>
        <td>¥{{ "%.2f"|format(p.price) }}</td>
        <td>{{ p.sell_total }}</td>
        <td class="{{ 'price-up' if p.change_pct and p.change_pct > 0 else 'price-down' }}">
          {{ ("%+.2f%%"|format(p.change_pct)) if p.change_pct is not none else '--' }}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>

  <p class="refresh-info">数据源: ECOSteam · 轮询间隔: {{ interval_min }}分钟</p>
</div>
</body>
</html>"""

@app.route("/")
def index():
    interval_min = POLL_INTERVAL_MIN
    db = get_db()

    # 统计
    item_count = len(MONITOR_ITEMS)
    alert_count = db.execute(
        "SELECT COUNT(*) FROM alerts WHERE created_at >= date('now')"
    ).fetchone()[0]

    # 最近告警
    alerts = [
        dict(r) for r in db.execute(
            "SELECT * FROM alerts ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
    ]

    # 当前价格
    prices = []
    for name in MONITOR_ITEMS:
        last = get_last_price(name)
        if last:
            # 24h 前价格
            prev = db.execute(
                "SELECT price FROM prices WHERE hash_name=? "
                "AND recorded_at <= datetime('now', '-24 hours') "
                "ORDER BY recorded_at DESC LIMIT 1",
                (name,),
            ).fetchone()
            change = None
            if prev and prev[0] > 0:
                change = round((last["price"] - prev[0]) / prev[0] * 100, 2)
            prices.append({
                "hash_name": name,
                "price": last["price"],
                "sell_total": last.get("sell_total", 0),
                "change_pct": change,
            })

    last_poll = db.execute(
        "SELECT MAX(recorded_at) FROM prices"
    ).fetchone()[0] or "N/A"

    return render_template_string(INDEX_HTML,
        stats={"item_count": item_count, "alert_count": alert_count,
               "last_poll": last_poll},
        alerts=alerts, prices=prices, interval_min=interval_min)

# ── API ──
@app.route("/api/prices")
def api_prices():
    """返回当前监控物品价格"""
    result = []
    for name in MONITOR_ITEMS:
        last = get_last_price(name)
        result.append(last if last else {"hash_name": name, "price": 0, "sell_total": 0})
    return jsonify(result)

@app.route("/api/price/<path:hash_name>")
def api_price_single(hash_name: str):
    """单个物品当前价格"""
    last = get_last_price(hash_name)
    if not last:
        return jsonify({"error": "not found"}), 404
    history = get_price_history(hash_name, hours=24)
    last["history_24h"] = [{"price": h["price"], "time": h["recorded_at"]} for h in history]
    return jsonify(last)

@app.route("/api/alerts")
def api_alerts():
    """最近告警列表"""
    limit = request.args.get("limit", 50, type=int)
    return jsonify(get_recent_alerts(limit))

@app.route("/api/health")
def api_health():
    """健康检查"""
    db = get_db()
    last_recorded = db.execute("SELECT MAX(recorded_at) FROM prices").fetchone()[0]
    return jsonify({
        "status": "ok",
        "items_monitored": len(MONITOR_ITEMS),
        "last_poll": last_recorded,
        "poll_interval_min": POLL_INTERVAL_MIN,
    })

# ──────────────────────────── Main ────────────────────────────
def main():
    init_db()
    logger.info("CS2 Monitor starting — %d items monitored", len(MONITOR_ITEMS))

    # 启动时立即跑一次轮询
    poll_prices()

    # 定时轮询
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(poll_prices, "interval", minutes=POLL_INTERVAL_MIN, id="poll")
    scheduler.start()
    logger.info("Scheduler started: every %d min", POLL_INTERVAL_MIN)

    port = int(os.environ.get("PORT", 8766))
    # Heroku 上用 gunicorn；本地直接跑 Flask
    if os.environ.get("DYNO"):  # Heroku 运行时
        # gunicorn 会导入 app，这里只启动 scheduler
        logger.info("Running on Heroku (DYNO detected), gunicorn will serve app")
    else:
        logger.info("Starting Flask dev server on :%d", port)
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()