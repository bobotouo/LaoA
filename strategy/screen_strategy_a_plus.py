#!/usr/bin/env python3
"""
策略 A+PLUS: MA20/MACD/形态择时 + 流动性过滤 + 打分制
────────────────────────────────────────
改进自策略A(screen_ma_macd_shape.py):
  1. MACD金叉阈值 8→15根(放宽)
  2. 新增流动性过滤: 近5日均成交额≥2亿 + 换手率≥3%
  3. 打分制(满分10)替代三条件AND,避免单条件差一点全否
  4. 数据源优先东财(11字段含成交额/换手率)
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from data_sources.a_share_adapter import AShareDataAdapter, DataSourceUnavailable
from data_sources.free_web_adapter import FreeWebAdapter


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = np.array([0.12, 0.45, 0.82, 1.0, 0.80, 0.43, 0.32, 0.38, 0.47, 0.31, 0.23])
# A-crash + weak-bounce + bottom-consolidation template
# Represents: rally → peak → sharp crash → weak bounce → decline → bottom consolidation
A_CRASH_TEMPLATE = np.array([0.15, 0.42, 0.78, 1.0, 0.32, 0.18, 0.28, 0.35, 0.22, 0.13, 0.10])


def macd_features(close: pd.Series) -> dict[str, float | int | bool]:
    series = pd.to_numeric(close, errors="coerce").dropna().astype(float)
    dif = series.ewm(span=12, adjust=False).mean() - series.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    histogram = dif - dea
    cross_indices = np.flatnonzero((dif.to_numpy()[1:] > dea.to_numpy()[1:]) & (dif.to_numpy()[:-1] <= dea.to_numpy()[:-1])) + 1
    cross_age = int(len(series) - 1 - cross_indices[-1]) if len(cross_indices) else 10_000
    cross_underwater = bool(
        len(cross_indices)
        and dif.iloc[cross_indices[-1]] < 0
        and dea.iloc[cross_indices[-1]] < 0
    )
    return {
        "dif": float(dif.iloc[-1]),
        "dea": float(dea.iloc[-1]),
        "macd_hist": float(histogram.iloc[-1]),
        "macd_cross_age": cross_age,
        "macd_underwater_cross": cross_underwater,
        "macd_ok": bool(cross_age <= 15 and cross_underwater and histogram.iloc[-1] > 0),
    }


def shape_features(
    close: pd.Series,
    template: np.ndarray = DEFAULT_TEMPLATE,
    mode: str = "reference",
) -> dict[str, float | bool]:
    series = pd.to_numeric(close, errors="coerce").dropna().astype(float)
    if len(series) < 50:
        return {"shape_score": float("nan"), "shape_ok": False}
    series = series.tail(80)
    values = series.to_numpy()
    length = len(values)

    # ---- template correlation ----
    points = values[np.linspace(0, length - 1, len(template)).astype(int)]
    normalized = (points - points.min()) / max(points.max() - points.min(), 1e-12)
    correlation = float(np.corrcoef(normalized, template)[0, 1])

    # ---- main peak (前高) in first 45% ----
    peak_limit = max(5, int(length * 0.45))
    peak_index = int(np.argmax(values[:peak_limit]))
    peak_price = values[peak_index]

    # ---- crash trough (暴跌低点) after peak, before 80% ----
    trough_start = peak_index + max(3, int(length * 0.05))
    trough_limit = min(length - 5, int(length * 0.80))
    if trough_start >= trough_limit:
        trough_start = peak_index + 1
        trough_limit = length - 5
    trough_index = trough_start + int(np.argmin(values[trough_start:trough_limit]))
    trough_price = values[trough_index]

    # ---- weak bounce peak (次高点) after crash trough ----
    bounce_start = trough_index + max(3, int(length * 0.04))
    bounce_limit = min(length - 5, int(length * 0.92))
    if bounce_start >= bounce_limit:
        bounce_start = trough_index + 1
        bounce_limit = length - 3
    bounce_index = bounce_start + int(np.argmax(values[bounce_start:bounce_limit]))
    bounce_price = values[bounce_index]

    # ---- second low after weak bounce ----
    second_low_start = bounce_index + max(2, int(length * 0.03))
    second_low_limit = length
    if second_low_start >= second_low_limit:
        second_low_start = bounce_index + 1
    second_low_index = second_low_start + int(np.argmin(values[second_low_start:second_low_limit]))
    second_low_price = values[second_low_index]

    # ---- current price vs recent low ----
    current_price = values[-1]
    recent_low = min(values[-15:]) if len(values) >= 15 else values[-1]
    near_bottom = bool(current_price <= recent_low * 1.05)

    # ---- metrics ----
    early_rise = peak_price / values[0] - 1
    peak_position = float(peak_index / length)
    trough_position = float(trough_index / length)
    bounce_position = float(bounce_index / length)
    second_low_position = float(second_low_index / length)

    post_peak_drawdown = trough_price / peak_price - 1  # crash depth
    mid_rebound = bounce_price / trough_price - 1         # weak bounce strength
    final_pullback = current_price / bounce_price - 1     # decline from bounce to now

    # crash speed: bars from peak to trough vs bars from start to peak
    crash_bars = trough_index - peak_index
    rise_bars = max(peak_index, 1)
    crash_speed_ratio = crash_bars / rise_bars

    # ---- consolidation: last 8 bars range vs overall range ----
    overall_range = values.max() - values.min()
    recent_8_range = values[-8:].max() - values[-8:].min()
    consolidation_pct = float(recent_8_range / overall_range) if overall_range > 0 else 1.0

    if mode == "a-crash":
        shape_ok = bool(
            correlation >= 0.35
            and early_rise >= 0.12
            and 0.15 <= peak_position <= 0.48
            and post_peak_drawdown <= -0.20
            and 0.03 <= mid_rebound <= 0.40
            and final_pullback <= -0.02
            and crash_speed_ratio <= 3.0
            and consolidation_pct <= 0.12
            and near_bottom
        )
    elif mode == "reference":
        # Reference chart: rally, first-half peak, drawdown, rebound, final pullback.
        shape_ok = bool(
            correlation >= 0.45
            and early_rise >= 0.08
            and 0.15 <= peak_position <= 0.48
            and post_peak_drawdown <= -0.15
            and 0.10 <= mid_rebound <= 0.40
            and final_pullback <= -0.03
        )
    else:
        raise ValueError(f"unsupported shape mode: {mode}")

    return {
        "shape_score": correlation,
        "shape_ok": shape_ok,
        "early_rise": early_rise,
        "peak_position": peak_position,
        "trough_position": trough_position,
        "bounce_position": bounce_position,
        "second_low_position": second_low_position,
        "post_peak_drawdown": post_peak_drawdown,
        "mid_rebound": mid_rebound,
        "final_pullback": final_pullback,
        "crash_speed_ratio": crash_speed_ratio,
        "consolidation_pct": consolidation_pct,
        "near_bottom": near_bottom,
    }


def liquidity_features(group: pd.DataFrame) -> dict[str, float]:
    """从日K计算流动性指标: 近5日均成交额(亿)、近5日均量比、最新换手率(%)。"""
    g = group.sort_values("date").tail(20)
    vol = pd.to_numeric(g.get("volume"), errors="coerce").dropna()
    amt = pd.to_numeric(g.get("amount"), errors="coerce").dropna()
    turnover = pd.to_numeric(g.get("turnover_rate"), errors="coerce").dropna()
    result: dict[str, float] = {
        "avg_amount_5d": 0.0,
        "volume_ratio_5d": 0.0,
        "turnover_rate": 0.0,
    }
    if len(amt) >= 5:
        recent_amt = amt.tail(5)
        result["avg_amount_5d"] = float(recent_amt.mean())  # 原始单位: 元
    if len(vol) >= 10:
        recent_vol = vol.tail(5).mean()
        prior_vol = vol.iloc[-10:-5].mean()
        result["volume_ratio_5d"] = float(recent_vol / prior_vol) if prior_vol > 0 else 0.0
    if not turnover.empty:
        result["turnover_rate"] = float(turnover.iloc[-1])
    return result


def score_candidate(row: dict) -> int:
    """打分制(满分10): 替代原来的三条件AND。
    - MA20趋势加速: 2分
    - MACD水下金叉+红柱: 3分 (金叉<=15根得满分, 16-25根得1分)
    - 形态匹配: 3分 (shape_ok得满分, shape_score>=0.7得2分, >=0.6得1分)
    - 流动性: 2分 (成交额>=2亿+量比>=1.0得满分, 仅成交额>=2亿得1分)
    """
    score = 0
    # MA20
    if row.get("ma20_ok"):
        score += 2
    # MACD
    cross_age = row.get("macd_cross_age", 99999)
    if row.get("macd_underwater_cross") and row.get("macd_hist", 0) > 0:
        if cross_age <= 15:
            score += 3
        elif cross_age <= 25:
            score += 1
    # 形态
    if row.get("shape_ok"):
        score += 3
    elif row.get("shape_score", 0) >= 0.7:
        score += 2
    elif row.get("shape_score", 0) >= 0.6:
        score += 1
    # 流动性
    avg_amt = row.get("avg_amount_5d", 0) / 1e8  # 元 -> 亿
    vr = row.get("volume_ratio_5d", 0)
    if avg_amt >= 2 and vr >= 1.0:
        score += 2
    elif avg_amt >= 2:
        score += 1
    return score


def screen_history(
    history: pd.DataFrame,
    universe: pd.DataFrame,
    max_price: float = 200.0,
    shape_mode: str = "reference",
    min_amount_5d: float = 2.0,
    min_turnover: float = 3.0,
) -> pd.DataFrame:
    if history.empty:
        return pd.DataFrame()
    names = universe[[column for column in ("full_code", "name") if column in universe.columns]].drop_duplicates()
    rows: list[dict[str, object]] = []
    for code, group in history.groupby("code", sort=False):
        group = group.sort_values("date").dropna(subset=["close"])
        if len(group) < 50:
            continue
        close = group["close"].astype(float)
        latest_price = float(close.iloc[-1])
        name_rows = names[names["full_code"].astype(str) == str(code)]
        name = str(name_rows["name"].iloc[0]) if not name_rows.empty else ""
        if latest_price > max_price or any(marker in name.upper() for marker in ("ST", "退")):
            continue
        ma20 = close.rolling(20).mean()
        ma5_slope = float(ma20.iloc[-1] / ma20.iloc[-6] - 1)
        previous_slope = float(ma20.iloc[-6] / ma20.iloc[-11] - 1)
        technical = macd_features(close)
        template = A_CRASH_TEMPLATE if shape_mode == "a-crash" else DEFAULT_TEMPLATE
        shape = shape_features(close, template=template, mode=shape_mode)
        liq = liquidity_features(group)
        rows.append(
            {
                "full_code": code,
                "name": name,
                "price": latest_price,
                "data_date": group["date"].iloc[-1],
                "bars": len(group),
                "ma20_slope_5d": ma5_slope,
                "ma20_previous_slope_5d": previous_slope,
                "ma20_ok": bool(ma5_slope > 0 and ma5_slope > previous_slope),
                **technical,
                **shape,
                **liq,
            }
        )
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    # 流动性过滤: 近5日均成交额 >= min_amount_5d 亿
    result["avg_amount_5d_yi"] = result["avg_amount_5d"] / 1e8
    mask_liq = result["avg_amount_5d_yi"] >= min_amount_5d
    # 换手率过滤: 仅当数据源提供了换手率时才应用（turnover_rate > 0 表示有真实值）；
    # free-web 源（腾讯 newfqkline）没有换手率字段，跳过此条件。
    if (result["turnover_rate"] > 0).any():
        mask_liq = mask_liq & (result["turnover_rate"] >= min_turnover)
    result = result[mask_liq].copy()
    if result.empty:
        return result
    # 打分制替代 AND
    records = result.to_dict(orient="records")
    result["score"] = [score_candidate(r) for r in records]
    result["match"] = result["score"] >= 7
    return result.sort_values(["score", "shape_score"], ascending=False).reset_index(drop=True)


def filter_universe(
    universe: pd.DataFrame,
    max_price: float,
    exclude_bse: bool = False,
    exclude_star: bool = False,
) -> pd.DataFrame:
    filtered = universe.copy()
    if "price" in filtered.columns:
        prices = pd.to_numeric(filtered["price"], errors="coerce")
        filtered = filtered[(prices > 0) & (prices <= max_price)]
    if "name" in filtered.columns:
        names = filtered["name"].astype(str).str.upper()
        filtered = filtered[~names.str.contains("ST|退", regex=True)]
    if "full_code" in filtered.columns:
        codes = filtered["full_code"].astype(str).str.upper()
        if exclude_bse:
            filtered = filtered[
                ~codes.str.endswith(".BJ")
                & ~codes.str.startswith(("4", "8", "9"))
            ]
            codes = filtered["full_code"].astype(str).str.upper()
        if exclude_star:
            filtered = filtered[~codes.str.startswith("688")]
    return filtered.reset_index(drop=True)

def _publish_top_picks(publish_url: str, result: pd.DataFrame) -> None:
    """POST the top 10 A+ picks (by score) to the dashboard after close.

    Picks mirror the fields the dashboard's /api/strategy/a-plus endpoint
    expects; failures are logged without failing the screening run.
    """
    import urllib.request

    top_cols = [
        "full_code", "name", "price", "score", "shape_score",
        "ma20_ok", "macd_ok", "shape_ok", "avg_amount_5d_yi",
        "turnover_rate", "data_date",
    ]
    top_cols = [c for c in top_cols if c in result.columns]
    top = result.sort_values("score", ascending=False).head(10)
    payload = []
    for _, row in top.iterrows():
        item = {}
        for key in top_cols:
            value = row.get(key)
            if isinstance(value, (bool, int, float)) or value is None:
                item[key] = value
            else:
                item[key] = str(value)
        payload.append(item)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        publish_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"publish_a_plus=ok count={len(payload)} status={response.status}")
    except Exception as exc:
        print(f"publish_a_plus=failed error={exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Free A-share MA20/MACD/shape screener.")
    parser.add_argument("--start", default=(date.today() - timedelta(days=140)).isoformat())
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--max-price", type=float, default=200.0)
    parser.add_argument("--limit", type=int, default=0, help="0 = all stocks")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--exclude-bse", action="store_true", help="exclude Beijing Stock Exchange")
    parser.add_argument("--exclude-star", action="store_true", help="exclude STAR Market (688xxx)")
    parser.add_argument(
        "--shape-mode",
        choices=("reference", "a-crash"),
        default="reference",
        help="reference follows the supplied chart; a-crash requires bottom consolidation",
    )
    parser.add_argument(
        "--source",
        choices=("router", "free-web", "finshare"),
        default="router",
        help="router uses configured fallbacks; free-web uses Sina/Tencent/EastMoney directly; finshare uses finshare CLI (has amount/turnover)",
    )
    parser.add_argument("--min-amount-5d", type=float, default=2.0, help="近5日均成交额下限(亿元)")
    parser.add_argument("--min-turnover", type=float, default=3.0, help="换手率下限(百分比)")
    parser.add_argument(
        "--publish-url",
        default=None,
        help="POST 当日 A+ 前10标的到该 URL（例：https://lao-a.bobotou118.dpdns.org/api/strategy/a-plus）",
    )
    args = parser.parse_args()

    if args.source == "free-web":
        adapter = FreeWebAdapter()
    elif args.source == "finshare":
        # 股票列表用 Sina，历史数据用 Finshare（含 amount/turnover）
        import concurrent.futures
        from data_sources.finshare_adapter import FinshareAdapter
        _list_adapter = FreeWebAdapter()
        _hist_adapter = FinshareAdapter()
        class _FinshareCombo:
            def fetch_stock_list(self, **kw):
                return _list_adapter.fetch_stock_list(**kw)
            def fetch_market_history(self, start, end, adjust, codes):
                frames = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                    jobs = {ex.submit(_hist_adapter.fetch_kline, c, start=start, end=end, period='daily', adjust=adjust): c for c in codes}
                    for i, f in enumerate(concurrent.futures.as_completed(jobs), 1):
                        try:
                            df = f.result()
                            if not df.empty:
                                frames.append(df)
                        except:
                            pass
                        if i % 500 == 0:
                            print(f'finshare_history={i}/{len(jobs)}', file=sys.stderr, flush=True)
                import pandas as pd
                return pd.concat(frames, ignore_index=True).sort_values(['code','date']).reset_index(drop=True) if frames else pd.DataFrame()
        adapter = _FinshareCombo()
    else:
        adapter = AShareDataAdapter()
    try:
        universe = adapter.fetch_stock_list(limit=args.limit, refresh=True)
        universe = filter_universe(
            universe,
            max_price=args.max_price,
            exclude_bse=args.exclude_bse,
            exclude_star=args.exclude_star,
        )
        codes = universe["full_code"].astype(str).drop_duplicates().tolist()
        history = adapter.fetch_market_history(
            start=args.start, end=args.end, adjust="qfq", codes=codes
        )
    except DataSourceUnavailable as exc:
        print(f"data_source_error={exc}", file=sys.stderr)
        return 2
    result = screen_history(
        history,
        universe,
        max_price=args.max_price,
        shape_mode=args.shape_mode,
        min_amount_5d=args.min_amount_5d,
        min_turnover=args.min_turnover,
    )
    payload = result.to_dict(orient="records")
    output = json.dumps(payload, ensure_ascii=False, default=str, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    matches = int(result["match"].sum()) if not result.empty else 0
    if args.publish_url and not result.empty:
        # 允许数据源最新日期比 --end 最晚 3 个自然日
        # （覆盖盘中触发、节假日、数据源延迟等场景）
        from datetime import date as _date
        data_dates = {
            str(row.get("data_date", ""))[:10]
            for row in result.to_dict(orient="records")
            if row.get("data_date")
        }
        if data_dates:
            max_date = max(data_dates)
            end_dt = _date.fromisoformat(args.end)
            data_dt = _date.fromisoformat(max_date)
            if max_date >= args.end or abs((end_dt - data_dt).days) <= 3:
                _publish_top_picks(args.publish_url, result)
            else:
                print(f"publish_a_plus=skipped data_date={max_date} too old vs end={args.end} (diff={abs((end_dt - data_dt).days)}d)")
        else:
            print(f"publish_a_plus=skipped no data_date in result")
    print(f"universe={len(codes)} history_codes={history['code'].nunique()} liquidity_passed={len(result)} matches={matches}")
    cols = ["score", "full_code", "name", "price", "shape_score", "ma20_ok", "macd_ok", "shape_ok", "avg_amount_5d_yi", "volume_ratio_5d", "turnover_rate", "match"]
    cols = [c for c in cols if c in result.columns]
    print(result[cols].head(30).to_string(index=False) if not result.empty else "no candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
