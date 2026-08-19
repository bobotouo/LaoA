# A 股全景

- 上证指数、深证成指、创业板指分时走势
- 行业与概念板块日内涨跌幅对比（含代表票）
- 沪深成交额、全天预测、5/20/60 日均额
- 上涨/下跌家数与涨跌幅分布
- 涨停/跌停数量、市场热门高标地（连板排序）
- 策略 A+ 筛选结果（每个交易日收盘后由 GitHub Actions 自动更新）

## 技术栈

- 后端：FastAPI、Requests
- 前端：React、Vite、ECharts
- 数据：东方财富公开行情（板块/分时/涨停池）+ FinShare/通达信（全市场快照与指数兜底）
- 自动化：GitHub Actions 每日 16:30（北京时间）运行 A+ 策略并把前 10 只推送到仪表盘

## 每日 A+ 自动化

`.github/workflows/daily-a-plus.yml` 在每个交易日 16:30 自动运行 A+ 筛选器
（`strategy/screen_strategy_a_plus.py`，数据源为新浪/腾讯/东财免费接口），
并把评分前 10 的标的 POST 到 `/api/strategy/a-plus`。手动触发可在 GitHub
仓库 Actions 页面点 "Run workflow"。

## 板块数据说明

当前板块榜与分时来自 **东方财富** `BK` 板块体系：

- 行业：`m:90+t:2`
- 概念：`m:90+t:3`
- 成分股：`b:BKxxxx`

## 安装

```bash
make setup
```

安装被中断或网络波动时可以分别重试，不必删除虚拟环境：

```bash
make setup-python
make setup-frontend
make doctor
```

## 开发运行

终端一：

```bash
make dev-backend
```

终端二：

```bash
make dev-frontend
```

访问 `http://127.0.0.1:5173`。

## 测试与生产构建

```bash
make test
make build
make run
```