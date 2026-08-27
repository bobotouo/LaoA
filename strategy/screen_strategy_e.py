#!/usr/bin/env python3
"""
策略E: 大盘风险监控与预警 Market Risk Monitor
═══════════════════════════════════════════════════════════
与个股策略(A/A+/B/C/D)并列的大盘环境策略。
不选个股, 输出 0~100 大盘风险分 + 四级预警, 作为所有开仓动作的前置闸门:
  - 🟢/🟡 允许按既定策略执行(ELEVATED 需降仓收紧止损)
  - 🟠 HIGH 建议半仓以下并暂停 C/D 打板低吸类策略
  - 🔴 SEVERE 建议清仓观望

因子体系(内部55% + 外围45%, 缺失因子自动按权重归一化):

【内部 · A股自身】
  F1  trend      趋势破位   12%  上证 vs MA10/20/60, 均线排列, MA20斜率
  F2  momentum   短期动量    8%  5日/20日跌幅越深分越高
  F3  drawdown   回撤深度    7%  距近20日高点的回撤
  F4  breadth    市场宽度   14%  全市场上涨家数占比(腾讯全量行情扫描)
  F5  limit      涨跌停对比  6%  跌停家数 / 跌停涨停比(恐慌度)
  F6  volume     量能异动    4%  放量下跌为高危信号
  F7  volatility 波动/异动   4%  振幅放大 + 跳空缺口(突发冲击代理)

【外围 · 全球联动】
  F8  overnight  隔夜美股   12%  纳指为主, 三大指数齐跌加重
  F9  vix        恐慌指数    8%  >20警惕 >25高危(带时效校验, 数据过期自动剔除)
  F10 a50        富时A50   10%  新加坡A50期指, A股最直接领先指标
  F11 hk         港股联动    5%  恒生 + 恒生科技
  F12 fx         汇率压力    7%  USDCNH 快速贬值 = 外资流出压力
  F13 commodity  避险商品    3%  黄金急涨 + 原油大跌 = risk-off 组合

风险分级 / 退出码(便于脚本闸门):
  0-29   🟢 LOW       正常     exit 0   可按既定策略执行
  30-49  🟡 ELEVATED  警惕     exit 0   降仓+收紧止损
  50-69  🟠 HIGH      高风险   exit 2   半仓以下, 暂停C/D
  70-100 🔴 SEVERE    极端风险 exit 3   清仓观望

共振加成(非线性修正):
  趋势破位/市场宽度/短期动量/A50期指/港股联动 五个核心因子中,
  ≥2个同时达到50分 → +5分; ≥3个 → +10分; ≥4个 → +15分。
  系统性risk-off日的风险是非线性的, 单因子线性加权会低估, 此项人工修正。

用法:
  python3 src/screen_strategy_e.py                 # 完整评估(含全市场宽度, ~1.5分钟)
  python3 src/screen_strategy_e.py --fast          # 快速版(~10秒, 跳过宽度扫描)
  python3 src/screen_strategy_e.py --watch 15      # 盘中每15分钟轮询, 升级时告警
  python3 src/screen_strategy_e.py --publish-url URL  # 结果POST到dashboard

脚本闸门示例:
  python3 src/screen_strategy_e.py --fast || { echo '大盘高风险, 停止执行B/C/D'; exit 1; }

输出文件:
  runs/market_risk/latest.json            最新评估(含全部因子明细)
  runs/market_risk/market_risk_*.json     每次运行存档
  runs/screen_e_latest.json               同步镜像(与其他screen_*输出同目录)

设计原则(遵循 docs/operating_rules.md):
  - 全部为确定性代码, 无未来函数; 每个外部数据自带日期做时效校验
  - 免费数据源(腾讯/新浪/东财), 无需token, 与其他 screener 一致
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SINA_CACHE_DIR = ROOT / "data" / "sina_lists"
OUT_DIR = ROOT / "runs" / "market_risk"
LATEST_MIRROR = ROOT / "runs" / "screen_e_latest.json"

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}

# 因子权重(缺失时按剩余权重归一化)
FACTOR_WEIGHTS = {
    "trend": 0.12, "momentum": 0.08, "drawdown": 0.07, "breadth": 0.14,
    "limit": 0.06, "volume": 0.04, "volatility": 0.04,
    "overnight_us": 0.12, "vix": 0.08, "a50": 0.10, "hk": 0.05,
    "fx": 0.07, "commodity": 0.03,
}
TOTAL_WEIGHT = sum(FACTOR_WEIGHTS.values())
FACTOR_NAMES = {
    "trend": "趋势破位", "momentum": "短期动量", "drawdown": "回撤深度",
    "breadth": "市场宽度", "limit": "涨跌停恐慌", "volume": "量能异动",
    "volatility": "波动/突发", "overnight_us": "隔夜美股", "vix": "恐慌指数VIX",
    "a50": "富时A50期指", "hk": "港股联动", "fx": "汇率压力", "commodity": "避险商品",
}

LEVELS = [
    (70, "🔴 SEVERE", "极端风险"),
    (50, "🟠 HIGH", "高风险"),
    (30, "🟡 ELEVATED", "警惕"),
    (0, "🟢 LOW", "正常"),
]
ORDER = {"LOW": 0, "ELEVATED": 1, "HIGH": 2, "SEVERE": 3}
EXIT_CODES = {"LOW": 0, "ELEVATED": 0, "HIGH": 2, "SEVERE": 3}
COLORS = {"LOW": "\033[1;92m", "ELEVATED": "\033[1;93m",
          "HIGH": "\033[1;33m", "SEVERE": "\033[1;91m"}
ADVICE = {
    "LOW": "大盘环境正常, 可按既定策略(A/A+/B/C/D)正常筛选执行。",
    "ELEVATED": "大盘转弱, 新开仓降仓(单票≤25%), 收紧止损至-2%, 高位连板股回避。",
    "HIGH": "高风险! 建议总仓位降至半仓以下, 暂停策略C/D(打板/低吸), 仅保留A+趋势票。",
    "SEVERE": "极端风险! 建议清仓观望, 停止一切新开仓, 等待企稳信号(MA20收复+宽度修复)。",
}


# ─────────────────────────── HTTP helpers ───────────────────────────
def http_get(url: str, timeout: int = 8, retries: int = 2) -> str | None:
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("gbk", errors="ignore")
        except Exception:
            if attempt == retries:
                return None
            time.sleep(0.6)


def http_get_json(url: str, timeout: int = 8) -> dict | None:
    raw = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8"))
        except Exception:
            if attempt == 2:
                return None
            time.sleep(0.6)


def strip_today(k: list[list]) -> list[list]:
    """盘前模式用: 只保留今日之前的K线(今日尚未交易, 不能算作已知事实)"""
    today = date.today().isoformat()
    return [r for r in k if str(r[0])[:10] < today]


def tencent_quotes(codes: list[str]) -> dict[str, dict]:
    """腾讯实时行情, 返回 {code: {name,price,prev,open,high,low,chg_pct,...}}"""
    out: dict[str, dict] = {}
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        txt = http_get(f"http://qt.gtimg.cn/q={','.join(batch)}")
        if not txt:
            continue
        for line in txt.strip().split("\n"):
            if '"' not in line:
                continue
            try:
                f = line.split('"')[1].split("~")
                if len(f) < 35 or not f[3]:
                    continue
                out[f[2]] = {
                    "name": f[1], "code": f[2], "price": float(f[3]),
                    "prev": float(f[4]) if f[4] else 0.0,
                    "open": float(f[5]) if f[5] else 0.0,
                    "chg_pct": float(f[32]) if f[32] else 0.0,
                    "high": float(f[33]) if f[33] else 0.0,
                    "low": float(f[34]) if f[34] else 0.0,
                    "amount_wan": float(f[37]) if len(f) > 37 and f[37] else 0.0,
                }
            except Exception:
                continue
    return out


def index_kline(code: str, days: int = 90) -> list[list]:
    """指数日K: [[date, open, close, high, low, volume], ...] 升序"""
    end = date.today()
    start = end - timedelta(days=int(days * 1.7))
    url = (f"http://ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={code},day,{start.isoformat()},{end.isoformat()},{days},")
    data = http_get_json(url)
    if not data:
        return []
    try:
        kd = data["data"][code]
        rows = kd.get("day") or kd.get("qfqday") or []
        rows = sorted(rows, key=lambda r: r[0])
        return [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
                for r in rows if len(r) >= 6]
    except Exception:
        return []


def sina_global(code: str) -> dict | None:
    """新浪全球品种(hf_/DINIW), 自带日期做时效校验"""
    txt = http_get(f"https://hq.sinajs.cn/list={code}")
    if not txt or '"' not in txt:
        return None
    try:
        body = txt.split('"')[1]
        f = body.split(",")
        price_idx = 1 if code.upper().startswith("DINI") else 0
        price = float(f[price_idx])
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", body)
        qdate = dates[-1] if dates else ""
        d: dict = {"code": code, "price": price, "date": qdate, "fields": f}
        # hf_ 格式: [0]最新 [2]买 [3]卖 [4]最高 [5]最低 [6]时间 [7]昨结算 [8]今开 [9]持仓
        if code.startswith("hf_") and len(f) > 9:
            try:
                d.update({"prev": float(f[7]), "high": float(f[4]),
                          "low": float(f[5]), "open": float(f[8])})
            except (ValueError, IndexError):
                pass
        else:
            nums = []
            for x in f[2:9]:
                try:
                    v = float(x)
                    if 5 < v < 200:
                        nums.append(v)
                except ValueError:
                    pass
            d["prev"] = sorted(nums)[len(nums) // 2] if nums else price
        return d
    except Exception:
        return None


def em_quote_ratio(secid: str) -> dict | None:
    """东财报价, 用 f43/f60 比值算涨跌幅(规避精度问题)"""
    url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
           f"&fields=f43,f60&ut=fa5fd1943c7b386f172d6893dbfba10b")
    d = http_get_json(url)
    try:
        data = d.get("data") or {}
        p, prev = data.get("f43"), data.get("f60")
        if p and prev:
            return {"price_raw": p, "prev_raw": prev,
                    "chg_pct": (p / prev - 1) * 100}
    except Exception:
        pass
    return None


# ─────────────────── yfinance 兜底 (海外 runner) ───────────────────
# 新浪 hq.sinajs.cn 对海外 IP 不可达。全球外围数据(A50/黄金/原油/美元指数/VIX/
# 美股/港股)在海外 runner 上用 yfinance(Yahoo, 美国服务) 兜底。本地仍优先新浪。
YF_SYMBOLS = {
    "a50": "2823.HK",          # 南方A50 ETF(近似新加坡富时A50期指)
    "gold": "GC=F",            # COMEX 黄金期货
    "oil": "BZ=F",             # 布伦特原油
    "udi": "DX-Y.NYB",         # 美元指数
    "vix": "^VIX",
    "usDJI": "^DJI",
    "usIXIC": "^IXIC",
    "usINX": "^GSPC",
    "hkHSI": "^HSI",
    "hkHSTECH": "^HSTECH",
    "usdcnh": "CNH=X",
}


def _yf_history_quote(symbol: str) -> dict | None:
    """用 yfinance 取最近两根日K, 返回 {price, prev, date} 供因子计分用."""
    import yfinance as yf  # 延迟导入, 避免本地未安装时报错
    try:
        data = yf.Ticker(symbol).history(period="5d", interval="1d")
        if data is None or len(data) < 2:
            return None
        last = data.iloc[-1]
        prev = data.iloc[-2]
        return {
            "symbol": symbol,
            "price": float(last["Close"]),
            "prev": float(prev["Close"]),
            "date": last.name.strftime("%Y-%m-%d"),
        }
    except Exception:
        return None


def _yf_fallback() -> dict[str, dict | None]:
    """批量用 yfinance 拉全球外围数据(兜底), 返回与 sina_global 兼容的结构.

    a50/gold/oil/udi 需要 {price, prev, date}, 其余(quotes/vix)单独处理。
    """
    out: dict[str, dict | None] = {}
    for key in ("a50", "gold", "oil", "udi", "vix"):
        out[key] = _yf_history_quote(YF_SYMBOLS[key])
    return out


# ─────────────────────────── universe / breadth ───────────────────────────
def load_universe() -> list[tuple[str, str, str]]:
    stocks = []
    import glob
    for fp in sorted(glob.glob(str(SINA_CACHE_DIR / "page_*.json"))):
        try:
            with open(fp) as fh:
                stocks.extend(json.load(fh))
        except Exception:
            continue
    valid = []
    seen = set()
    for s in stocks:
        code = str(s.get("code", ""))
        name = str(s.get("name", "")).strip()
        if code in seen or not code:
            continue
        if ("ST" in name.upper() or "*" in name or "退" in name
                or code.startswith(("8", "4", "92"))):
            continue
        seen.add(code)
        valid.append(("sh" if code.startswith("6") else "sz", code, name))
    return valid


def market_breadth(universe) -> dict | None:
    """全市场扫描: 涨跌家数 / 涨停跌停"""
    codes = [f"{p}{c}" for p, c, _ in universe]
    meta = {c: n for _, c, n in universe}
    up = down = flat = lu = ld = valid = 0
    for i in range(0, len(codes), 80):
        batch = codes[i:i + 80]
        q = tencent_quotes(batch)
        for full_code, item in q.items():
            code = full_code.lstrip("szh")
            name = meta.get(code, item["name"])
            if item["price"] <= 0 or item["prev"] <= 0:
                continue
            valid += 1
            chg = item["chg_pct"]
            if chg > 0:
                up += 1
            elif chg < 0:
                down += 1
            else:
                flat += 1
            is_new = name.startswith(("N", "C")) and len(name) <= 5
            is_20 = code.startswith("3") or code.startswith("688")
            lim = 19.6 if is_20 else 9.8
            if not is_new:
                if chg >= lim:
                    lu += 1
                elif chg <= -lim:
                    ld += 1
        print(f"\r  宽度扫描 {min(i+80, len(codes))}/{len(codes)}", end="",
              flush=True, file=sys.stderr)
    print(file=sys.stderr, flush=True)
    if valid < 500:
        return None
    return {"valid": valid, "up": up, "down": down, "flat": flat,
            "up_pct": up / valid * 100, "down_pct": down / valid * 100,
            "limit_up": lu, "limit_down": ld}


# ─────────────────────────── factor scorers (0~100) ───────────────────────
def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def ma(vals: list[float], n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def score_trend(k: list[list]) -> tuple[float, str]:
    closes = [r[2] for r in k]
    px = closes[-1]
    m10, m20, m60 = ma(closes, 10), ma(closes, 20), ma(closes, 60)
    if not (m10 and m20 and m60):
        return 0.0, "K线不足"
    s = 0.0
    pos = []
    if px < m10:
        s += 18
        pos.append("<MA10")
    if px < m20:
        s += 32
        pos.append("<MA20")
    if px < m60:
        s += 22
        pos.append("<MA60")
    if m10 < m20:
        s += 13
        pos.append("MA10<MA20")
    if len(closes) >= 26:
        slope = ma(closes, 20) / ma(closes[:-5], 20) - 1
        if slope < -0.005:
            s += 15
            pos.append("MA20下行")
    detail = f"上证{px:.0f} {'/'.join(pos) if pos else '站上所有均线'}"
    return clamp(s), detail


def score_momentum(k: list[list]) -> tuple[float, str]:
    closes = [r[2] for r in k]
    if len(closes) < 21:
        return 0.0, "K线不足"
    chg5 = closes[-1] / closes[-6] - 1
    chg20 = closes[-1] / closes[-21] - 1
    # 两项各自独立计分(不互相对冲): 短期急跌是即期风险, 不因中期仍涨而抵消
    short_term = max(0.0, (-chg5 - 0.010)) / 0.050 * 70
    mid_term = max(0.0, (-chg20 - 0.030)) / 0.100 * 30
    s = clamp(short_term + mid_term)
    return s, f"5日{chg5:+.1%} / 20日{chg20:+.1%}"


def score_drawdown(k: list[list]) -> tuple[float, str]:
    highs = [r[3] for r in k]
    px = k[-1][2]
    if len(highs) < 20:
        return 0.0, "K线不足"
    dd = px / max(highs[-20:]) - 1
    return clamp((-dd - 0.015) / 0.065 * 100), f"距20日高点{dd:+.1%}"


def score_breadth(b: dict | None) -> tuple[float, str]:
    if not b:
        return 0.0, "未扫描(--fast)"
    up_pct = b["up_pct"]
    s = clamp((52 - up_pct) / 27 * 100)
    extra = f", 涨停{b['limit_up']}/跌停{b['limit_down']}"
    return s, f"上涨家数占比{up_pct:.0f}%({b['up']}/{b['valid']}{extra})"


def score_limit(b: dict | None) -> tuple[float, str]:
    if not b:
        return 0.0, "未扫描"
    ld, lu = b["limit_down"], b["limit_up"]
    s = 0.0
    if ld >= 5:
        s += 20
    if ld >= 15:
        s += 25
    if ld >= 30:
        s += 30
    if lu > 0 and ld >= lu * 1.5:
        s += 25
    return clamp(s), f"跌停{ld}只 vs 涨停{lu}只"


def score_volume(k: list[list]) -> tuple[float, str]:
    vols = [r[5] for r in k]
    if len(vols) < 7:
        return 0.0, "K线不足"
    chg = k[-1][2] / k[-2][2] - 1
    base5 = sum(vols[-6:-1]) / 5
    ratio = vols[-1] / base5 if base5 > 0 else 1.0
    if chg < 0:
        s = clamp((ratio - 0.95) / 0.55 * 100)
    else:
        s = clamp((ratio - 1.3) / 0.7 * 25)
    return s, f"量能比5日均量{ratio:.2f}x, 指数{chg:+.1%}"


def score_volatility(k: list[list]) -> tuple[float, str]:
    if len(k) < 21:
        return 0.0, "K线不足"
    amps = [(r[3] - r[4]) / k[i - 1][2] * 100 for i, r in enumerate(k)][-20:]
    amp5 = sum(amps[-5:]) / 5
    amp20 = sum(amps) / len(amps)
    gap = abs(k[-1][1] / k[-2][2] - 1) * 100
    s = clamp((amp5 - 1.0) / 1.6 * 60) + clamp((max(gap - amp20, 0)) / 1.2 * 40)
    tag = " ⚠️跳空异动" if gap > max(amp20, 0.6) else ""
    return clamp(s), f"5日均振幅{amp5:.2f}%(基线{amp20:.2f}), 跳空{gap:.2f}%{tag}"


def score_overnight(q: dict[str, dict]) -> tuple[float, str]:
    nd = q.get("usIXIC", {}).get("chg_pct")
    dj = q.get("usDJI", {}).get("chg_pct")
    sp = q.get("usINX", {}).get("chg_pct")
    vals = [v for v in (nd, dj, sp) if v is not None]
    if not vals:
        return 0.0, "无数据"
    avg = sum(vals) / len(vals)
    neg_cnt = sum(1 for v in vals if v < 0)
    s = clamp((-avg - 0.5) / 2.2 * 75 + (neg_cnt * 8 if neg_cnt >= 2 else 0))
    names = {"usIXIC": "纳指", "usDJI": "道指", "usINX": "标普"}
    parts = [f"{names[k]}{q[k]['chg_pct']:+.1f}%"
             for k in ("usIXIC", "usDJI", "usINX") if k in q]
    return s, " ".join(parts)


def fresh(dstr: str, max_age_days: int = 5) -> bool:
    if not dstr:
        return False
    try:
        d = datetime.strptime(dstr[:10], "%Y-%m-%d").date()
        return abs((date.today() - d).days) <= max_age_days
    except ValueError:
        return False


def score_vix(vix: dict | None) -> tuple[float, str]:
    if not vix or not fresh(vix.get("date", "")):
        return 0.0, "无新鲜数据(自动剔除)"
    v = vix["price"]
    if v >= 30:
        s = 85 + min(v - 30, 10)
    elif v >= 25:
        s = 65 + (v - 25) * 4
    elif v >= 20:
        s = 40 + (v - 20) * 5
    elif v >= 15:
        s = (v - 15) * 6
    else:
        s = 0
    return clamp(s), f"VIX={v:.1f} ({vix.get('date','')})"


def score_a50(a50: dict | None) -> tuple[float, str]:
    if not a50 or not a50.get("prev"):
        return 0.0, "无数据"
    if a50.get("date") and not fresh(a50["date"], 4):
        return 0.0, f"数据过期({a50['date']})"
    chg = (a50["price"] / a50["prev"] - 1) * 100
    # A50是高beta领先指标, 跌幅计分要陡: -0.2%起计, -1.6%即满分
    s = clamp((-chg - 0.20) / 1.40 * 100)
    return s, f"A50期指{a50['price']:.0f} {chg:+.2f}%(领先指标)"


def score_hk(q: dict[str, dict]) -> tuple[float, str]:
    hsi = q.get("hkHSI", {}).get("chg_pct")
    tech = q.get("hkHSTECH", {}).get("chg_pct")
    if hsi is None and tech is None:
        return 0.0, "无数据"
    # 恒科是高beta龙头, 权重与斜率都高于恒指
    w = clamp((-hsi - 0.6) / 2.8 * 45 if hsi is not None else 0) \
        + clamp((-tech - 1.0) / 3.5 * 55 if tech is not None else 0)
    parts = []
    if hsi is not None:
        parts.append(f"恒指{hsi:+.1f}%")
    if tech is not None:
        parts.append(f"恒科{tech:+.1f}%")
    return w, " ".join(parts)


def score_fx(usdcnh: dict | None, udi: dict | None) -> tuple[float, str]:
    s, parts = 0.0, []
    if usdcnh and usdcnh.get("prev_raw"):
        chg = usdcnh["chg_pct"]
        s += clamp((chg - 0.06) / 0.30 * 70)
        lvl = usdcnh["price_raw"] / 10000
        parts.append(f"USDCNH{lvl:.4f}({chg:+.2f}%)")
    if udi and udi.get("prev"):
        chg_d = (udi["price"] / udi["prev"] - 1) * 100
        s += clamp((chg_d - 0.15) / 0.55 * 30)
        parts.append(f"美元指数{udi['price']:.1f}({chg_d:+.2f}%)")
    if not parts:
        return 0.0, "无数据"
    return clamp(s), ", ".join(parts)


def score_commodity(gold: dict | None, oil: dict | None) -> tuple[float, str]:
    if not gold or not oil or not gold.get("prev") or not oil.get("prev"):
        return 0.0, "无数据"
    g = (gold["price"] / gold["prev"] - 1) * 100
    o = (oil["price"] / oil["prev"] - 1) * 100
    s = 0.0
    if g > 0.8:
        s += 45
    elif g > 0.4:
        s += 20
    if o < -2.5:
        s += 40
    elif o < -1.2:
        s += 18
    if g > 0.8 and o < -1.2:
        s += 15  # 双重risk-off共振
    return clamp(s), f"伦敦金{g:+.2f}% / 布油{o:+.2f}%"


# ─────────────────────────── aggregation ───────────────────────────
def evaluate(data: dict, preopen: bool = False) -> dict:
    k_sse = data["kline"]["sh000001"]
    if preopen:
        # 盘前预测: 内部因子只允许使用昨日收盘前的已知事实
        k_sse = strip_today(k_sse)
    scores = {
        "trend": score_trend(k_sse),
        "momentum": score_momentum(k_sse),
        "drawdown": score_drawdown(k_sse),
        "breadth": score_breadth(data.get("breadth")),
        "limit": score_limit(data.get("breadth")),
        "volume": score_volume(k_sse),
        "volatility": score_volatility(k_sse),
        "overnight_us": score_overnight(data["quotes"]),
        "vix": score_vix(data.get("vix")),
        "a50": score_a50(data.get("a50")),
        "hk": score_hk(data["quotes"]),
        "fx": score_fx(data.get("usdcnh"), data.get("udi")),
        "commodity": score_commodity(data.get("gold"), data.get("oil")),
    }
    contributions = {}
    used_w = 0.0
    for f, (s, detail) in scores.items():
        skipped = (detail in ("未扫描",) or "无数据" in detail or "K线不足" in detail
                   or "剔除" in detail or "过期" in detail)
        w = 0.0 if skipped else FACTOR_WEIGHTS[f]
        used_w += w
        contributions[f] = {"score": round(s, 1), "weight": w,
                            "contribution": round(s * w, 2),
                            "detail": detail, "skipped": skipped}
    norm = (TOTAL_WEIGHT / used_w) if used_w > 0 else 0
    raw_total = sum(c["contribution"] for c in contributions.values())

    # 共振加成: 核心因子多个同时恶化时,
    # 风险是非线性的 —— 单因子线性加权会低估系统性risk-off日, 人工加成修正。
    # 盘前模式用隔夜美股替代港股(9:26时港股尚未开盘, 无有效涨跌)
    core = ("trend", "breadth", "momentum", "a50", "overnight_us" if preopen else "hk")
    hot = [f for f in core
           if not contributions[f]["skipped"] and contributions[f]["score"] >= 50]
    if len(hot) >= 4:
        bonus = 15.0
    elif len(hot) == 3:
        bonus = 10.0
    elif len(hot) == 2:
        bonus = 5.0
    else:
        bonus = 0.0

    total = min(round(raw_total * norm + bonus, 1), 100.0)
    level_key, name, cn = "LOW", "🟢 LOW", "正常"
    for lv, nm, cnn in LEVELS:
        if total >= lv:
            level_key, name, cn = nm.split()[1], nm, cnn
            break
    return {"total": total, "level": level_key, "level_name": f"{name} {cn}",
            "factors": contributions, "norm": round(norm, 3),
            "confluence_bonus": bonus, "core_hot": hot, "preopen": preopen}


# ─────────────────────────── collect ───────────────────────────
def fetch_remote_basis(data_url: str) -> dict | None:
    """从已部署 API 拉取 A 股市场宽度/涨跌停/上证快照(海外 runner 直连不到中国数据源)。

    接口(/api/market/e-data)返回 {breadth:{valid,up,down,flat,up_pct,down_pct,
    limit_up,limit_down}, sse:{price,change_pct,previous_close,name}, trade_date}。
    """
    for attempt in range(3):
        try:
            req = urllib.request.Request(data_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1.0)
    return None


def collect(fast: bool = False, preopen: bool = False, data_url: str = "") -> dict:
    idx_codes = ["sh000001", "sz399001", "sz399006", "sh000688"]
    ext_codes = ["usDJI", "usIXIC", "usINX", "hkHSI", "hkHSTECH"]
    mode_txt = "盘前预测" if preopen else "实时评估"
    print(f"[1/4] 拉取指数与外围行情({mode_txt})...", file=sys.stderr)
    quotes = tencent_quotes(idx_codes + ext_codes)
    # 腾讯对美股/港股返回的代码字段带不同写法(.IXIC / HSTECH), 归一化到请求代码
    for req, alt in {"usDJI": ".DJI", "usIXIC": ".IXIC", "usINX": ".INX",
                     "hkHSI": "HSI", "hkHSTECH": "HSTECH"}.items():
        if req not in quotes and alt in quotes:
            quotes[req] = quotes[alt]

    # 腾讯实时行情海外不可达时, 用 yfinance 兜底美股/港股涨跌幅
    if not quotes:
        print("      腾讯实时行情不可达, 用 yfinance 兜底外围指数", file=sys.stderr)
        for key in ("usDJI", "usIXIC", "usINX", "hkHSI", "hkHSTECH"):
            q = _yf_history_quote(YF_SYMBOLS.get(key, ""))
            if q and q.get("prev"):
                prev, px = q["prev"], q["price"]
                quotes[key] = {"name": key, "code": key, "price": px,
                               "prev": prev, "chg_pct": (px / prev - 1) * 100}
    kline = {c: index_kline(c) for c in ("sh000001",)}
    print(f"      K线{len(kline['sh000001'])}根, 行情{len(quotes)}个", file=sys.stderr)

    print("[2/4] 外围衍生品...", file=sys.stderr)
    a50 = sina_global("hf_CHA50CFD")
    gold = sina_global("hf_GC")
    oil = sina_global("hf_OIL")
    udi = sina_global("DINIW")
    missing = [n for n, v in [("a50", a50), ("gold", gold), ("oil", oil),
                              ("udi", udi)] if not v]
    if missing:
        # 新浪不可达 → yfinance 兜底
        print(f"      新浪外围源缺失({','.join(missing)}), 用 yfinance 兜底", file=sys.stderr)
        yfd = _yf_fallback()
        a50 = a50 or yfd.get("a50")
        gold = gold or yfd.get("gold")
        oil = oil or yfd.get("oil")
        udi = udi or yfd.get("udi")

    vix_txt = http_get("http://qt.gtimg.cn/q=usVIX")
    vix = None
    if vix_txt and '"' in vix_txt:
        try:
            f = vix_txt.split('"')[1].split("~")
            ts = next((x for x in f if re.match(r"\d{4}-\d{2}-\d{2}", x)), "")
            vix = {"price": float(f[3]), "date": ts[:10]}
        except Exception:
            vix = None
    if vix is None:
        yv = _yf_history_quote(YF_SYMBOLS.get("vix", ""))
        if yv:
            vix = {"price": yv["price"], "date": yv["date"]}

    usdcnh = em_quote_ratio("133.USDCNH")
    if usdcnh is None:
        yc = _yf_history_quote(YF_SYMBOLS.get("usdcnh", ""))
        if yc and yc.get("prev") and yc.get("prev"):
            prev, px = yc["prev"], yc["price"]
            usdcnh = {"price_raw": px * 10000, "prev_raw": prev * 10000,
                      "chg_pct": (px / prev - 1) * 100}

    breadth = None
    # 优先从远程 API 拉取市场宽度/涨跌停(海外 runner); 本地则扫集合竞价快照
    if data_url:
        basis = fetch_remote_basis(data_url)
        if basis and (basis.get("breadth") or {}).get("valid"):
            b = basis["breadth"]
            breadth = {"valid": int(b.get("valid") or 0), "up": int(b.get("up") or 0),
                       "down": int(b.get("down") or 0), "flat": int(b.get("flat") or 0),
                       "up_pct": float(b.get("up_pct") or 0), "down_pct": float(b.get("down_pct") or 0),
                       "limit_up": int(b.get("limit_up") or 0), "limit_down": int(b.get("limit_down") or 0)}
            print(f"[3/4] 远程市场宽度: 涨{breadth['up']}/跌{breadth['down']}/平{breadth['flat']} "
                  f"涨停{breadth['limit_up']}/跌停{breadth['limit_down']}", file=sys.stderr)
    if breadth is None and not fast:
        label = "集合竞价快照" if preopen else "全市场宽度"
        print(f"[3/4] {label}扫描...", file=sys.stderr)
        breadth = market_breadth(load_universe())
    elif breadth is None:
        print("[3/4] 跳过宽度扫描(--fast)", file=sys.stderr)

    print("[4/4] 计算因子...", file=sys.stderr)
    return {"quotes": quotes, "kline": kline, "breadth": breadth,
            "a50": a50, "gold": gold, "oil": oil, "udi": udi,
            "vix": vix, "usdcnh": usdcnh}


# ─────────────────────────── report ───────────────────────────
def render(res: dict, ctx: dict, preopen: bool = False, fast: bool = False) -> str:
    total, level_key = res["total"], res["level"]
    bar_len = 28
    filled = int(total / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = COLORS[level_key]
    if preopen:
        hhmm = now[11:16]
        if fast or not ctx.get("breadth"):
            conf = "🟡置信度中(无竞价快照, 仅隔夜外围+昨日状态)"
        elif "09:25" <= hhmm <= "09:40":
            conf = "🟢置信度高(竞价快照已定型+隔夜外围)"
        else:
            conf = "🟡置信度中(非标准竞价时段)"
        mode_line = f"  模式: 🔮 盘前预测(今日风险预判)   {conf}"
    else:
        mode_line = "  模式: 📡 实时评估(盘中/收盘事实)"
    lines = []
    lines.append("=" * 66)
    lines.append(f"  🛡  策略E · 大盘风险监控  {now}")
    lines.append("=" * 66)
    lines.append("")
    lv_name = next(nm + " " + cn for lv, nm, cn in LEVELS if total >= lv)
    lines.append(f"  综合风险分: {color}{total:>5.1f} / 100  【{lv_name}】\033[0m")
    lines.append(f"  [{bar}]")
    lines.append(f"   0       25      50       75      100")
    lines.append(mode_line)
    if preopen:
        lines.append("  ⚠ 预测基于隔夜外围+集合竞价, 开盘后请用实时模式复核")
    lines.append("")
    lines.append(f"  💡 {ADVICE[level_key]}")
    lines.append("")
    lines.append("  " + "-" * 62)
    lines.append(f"  {'因子':<10}{'状态':<6}{'分':>6}{'权重':>7}{'贡献':>7}  说明")
    lines.append("  " + "-" * 62)
    ranked = sorted(res["factors"].items(),
                    key=lambda kv: -kv[1]["score"] * kv[1]["weight"])
    for f, c in ranked:
        status = " ➖" if c["skipped"] else ("🔴" if c["score"] >= 60
                                            else "🟠" if c["score"] >= 35 else "🟢")
        nm = FACTOR_NAMES[f]
        lines.append(f"  {nm:<10}{status:<6}{c['score']:>6.0f}{c['weight']*100:>6.0f}%"
                     f"{c['contribution']:>7.2f}  {c['detail'][:34]}")
    lines.append("  " + "-" * 62)
    top = [f"{FACTOR_NAMES[f]}({c['contribution']:.1f})" for f, c in
           sorted(res["factors"].items(), key=lambda kv: -kv[1]["contribution"])[:3]
           if not c["skipped"] and c["score"] > 20]
    if top:
        lines.append(f"  ⚠ 主要风险来源: {' > '.join(top)}")
    if res.get("confluence_bonus"):
        hot_names = "、".join(FACTOR_NAMES[f] for f in res.get("core_hot", []))
        lines.append(f"  🔗 共振加成 +{res['confluence_bonus']:.0f}分 "
                     f"(核心因子同时恶化{len(res.get('core_hot', []))}个: {hot_names})")
    b = ctx.get("breadth")
    if b:
        lines.append(f"  📈 市场宽度: 上涨{b['up']} / 下跌{b['down']} / 平{b['flat']}"
                     f"  |  涨停{b['limit_up']} 跌停{b['limit_down']}")
    lines.append("")
    return "\n".join(lines)


def save_result(res: dict, ctx: dict) -> tuple[Path, dict | None]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    latest = OUT_DIR / "latest.json"
    prev = None
    if latest.exists():
        try:
            prev = json.loads(latest.read_text())
        except Exception:
            prev = None
    payload = {
        "strategy": "E",
        "mode": "preopen_forecast" if res.get("preopen") else "live",
        "run_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": date.today().isoformat(),
        "total_score": res["total"],
        "level": res["level"],
        "level_name": res["level_name"],
        "advice": ADVICE[res["level"]],
        "exit_code": EXIT_CODES[res["level"]],
        "weight_normalization": res["norm"],
        "confluence_bonus": res.get("confluence_bonus", 0),
        "core_hot": res.get("core_hot", []),
        "factors": res["factors"],
        "context": {
            "sse": ctx["quotes"].get("000001"),
            "nasdaq": ctx["quotes"].get("usIXIC"),
            "a50": {k: v for k, v in (ctx.get("a50") or {}).items() if k != "fields"},
            "usdcnh": ctx.get("usdcnh"),
            "breadth": ctx.get("breadth"),
        },
    }
    (OUT_DIR / f"market_risk_{now.strftime('%Y%m%d_%H%M%S')}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2))
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        LATEST_MIRROR.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    except OSError:
        pass
    return latest, prev


def publish(url: str, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"publish_strategy_e=ok status={resp.status}", file=sys.stderr)
    except Exception as exc:
        print(f"publish_strategy_e=failed error={exc}", file=sys.stderr)


def run_once(args) -> int:
    data = collect(fast=args.fast, preopen=args.preopen, data_url=args.data_url or "")
    res = evaluate(data, preopen=args.preopen)
    saved, prev = save_result(res, data)
    print(render(res, data, preopen=args.preopen, fast=args.fast))

    # 只要提供了 publish-url 就发布（供 dashboard 盘前预测模块展示），
    # 而不仅在风险升级时发布。
    if args.publish_url:
        with open(saved) as fh:
            publish(args.publish_url, json.load(fh))

    prev_level = (prev or {}).get("level")
    if prev_level and ORDER[res["level"]] > ORDER.get(prev_level, 99):
        print()
        print("\033[1;91m" + "!" * 66)
        print(f"  🚨🚨 策略E 风险等级升级: {prev_level} → {res['level']} "
              f"({res['level_name']}) 🚨🚨")
        print(f"  上次评估: {(prev or {}).get('run_at')}  "
              f"分数{(prev or {}).get('total_score')} → {res['total']}")
        print(f"  {ADVICE[res['level']]}")
        print("!" * 66 + "\033[0m")
    return EXIT_CODES[res["level"]]


def main() -> int:
    ap = argparse.ArgumentParser(description="策略E: 大盘风险监控与预警(多因子)")
    ap.add_argument("--fast", action="store_true", help="跳过全市场宽度扫描")
    ap.add_argument("--preopen", action="store_true",
                    help="盘前预测模式: 内部因子只用昨日收盘事实, "
                         "宽度改用集合竞价快照(9:15-9:25), 预测今日风险")
    ap.add_argument("--watch", type=int, default=0, metavar="MIN",
                    help="循环模式: 每MIN分钟重新评估")
    ap.add_argument("--publish-url", default=None, help="结果POST到该URL")
    ap.add_argument("--data-url", default=None,
                    help="远程A股基础数据接口(市场宽度/涨跌停/上证快照)。"
                         "海外 runner 无法直连新浪/腾讯, 用此中转(如 /api/market/e-data)")
    args = ap.parse_args()

    if args.watch > 0:
        code = 0
        while True:
            try:
                code = run_once(args)
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                print(f"[strategy-e error] {exc}", file=sys.stderr)
            print(f"\n⏱ 下次评估: {args.watch}分钟后 (Ctrl+C退出)", file=sys.stderr)
            time.sleep(args.watch * 60)
    return run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
