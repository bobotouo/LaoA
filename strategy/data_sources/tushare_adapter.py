from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import json
import os
import time
from typing import Any
from urllib.request import Request, urlopen

import pandas as pd


TUSHARE_ENDPOINT = "https://api.tushare.pro"


@dataclass(frozen=True)
class TushareConfig:
    token: str = ""
    endpoint: str = TUSHARE_ENDPOINT
    timeout_seconds: int = 30
    request_interval_seconds: float = 0.12


class TushareAdapter:
    """Small Tushare Pro adapter that does not require the tushare SDK."""

    def __init__(self, config: TushareConfig | None = None) -> None:
        self.config = config or TushareConfig(token=os.getenv("TUSHARE_TOKEN", "").strip())

    @property
    def available(self) -> bool:
        return bool(self.config.token)

    def fetch_stock_list(
        self,
        market: str = "all",
        limit: int = 0,
        as_of: date | None = None,
    ) -> pd.DataFrame:
        self._require_token()
        as_of = as_of or date.today()
        basic = self._query(
            "stock_basic",
            {"exchange": "", "list_status": "L"},
            "ts_code,symbol,name,area,industry,market,exchange,list_status,list_date",
        )
        trade_date = self._latest_trade_date(as_of)
        daily = self._query(
            "daily",
            {"trade_date": trade_date.strftime("%Y%m%d")},
            "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
        )
        if basic.empty or daily.empty:
            return pd.DataFrame()

        df = basic.merge(daily, on="ts_code", how="inner")
        if market == "sh":
            df = df[df["ts_code"].astype(str).str.endswith(".SH")]
        elif market == "sz":
            df = df[df["ts_code"].astype(str).str.endswith(".SZ")]

        df = df.rename(
            columns={
                "symbol": "code",
                "pre_close": "prev_close",
                "pct_chg": "change_pct",
                "vol": "volume",
            }
        )
        for column in (
            "open",
            "high",
            "low",
            "close",
            "prev_close",
            "change",
            "change_pct",
            "volume",
            "amount",
        ):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        # Tushare amount is in thousands of yuan; the project contract uses yuan.
        df["amount"] = df["amount"] * 1000.0
        df["price"] = df["close"]
        df["full_code"] = df["ts_code"].astype(str)
        df["data_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
        df["data_source"] = "tushare"
        df["market"] = df["full_code"].map(lambda code: 1 if code.endswith(".SH") else 0)
        df = df.sort_values("change_pct", ascending=False, na_position="last")
        if limit > 0:
            df = df.head(limit)
        return df.reset_index(drop=True)

    def fetch_kline(
        self,
        code: str,
        start: str,
        end: str,
        period: str = "daily",
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        self._require_token()
        if period != "daily":
            raise ValueError("TushareAdapter currently supports daily K-line only")
        if adjust not in {"none", "qfq", "hfq"}:
            raise ValueError(f"unsupported adjustment: {adjust}")

        ts_code = self._normalize_code(code)
        params = {
            "ts_code": ts_code,
            "start_date": start.replace("-", ""),
            "end_date": end.replace("-", ""),
        }
        df = self._query(
            "daily",
            params,
            "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
        )
        if df.empty:
            return df

        price_columns = ["open", "high", "low", "close", "pre_close"]
        for column in (*price_columns, "vol", "amount"):
            df[column] = pd.to_numeric(df[column], errors="coerce")

        actual_adjustment = "none"
        if adjust != "none":
            factors = self._query("adj_factor", params, "ts_code,trade_date,adj_factor")
            if factors.empty:
                raise RuntimeError(f"Tushare returned no adjustment factors for {ts_code}")
            factors["adj_factor"] = pd.to_numeric(factors["adj_factor"], errors="coerce")
            df = df.merge(factors, on=["ts_code", "trade_date"], how="inner")
            ordered_factors = df.sort_values("trade_date")["adj_factor"].dropna()
            if ordered_factors.empty:
                raise RuntimeError(f"Tushare returned invalid adjustment factors for {ts_code}")
            base_factor = ordered_factors.iloc[-1] if adjust == "qfq" else ordered_factors.iloc[0]
            ratio = df["adj_factor"] / float(base_factor)
            for column in price_columns:
                df[column] = df[column] * ratio
            actual_adjustment = adjust

        df = df.rename(
            columns={
                "trade_date": "date",
                "pre_close": "prev_close",
                "pct_chg": "change_pct",
                "vol": "volume",
            }
        )
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
        df["amount"] = df["amount"] * 1000.0
        df["code"] = ts_code
        df["data_source"] = "tushare"
        df["adjustment"] = actual_adjustment
        return df.sort_values("date").reset_index(drop=True)

    def fetch_market_history(
        self,
        start: str,
        end: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """Fetch the full A-share daily history by trade date, not by stock."""
        self._require_token()
        if adjust not in {"none", "qfq", "hfq"}:
            raise ValueError(f"unsupported adjustment: {adjust}")
        calendar = self._query(
            "trade_cal",
            {
                "exchange": "SSE",
                "start_date": start.replace("-", ""),
                "end_date": end.replace("-", ""),
                "is_open": "1",
            },
            "exchange,cal_date,is_open,pretrade_date",
        )
        trade_dates = sorted(calendar.get("cal_date", pd.Series(dtype=str)).astype(str).tolist())
        if not trade_dates:
            return pd.DataFrame()

        daily_frames: list[pd.DataFrame] = []
        factor_frames: list[pd.DataFrame] = []
        for trade_date in trade_dates:
            daily = self._query(
                "daily",
                {"trade_date": trade_date},
                "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
            )
            if not daily.empty:
                daily_frames.append(daily)
            if adjust != "none":
                factors = self._query(
                    "adj_factor",
                    {"trade_date": trade_date},
                    "ts_code,trade_date,adj_factor",
                )
                if not factors.empty:
                    factor_frames.append(factors)
            if self.config.request_interval_seconds > 0:
                time.sleep(self.config.request_interval_seconds)

        if not daily_frames:
            return pd.DataFrame()
        df = pd.concat(daily_frames, ignore_index=True)
        price_columns = ["open", "high", "low", "close", "pre_close"]
        for column in (*price_columns, "vol", "amount"):
            df[column] = pd.to_numeric(df[column], errors="coerce")

        actual_adjustment = "none"
        if adjust != "none":
            if not factor_frames:
                raise RuntimeError("Tushare returned no market adjustment factors")
            factors = pd.concat(factor_frames, ignore_index=True)
            factors["adj_factor"] = pd.to_numeric(factors["adj_factor"], errors="coerce")
            df = df.merge(factors, on=["ts_code", "trade_date"], how="inner")
            df = df.sort_values(["ts_code", "trade_date"])
            if adjust == "qfq":
                base_factor = df.groupby("ts_code")["adj_factor"].transform("last")
            else:
                base_factor = df.groupby("ts_code")["adj_factor"].transform("first")
            ratio = df["adj_factor"] / base_factor
            for column in price_columns:
                df[column] = df[column] * ratio
            actual_adjustment = adjust

        df = df.rename(
            columns={
                "trade_date": "date",
                "pre_close": "prev_close",
                "pct_chg": "change_pct",
                "vol": "volume",
            }
        )
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
        df["amount"] = df["amount"] * 1000.0
        df["code"] = df["ts_code"].astype(str)
        df["data_source"] = "tushare"
        df["adjustment"] = actual_adjustment
        return df.sort_values(["code", "date"]).reset_index(drop=True)

    def _latest_trade_date(self, as_of: date) -> date:
        calendar = self._query(
            "trade_cal",
            {
                "exchange": "SSE",
                "start_date": (as_of - timedelta(days=14)).strftime("%Y%m%d"),
                "end_date": as_of.strftime("%Y%m%d"),
                "is_open": "1",
            },
            "exchange,cal_date,is_open,pretrade_date",
        )
        if calendar.empty:
            raise RuntimeError("Tushare returned no open trading dates")
        latest = pd.to_datetime(calendar["cal_date"], format="%Y%m%d", errors="coerce").max()
        if pd.isna(latest):
            raise RuntimeError("Tushare returned invalid trading dates")
        return latest.date()

    def _query(self, api_name: str, params: dict[str, Any], fields: str) -> pd.DataFrame:
        body = json.dumps(
            {
                "api_name": api_name,
                "token": self.config.token,
                "params": params,
                "fields": fields,
            }
        ).encode("utf-8")
        request = Request(
            self.config.endpoint,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "ai-stock-cl/1.0"},
            method="POST",
        )
        with urlopen(request, timeout=self.config.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if int(payload.get("code", -1)) != 0:
            raise RuntimeError(f"Tushare {api_name} failed: {payload.get('msg', 'unknown error')}")
        data = payload.get("data") or {}
        return pd.DataFrame(data.get("items") or [], columns=data.get("fields") or [])

    def _require_token(self) -> None:
        if not self.available:
            raise RuntimeError("TUSHARE_TOKEN is not configured")

    @staticmethod
    def _normalize_code(code: str) -> str:
        text = str(code).strip().upper()
        if "." in text:
            return text
        if text.startswith(("600", "601", "603", "605", "688")):
            return f"{text}.SH"
        if text.startswith(("8", "9")):
            return f"{text}.BJ"
        return f"{text}.SZ"
