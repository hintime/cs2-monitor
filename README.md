# CS2 Monitor 🔫

CS2 饰品价格异动监控 — 定时拉取 ECOSteam 数据，检测异动并展示。

**在线地址**: https://hintime.github.io/cs2-monitor/

## 架构

```
GitHub Actions (每10分钟)         GitHub Pages
┌─────────────────────────┐      ┌──────────────────┐
│ fetch_monitor.py        │      │ docs/index.html  │
│  ├─ ECO 全量价格拉取     │ ──→  │ docs/monitor_    │
│  ├─ SQLite 历史记录       │      │   data.json      │
│  ├─ 异动检测 (≥5%)       │      └──────────────────┘
│  └─ 生成 monitor_data.json│
└─────────────────────────┘
```

## 监控物品

20 件热门 CS2 饰品皮肤，覆盖 AK-47、AWP、M4、USP、沙鹰等核心枪械。

## 文件

| 文件 | 说明 |
|------|------|
| `fetch_monitor.py` | Actions 后端 — 拉数据 + 检测异动 |
| `docs/index.html` | 前端 — 静态看板 |
| `docs/monitor_data.json` | 输出的 JSON 数据 |
| `.github/workflows/monitor.yml` | Actions 定时触发 |

## Secret 配置

需要在 GitHub 仓库 Settings → Secrets and variables → Actions 中设置：

- `ECO_PARTNER_ID`: ECOSteam PartnerId
- `ECO_PRIVATE_KEY_B64`: RSA 私钥 PEM 的 Base64

## 本地运行

```bash
pip install requests pycryptodome
python fetch_monitor.py
```

## 姊妹项目

- [cs2-dashboard](https://hintime.github.io/cs2-dashboard/) — 持仓投资看板