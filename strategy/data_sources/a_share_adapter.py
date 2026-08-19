from __future__ import annotations

from datetime import date, datetime, timedelta
import os
from typing import Any

import pandas as pd

from data_sources.finshare_adapter import FinshareAdapter
from data_sources.baostock_adapter import BaoStockAdapter
from data_sources.free_web_adapter import FreeWebAdapter
from data_sources.tdx_adapter import TdxAdapter
from data_sources.tushare_adapter import TushareAdapter


class DataSourceUnavailable(RuntimeError):
    """Raised when no configured provider can return usable market data."""


class AShareDataAdapter:
    """Route A-share requests across free sources and optional Tushare.

    BaoStock supplies free adjusted history, Finshare keeps its own
    EastMoney/BaoStock/Tencent fallback, and TDX supplies realtime snapshots
    plus a final unadjusted fallback. Tushare is only an optional accelerator.
    """

    def __init__(
        self,
        tushare: TushareAdapter | None = None,
        baostock: BaoStockAdapter | None = None,
        free_web: FreeWebAdapter | None = None,
        finshare: FinshareAdapter | None = None,
        tdx: TdxAdapter | None = None,
        max_staleness_days: int | None = None,
    ) -> None:
        self.tushare = tushare or TushareAdapter()
        self.baostock = baostock or BaoStockAdapter()
        self.free_web = free_web or FreeWebAdapter()
        self.finshare = finshare or FinshareAdapter()
        self.tdx = tdx or TdxAdapter()
        configured_staleness = os.getenv("A_SHARE_MAX_STALENESS_DAYS", "7")
        self.max_staleness_days = (
            max_staleness_days if max_staleness_days is not None else int(configured_staleness)
        )

    def fetch_stock_list(
        self,
        market: str = "all",
        limit: int = 120,
        refresh: bool = False,
    ) -> pd.DataFrame:
        failures: list[str] = []
        if self.tushare.available:
            try:
                df = self.tushare.fetch_stock_list(market=market, limit=limit)
                if not df.empty:
                    self._require_fresh(df, "data_date", "Tushare stock list")
                    return df
            except Exception as exc:
                failures.append(f"tushare: {exc}")

        try:
            df = self.finshare.fetch_stock_list(market=market, limit=limit, refresh=refresh)
            if not df.empty:
                self._require_fresh(df, "data_date", "Finshare stock list")
                return df
            failures.append("finshare: empty response")
        except Exception as exc:
            failures.append(f"finshare: {exc}")

        # BaoStock has no realtime quote, but its free basic list is still
        # useful for bulk-history jobs when quote providers are unavailable.
        try:
            basic = self.baostock.fetch_stock_list(market=market, limit=limit)
            if not basic.empty:
                return basic
            failures.append("baostock: empty response")
        except Exception as exc:
            failures.append(f"baostock: {exc}")

        try:
            df = self.free_web.fetch_stock_list(market=market, limit=limit, refresh=refresh)
            if not df.empty:
                self._require_fresh(df, "data_date", "Sina stock list")
                return df
            failures.append("sina: empty response")
        except Exception as exc:
            failures.append(f"sina: {exc}")

        raise DataSourceUnavailable("stock list unavailable\n" + "\n".join(failures))

    def fetch_kline(
        self,
        code: str,
        start: str,
        end: str,
        period: str = "daily",
        adjust: str = "qfq",
        refresh: bool = False,
    ) -> pd.DataFrame:
        failures: list[str] = []
        if period == "daily" and self.tushare.available:
            try:
                df = self.tushare.fetch_kline(code, start, end, period=period, adjust=adjust)
                if not df.empty:
                    self._require_fresh(df, "date", f"Tushare kline {code}", expected=end)
                    return df
            except Exception as exc:
                failures.append(f"tushare: {exc}")

        try:
            df = self.baostock.fetch_kline(
                code,
                start=start,
                end=end,
                adjust=adjust,
                refresh=refresh,
            )
            if not df.empty:
                self._require_fresh(df, "date", f"BaoStock kline {code}", expected=end)
                return df
        except Exception as exc:
            failures.append(f"baostock: {exc}")

        try:
            df = self.finshare.fetch_kline(
                code,
                start=start,
                end=end,
                period=period,
                adjust=adjust,
                refresh=refresh,
            )
            if not df.empty:
                self._require_fresh(df, "date", f"Finshare kline {code}", expected=end)
                return df
        except Exception as exc:
            failures.append(f"finshare: {exc}")

        if period == "daily":
            try:
                df = self.tdx.fetch_kline(code, start=start, end=end, refresh=refresh)
                if not df.empty:
                    self._require_fresh(df, "date", f"TDX kline {code}", expected=end)
                    return df
            except Exception as exc:
                failures.append(f"tdx: {exc}")

        try:
            df = self.free_web.fetch_kline(
                code, start=start, end=end, adjust=adjust, refresh=refresh
            )
            if not df.empty:
                self._require_fresh(df, "date", f"EastMoney kline {code}", expected=end)
                return df
        except Exception as exc:
            failures.append(f"eastmoney: {exc}")

        raise DataSourceUnavailable(
            f"historical data unavailable for {code}\n" + "\n".join(failures)
        )

    def fetch_market_history(
        self,
        start: str,
        end: str,
        adjust: str = "qfq",
        codes: list[str] | None = None,
        market: str = "all",
        limit: int = 0,
    ) -> pd.DataFrame:
        """Fetch full-market history with Tushare or free BaoStock batches."""
        if self.tushare.available:
            try:
                df = self.tushare.fetch_market_history(start=start, end=end, adjust=adjust)
                if df.empty:
                    raise RuntimeError("Tushare returned empty full-market history")
                self._require_fresh(df, "date", "Tushare full-market history", expected=end)
                return df
            except Exception as exc:
                tushare_failure = f"tushare: {exc}"
        else:
            tushare_failure = "tushare: TUSHARE_TOKEN is not configured"

        try:
            codes = codes or self._codes_from_stock_list(market=market, limit=limit)
            if not codes:
                raise RuntimeError("no stock codes available for BaoStock batch")
            df = self.baostock.fetch_market_history(codes, start=start, end=end, adjust=adjust)
            if df.empty:
                raise RuntimeError("BaoStock returned empty full-market history")
            self._require_fresh(df, "date", "BaoStock full-market history", expected=end)
            return df
        except Exception as exc:
            baostock_failure = f"baostock: {exc}"

        try:
            codes = codes or self._codes_from_stock_list(market=market, limit=limit)
            if not codes:
                raise RuntimeError("no stock codes available for EastMoney batch")
            df = self.free_web.fetch_market_history(codes, start=start, end=end, adjust=adjust)
            if df.empty:
                raise RuntimeError("EastMoney returned empty full-market history")
            self._require_fresh(df, "date", "EastMoney full-market history", expected=end)
            return df
        except Exception as exc:
            raise DataSourceUnavailable(
                "full-market history unavailable\n"
                f"{tushare_failure}\n{baostock_failure}\neastmoney: {exc}"
            ) from exc

    def _codes_from_stock_list(self, market: str, limit: int) -> list[str]:
        try:
            df = self.finshare.fetch_stock_list(market=market, limit=limit, refresh=False)
            if not df.empty and "full_code" in df.columns:
                return df["full_code"].astype(str).drop_duplicates().tolist()
        except Exception:
            pass
        try:
            df = self.baostock.fetch_stock_list(market=market, limit=limit)
            if not df.empty and "full_code" in df.columns:
                return df["full_code"].astype(str).drop_duplicates().tolist()
        except Exception:
            pass
        try:
            df = self.free_web.fetch_stock_list(market=market, limit=limit, refresh=False)
            if not df.empty and "full_code" in df.columns:
                return df["full_code"].astype(str).drop_duplicates().tolist()
        except Exception:
            pass
        return []

    def fetch_batch_snapshots(self, codes: list[str]) -> pd.DataFrame:
        failures: list[str] = []
        try:
            df = self.tdx.fetch_batch_snapshots(codes)
            if not df.empty:
                return self._normalize_snapshot(df)
        except Exception as exc:
            failures.append(f"tdx: {exc}")

        try:
            df = self.finshare.fetch_batch_snapshots(codes)
            if not df.empty:
                return self._normalize_snapshot(df)
        except Exception as exc:
            failures.append(f"finshare: {exc}")

        raise DataSourceUnavailable("realtime snapshot unavailable\n" + "\n".join(failures))

    def fetch_minutely(
        self,
        code: str,
        start: str,
        end: str,
        freq: int = 5,
        adjust: str = "qfq",
        refresh: bool = False,
    ) -> pd.DataFrame:
        df = self.finshare.fetch_minutely(
            code,
            start=start,
            end=end,
            freq=freq,
            adjust=adjust,
            refresh=refresh,
        )
        if df.empty:
            raise DataSourceUnavailable(f"minute data unavailable for {code}")
        return df

    def _require_fresh(
        self,
        frame: pd.DataFrame,
        date_column: str,
        label: str,
        expected: str | None = None,
    ) -> None:
        if date_column not in frame.columns:
            raise RuntimeError(f"{label} has no {date_column} column")
        dates = pd.to_datetime(frame[date_column], errors="coerce").dropna()
        if dates.empty:
            raise RuntimeError(f"{label} has no valid dates")
        latest = dates.max().date()
        expected_date = datetime.strptime(expected, "%Y-%m-%d").date() if expected else date.today()
        if latest < expected_date - timedelta(days=self.max_staleness_days):
            raise RuntimeError(
                f"{label} is stale: latest={latest.isoformat()} "
                f"expected>={expected_date - timedelta(days=self.max_staleness_days)}"
            )

    @staticmethod
    def _normalize_snapshot(frame: pd.DataFrame) -> pd.DataFrame:
        df = frame.copy()
        if "code" not in df.columns and "full_code" in df.columns:
            df["code"] = df["full_code"]
        if "last_price" in df.columns:
            df["price"] = pd.to_numeric(df["last_price"], errors="coerce")
        if "prev_close" in df.columns and "price" in df.columns:
            df["change_pct"] = (
                (df["price"] / pd.to_numeric(df["prev_close"], errors="coerce") - 1.0) * 100.0
            )
        df["data_date"] = pd.Timestamp.now().normalize()
        return df


# Existing callers can migrate incrementally without a breaking import change.
MarketDataAdapter = AShareDataAdapter
