# A 股全景

一个面向中国 A 股的实时市场看板 MVP，覆盖：

- 上证指数、深证成指、创业板指分时走势
- 行业与概念板块日内涨跌幅对比（含代表票）
- 沪深成交额、全天预测、5/20/60 日均额
- 上涨/下跌家数与涨跌幅分布
- 涨停数量、跌停数量与涨停梯队

交易时段持续刷新真实行情；开盘前 / 收盘后继续拉取并展示最近一个交易日的最新快照。接口失败时回退到本地缓存的最近一次成功数据，不再使用演示数据。

## 技术栈

- 后端：FastAPI、Requests
- 前端：React、Vite、ECharts
- 数据：东方财富公开行情（板块/分时/涨停池）+ FinShare/通达信（全市场快照与指数兜底）

> 公开网页行情没有 SLA，也不等于获得数据再分发授权。公开部署或商业使用前，应替换为交易所授权数据源、iFinD、Wind、Choice 等正式服务。

## 板块数据说明

当前板块榜与分时来自 **东方财富** `BK` 板块体系：

- 行业：`m:90+t:2`
- 概念：`m:90+t:3`
- 成分股：`b:BKxxxx`

FinShare 的 `get_industry_list()` / `get_concept_list()` 底层也是东财同一套接口。通达信行业指数仅作东财失败时的行业兜底，覆盖不完整。

同花顺板块（iFinD / QuantAPI / 非官方 thsdk）分类更细，但是商业授权或稳定性风险更高，本项目未接入。

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

生产构建完成后，FastAPI 会直接托管 `frontend/dist`，访问 `http://127.0.0.1:8000` 即可。

## API

- `GET /api/health`
- `GET /api/market/dashboard?refresh=false`
- FastAPI 文档：`/docs`

## 部署到 Vercel

本仓库已按「前端静态资源 + FastAPI Serverless」配置好：

| 文件 | 作用 |
|------|------|
| `vercel.json` | 安装/构建命令、函数超时、SPA 回退 |
| `api/index.py` | Vercel Python 入口 |
| `scripts/vercel-build.sh` | 构建前端并输出到 `public/` |
| `requirements.txt` | Vercel 安装 Python 依赖 |
| `.python-version` | 固定 Python 3.12 |

### 方式 A：GitHub 导入（推荐）

1. 把仓库推到 GitHub
2. 打开 [vercel.com/new](https://vercel.com/new)，导入该仓库
3. Framework Preset 保持默认即可（已有 `vercel.json` / `pyproject.toml`）
4. 点击 Deploy

部署完成后访问 `https://你的项目.vercel.app`，健康检查：`/api/health`。

### 方式 B：Vercel CLI

```bash
npm i -g vercel
cd /Users/bobo/Documents/LaoA
vercel login
vercel        # 预览环境
vercel --prod # 生产环境
```

### 部署注意

- 看板接口可能要拉取十余秒，已把函数 `maxDuration` 设为 **60s**
- Vercel 无持久磁盘，最近成功行情缓存在 `/tmp`，冷启动后可能暂时没有 stale 回退
- **FinShare/通达信** 在海外 Serverless 上经常连不上，届时会自动走东财公开接口；板块数据本身就来自东财
- Hobby 套餐有执行时长与流量限制；若经常超时，可把后端放到 Railway / Render / 一台国内 VPS，前端继续放 Vercel
- 公开行情无再分发授权保证，正式对外前请换授权数据源
