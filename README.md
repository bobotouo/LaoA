# A 股全景

- 上证指数、深证成指、创业板指分时走势
- 行业与概念板块日内涨跌幅对比（含代表票）
- 沪深成交额、全天预测、5/20/60 日均额
- 上涨/下跌家数与涨跌幅分布
- 涨停数量、跌停数量与涨停梯队


## 技术栈

- 后端：FastAPI、Requests
- 前端：React、Vite、ECharts
- 数据：东方财富公开行情（板块/分时/涨停池）+ FinShare/通达信（全市场快照与指数兜底）


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

## API

- `GET /api/health`
- `GET /api/market/dashboard?refresh=false`
- FastAPI 文档：`/docs`

## 部署到 Vercel

1. 推送仓库到 GitHub 后在 [vercel.com/new](https://vercel.com/new) 导入，或本地执行 `vercel --prod`
2. Python 依赖交给 Vercel 自动安装（`.python-version` = 3.12）；`installCommand` 只跑前端 `npm install`，不要手动 `pip`/`uv pip --system`（会落到镜像自带的 3.9）
3. 前端构建输出到 `public/`，API 入口为 `api/index.py`，函数超时 60s

注意：海外节点上 FinShare/通达信可能连不上，会自动回退东财接口。

