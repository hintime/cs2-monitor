# CS2 Monitor - 饰品价格异动监控

🎮 **实时监控 CS2 饰品价格异动，基于 ECOSteam API**

🔗 **在线访问**: https://hintime.github.io/cs2-monitor/

## ✨ 功能特性

- 📊 **实时监控**: 每10分钟自动抓取 ECOSteam 价格数据
- 🔔 **异动告警**: 价格涨跌超过阈值时自动记录
- 📈 **趋势图表**: 24小时价格走势迷你图
- 🔍 **智能搜索**: 支持饰品名称搜索和涨跌筛选
- 📱 **响应式设计**: 适配桌面和移动设备
- ⚡ **高性能**: 优化的数据结构和渲染性能

## 🚀 快速开始

### 1. Fork 本仓库

点击右上角的 "Fork" 按钮

### 2. 配置 Secrets

在仓库设置中添加以下 Secrets:

| Secret | 说明 |
|--------|------|
| `ECO_PARTNER_ID` | ECOSteam 合作伙伴ID |
| `ECO_PRIVATE_KEY_B64` | Base64编码的 RSA 私钥 |

### 3. 启用 GitHub Pages

1. 进入 Settings → Pages
2. Source 选择 "GitHub Actions"
3. 工作流会自动部署

### 4. 自定义配置

在仓库 Variables 中可以设置:

| Variable | 默认值 | 说明 |
|----------|--------|------|
| `ALERT_THRESHOLD_PCT` | 5.0 | 告警阈值百分比 |

## 📁 项目结构

```
cs2-monitor/
├── .github/
│   └── workflows/
│       └── monitor.yml      # GitHub Actions 工作流
├── docs/
│   ├── index.html           # 监控页面
│   └── monitor_data.json    # 生成的数据文件
├── fetch_monitor.py         # 数据抓取脚本
├── requirements.txt         # Python 依赖
└── README.md
```

## 🔧 本地开发

### 安装依赖

```bash
pip install requests pycryptodome
```

### 运行监控脚本

```bash
export ECO_PARTNER_ID="your_partner_id"
export ECO_PRIVATE_KEY_B64="your_base64_private_key"
python fetch_monitor.py
```

### 启动本地服务器查看页面

```bash
cd docs
python -m http.server 8000
```

访问 http://localhost:8000

## 📊 数据源

- **ECOSteam API**: https://openapi.ecosteam.cn
- **更新频率**: 每10分钟
- **监控物品**: 20件热门 CS2 饰品

## 🛠️ 技术栈

- **后端**: Python 3.11 + SQLite
- **前端**: 原生 HTML/CSS/JavaScript
- **部署**: GitHub Actions + GitHub Pages
- **API**: ECOSteam Open API

## 📝 监控物品列表

| 饰品名称 | 品质 |
|---------|------|
| AK-47 \| Redline | Field-Tested |
| AWP \| Asiimov | Field-Tested |
| M4A1-S \| Printstream | Field-Tested |
| Desert Eagle \| Blaze | Factory New |
| ... | ... |

## 🤝 贡献

欢迎提交 Issue 和 Pull Request!

## 📄 许可证

MIT License

## 🙏 致谢

- [ECOSteam](https://www.ecosteam.cn/) 提供数据接口
- GitHub 提供免费托管服务
