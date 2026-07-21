from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, time as day_time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .finshare_provider import FinshareProvider


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SECTOR_SOURCE = "东方财富板块（行业 BK / 概念 BK）"


def default_last_dashboard_path() -> Path:
    # Vercel / serverless filesystems are ephemeral; prefer /tmp there.
    if os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        return Path("/tmp/laoa/last_dashboard.json")
    return Path(__file__).resolve().parents[1] / ".cache" / "last_dashboard.json"


class MarketDataError(RuntimeError):
    pass


@dataclass
class CacheEntry:
    value: Any
    expires_at: float


class MarketDataService:
    INDEX_SECIDS = {
        "000001": ("1.000001", "上证指数"),
        "399001": ("0.399001", "深证成指"),
        "399006": ("0.399006", "创业板指"),
    }

    def __init__(
        self,
        cache_ttl: int = 8,
        timeout: float = 5.0,
        last_dashboard_path: Path | None = None,
    ) -> None:
        self.cache_ttl = cache_ttl
        self.closed_cache_ttl = max(cache_ttl, 60)
        self.timeout = timeout
        self.last_dashboard_path = last_dashboard_path or default_last_dashboard_path()
        self._cache: dict[str, CacheEntry] = {}
        self._component_cache: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self.finshare = FinshareProvider()
        self._active_market_source = "东方财富公开行情"
        self.session = requests.Session()
        retry_total = 0 if os.getenv("VERCEL") else 2
        retry = Retry(
            total=retry_total,
            connect=retry_total,
            read=min(1, retry_total),
            backoff_factor=0.25,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16))
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                "Referer": "https://quote.eastmoney.com/",
                "Accept": "application/json,text/plain,*/*",
            }
        )

    def get_dashboard(self, refresh: bool = False) -> dict[str, Any]:
        """Always return real market data. Outside sessions this is the latest close snapshot."""
        key = "dashboard"
        now = time.time()
        if not refresh:
            with self._lock:
                cached = self._cache.get(key)
                if cached and cached.expires_at > now:
                    return cached.value

        try:
            result = self._build_live_dashboard()
            self._save_last_dashboard(result)
        except Exception as exc:
            stale = self._load_last_dashboard()
            if stale is None:
                raise MarketDataError(str(exc)) from exc
            result = deepcopy(stale)
            result["meta"] = {
                **result.get("meta", {}),
                "stale": True,
                "mode": "live",
                "message": f"数据源暂不可用，展示最近一次成功行情（{exc}）",
            }

        ttl = self.cache_ttl if result.get("meta", {}).get("isTrading") else self.closed_cache_ttl
        with self._lock:
            self._cache[key] = CacheEntry(result, now + ttl)
        return result

    def _request_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for candidate in self._request_candidates(url):
            try:
                response = self.session.get(candidate, params=params, timeout=self.timeout)
                response.raise_for_status()
                text = response.text.strip()
                try:
                    return response.json()
                except requests.JSONDecodeError:
                    start = text.find("{")
                    end = text.rfind("}")
                    if start >= 0 and end > start:
                        return json.loads(text[start : end + 1])
                    raise MarketDataError(f"行情接口返回格式异常（{candidate}）")
            except (requests.RequestException, MarketDataError, json.JSONDecodeError) as exc:
                last_error = exc

        raise MarketDataError(f"行情接口暂不可用（{urlsplit(url).hostname or url}）：{last_error}") from last_error

    @staticmethod
    def _request_candidates(url: str) -> list[str]:
        """Return the canonical East Money host followed by public fallbacks.

        Vercel's shared outbound IPs are occasionally disconnected by one
        East Money gateway. The API path is identical across these hosts.
        """
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        service = next(
            (label for label in ("push2his", "push2ex", "push2") if label in hostname),
            None,
        )
        if not service:
            return [url]

        preferred = os.getenv("EASTMONEY_HOST", "").strip()
        hosts = [
            preferred,
            f"{service}.eastmoney.com",
            f"82.{service}.eastmoney.com",
            f"60.{service}.eastmoney.com",
        ]
        candidates: list[str] = []
        for host in hosts:
            if not host:
                continue
            candidate = urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates or [url]

    def _build_live_dashboard(self) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=4) as executor:
            index_future = executor.submit(self._fetch_indices)
            sector_future = executor.submit(self._fetch_sectors)
            spot_future = executor.submit(self._fetch_spot_market)
            overview_future = executor.submit(self._fetch_overview_counts)

            indices = index_future.result()
            sectors = sector_future.result()
            spots = spot_future.result()
            overview_counts = overview_future.result()

        if len(indices) < 3 or not sectors:
            raise MarketDataError("核心实时行情数据不完整")

        breadth, turnover = self._summarize_spots(spots, overview_counts)
        turnover.update(self._fetch_turnover_averages(turnover["current"]))
        limit_pool = self._fetch_limit_pool(spots)
        now = datetime.now(SHANGHAI_TZ)

        is_trading = self._is_trading_time(now)
        return {
            "meta": {
                "mode": "live",
                "stale": False,
                "source": self._active_market_source,
                "sectorSource": SECTOR_SOURCE,
                "updatedAt": now.isoformat(timespec="seconds"),
                "isTrading": is_trading,
                "message": (
                    "交易中，数据实时刷新"
                    if is_trading
                    else "非交易时段，展示最近一个交易日的最新行情"
                ),
                "stockCount": len(spots),
                "sectorCount": len(sectors),
                "pollIntervalMs": 8000 if is_trading else 120000,
            },
            "indices": indices,
            "sectors": sectors,
            "breadth": breadth,
            "turnover": turnover,
            "limitPool": limit_pool,
        }

    def _fetch_indices(self) -> list[dict[str, Any]]:
        cached = self._get_component_cache("indices")
        if cached is not None:
            return cached

        if self.finshare.available:
            try:
                result = self.finshare.index_snapshots()
                if len(result) == 3:
                    self._set_component_cache("indices", result, ttl=15)
                    return result
            except Exception:
                pass

        secids = ",".join(item[0] for item in self.INDEX_SECIDS.values())
        try:
            payload = self._request_json(
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                {
                    "fltt": 2,
                    "invt": 2,
                    "fields": "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18",
                    "secids": secids,
                },
            )
        except Exception:
            return self._get_component_cache("indices", allow_stale=True) or []
        rows = (payload.get("data") or {}).get("diff") or []
        by_code = {str(row.get("f12")): row for row in rows}

        result: list[dict[str, Any]] = []
        trends: dict[str, list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_map = {
                executor.submit(self._fetch_trend, secid): (code, name)
                for code, (secid, name) in self.INDEX_SECIDS.items()
            }
            for future in as_completed(future_map):
                code, _ = future_map[future]
                try:
                    trends[code] = future.result()
                except Exception:
                    trends[code] = []

        for code, (_, default_name) in self.INDEX_SECIDS.items():
            row = by_code.get(code)
            if not row:
                continue
            result.append(
                {
                    "code": code,
                    "name": row.get("f14") or default_name,
                    "price": self._number(row.get("f2")),
                    "changePct": self._number(row.get("f3")),
                    "change": self._number(row.get("f4")),
                    "high": self._number(row.get("f15")),
                    "low": self._number(row.get("f16")),
                    "open": self._number(row.get("f17")),
                    "previousClose": self._number(row.get("f18")),
                    "amount": self._number(row.get("f6")),
                    "series": trends.get(code) or [],
                }
            )
        if result:
            self._set_component_cache("indices", result, ttl=12)
        return result

    def _fetch_trend(self, secid: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            "https://push2his.eastmoney.com/api/qt/stock/trends2/get",
            {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "iscr": 0,
                "ndays": 1,
            },
        )
        data = payload.get("data") or {}
        previous = self._number(data.get("prePrice"))
        result = []
        for item in data.get("trends") or []:
            parts = str(item).split(",")
            if len(parts) < 3:
                continue
            price = self._number(parts[2])
            result.append(
                {
                    "time": parts[0][-5:],
                    "value": price,
                    "changePct": round((price / previous - 1) * 100, 3) if previous else 0,
                }
            )
        return result

    def _fetch_sectors(self) -> list[dict[str, Any]]:
        cached = self._get_component_cache("sectors")
        if cached is not None:
            return cached

        # Prefer East Money board list: full ranking + real intraday trends.
        try:
            result = self._fetch_eastmoney_sectors(top=10, bottom=10)
            if result:
                self._set_component_cache("sectors", result, ttl=20)
                return result
        except Exception:
            pass

        # Fallback: TongdaXin industry bars when East Money boards are unavailable.
        if self.finshare.available:
            try:
                industries = self.finshare.industry_series(top=10, bottom=10)
                if industries:
                    self._set_component_cache("sectors", industries, ttl=30)
                    return industries
            except Exception:
                pass

        return self._get_component_cache("sectors", allow_stale=True) or []

    def _fetch_eastmoney_sectors(self, top: int = 10, bottom: int = 10) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for sector_type, fs in (
            ("行业", "m:90+t:2+f:!50"),
            ("概念", "m:90+t:3+f:!50"),
        ):
            rows.extend(self._fetch_eastmoney_sector_rank(sector_type, fs, top=top, bottom=bottom))

        deduplicated: dict[str, dict[str, Any]] = {row["code"]: row for row in rows}
        selected = sorted(deduplicated.values(), key=lambda row: row["changePct"], reverse=True)
        if not selected:
            return []

        with ThreadPoolExecutor(max_workers=16) as executor:
            trend_futures = {
                executor.submit(self._fetch_trend, f"90.{row['code']}"): row for row in selected
            }
            leader_futures = {
                executor.submit(self._fetch_sector_leaders, row["code"]): row for row in selected
            }
            for future in as_completed(trend_futures):
                row = trend_futures[future]
                try:
                    trend = future.result()
                except Exception:
                    trend = []
                row["series"] = [
                    {"time": point["time"], "value": point["changePct"]} for point in trend
                ]
                row["dataSource"] = "eastmoney"
            for future in as_completed(leader_futures):
                row = leader_futures[future]
                try:
                    row["leaders"] = future.result()
                except Exception:
                    row["leaders"] = []

        for row in selected:
            if not row.get("series"):
                row["series"] = [
                    {"time": "09:30", "value": 0},
                    {"time": "15:00", "value": row["changePct"]},
                ]
            row.setdefault("leaders", [])
        return selected

    def _fetch_sector_leaders(self, board_code: str, limit: int = 15) -> list[dict[str, Any]]:
        """Top constituents of an East Money board by change percentage."""
        code = str(board_code or "").strip().upper()
        if not code.startswith("BK"):
            return []
        payload = self._request_json(
            "https://82.push2.eastmoney.com/api/qt/clist/get",
            {
                "pn": 1,
                "pz": max(limit, 1),
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": f"b:{code}+f:!50",
                "fields": "f2,f3,f12,f14",
            },
        )
        diff = (payload.get("data") or {}).get("diff") or []
        leaders = []
        for row in diff:
            stock_code = str(row.get("f12") or "")
            name = str(row.get("f14") or "")
            if not stock_code or not name:
                continue
            leaders.append(
                {
                    "code": stock_code,
                    "name": name,
                    "changePct": self._number(row.get("f3")),
                    "price": self._number(row.get("f2")),
                }
            )
            if len(leaders) >= limit:
                break
        return leaders

    def _fetch_eastmoney_sector_rank(
        self, sector_type: str, fs: str, top: int, bottom: int
    ) -> list[dict[str, Any]]:
        """Fetch true leaders and laggards via ascending/descending sort."""
        gainers = self._fetch_eastmoney_sector_page(sector_type, fs, descending=True, limit=top)
        laggards = self._fetch_eastmoney_sector_page(sector_type, fs, descending=False, limit=bottom)
        merged: dict[str, dict[str, Any]] = {}
        for row in gainers + laggards:
            merged[row["code"]] = row
        return list(merged.values())

    def _fetch_eastmoney_sector_page(
        self, sector_type: str, fs: str, descending: bool, limit: int
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            "https://82.push2.eastmoney.com/api/qt/clist/get",
            {
                "pn": 1,
                "pz": max(limit, 1),
                "po": 1 if descending else 0,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": fs,
                "fields": "f2,f3,f12,f14",
            },
        )
        diff = (payload.get("data") or {}).get("diff") or []
        rows = [
            {
                "code": str(row.get("f12") or ""),
                "name": str(row.get("f14") or ""),
                "changePct": self._number(row.get("f3")),
                "type": sector_type,
            }
            for row in diff
            if row.get("f12") and row.get("f14")
        ]
        return rows[:limit]

    def _fetch_spot_market(self) -> list[dict[str, Any]]:
        cached = self._get_component_cache("market-snapshots")
        if cached is not None:
            return cached

        if self.finshare.available:
            try:
                rows = self.finshare.market_snapshot_rows()
                if len(rows) >= 4000:
                    self._active_market_source = (
                        f"FinShare {self.finshare.version} · 通达信 / 东方财富"
                    )
                    self._set_component_cache("market-snapshots", rows, ttl=18)
                    return rows
            except Exception:
                pass

        endpoint = "https://82.push2.eastmoney.com/api/qt/clist/get"
        base_params = {
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f2,f3,f6,f12,f14",
        }
        payload = self._request_json(endpoint, {**base_params, "pn": 1, "pz": 6000})
        data = payload.get("data") or {}
        rows = data.get("diff") or []

        # clist/get is capped at 100 rows by some East Money gateways even when
        # a larger pz is requested. Fetch the remaining pages explicitly so the
        # histogram is based on the whole market instead of the top 100 gainers.
        total = int(self._number(data.get("total")))
        page_size = len(rows)
        if rows and total > page_size and page_size > 0:
            page_count = min((total + page_size - 1) // page_size, 80)

            def fetch_page(page: int) -> list[dict[str, Any]]:
                page_payload = self._request_json(
                    endpoint,
                    {**base_params, "pn": page, "pz": page_size},
                )
                return ((page_payload.get("data") or {}).get("diff") or [])

            with ThreadPoolExecutor(max_workers=8) as executor:
                for page_rows in executor.map(fetch_page, range(2, page_count + 1)):
                    rows.extend(page_rows)

            deduped: dict[str, dict[str, Any]] = {}
            for row in rows:
                code = str(row.get("f12") or "")
                if code:
                    deduped[code] = row
            rows = list(deduped.values())

            # A partial result would make breadth look precise while silently
            # sampling only part of the market; fail so the caller can use its
            # last known complete snapshot instead.
            if total >= 1000 and len(rows) < int(total * 0.9):
                raise MarketDataError(f"东方财富全市场分页不完整（{len(rows)}/{total}）")

        self._active_market_source = "东方财富公开行情"
        self._set_component_cache("market-snapshots", rows, ttl=24)
        return rows

    def _fetch_overview_counts(self) -> dict[str, int]:
        cached = self._get_component_cache("overview")
        if cached is not None:
            return cached
        try:
            payload = self._request_json(
                "https://82.push2.eastmoney.com/api/qt/ulist.np/get",
                {
                    "fltt": 2,
                    "fields": "f104,f105,f106,f107",
                    "secids": "1.000001,0.399001",
                },
            )
        except Exception:
            return self._get_component_cache("overview", allow_stale=True) or {}
        rows = (payload.get("data") or {}).get("diff") or []
        result = {
            "up": sum(int(self._number(row.get("f104"))) for row in rows),
            "down": sum(int(self._number(row.get("f105"))) for row in rows),
            "limitUp": sum(int(self._number(row.get("f106"))) for row in rows),
            "limitDown": sum(int(self._number(row.get("f107"))) for row in rows),
        }
        self._set_component_cache("overview", result, ttl=15)
        return result

    def _summarize_spots(
        self, spots: list[dict[str, Any]], overview_counts: dict[str, int]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        changes = [self._number(row.get("f3")) for row in spots if row.get("f3") not in (None, "-")]
        up = sum(1 for value in changes if value > 0.005)
        down = sum(1 for value in changes if value < -0.005)
        flat = max(0, len(changes) - up - down)

        bins = [
            ("跌停", lambda value: value <= -9.5),
            ("≤-8", lambda value: -9.5 < value <= -8),
            ("-8~-5", lambda value: -8 < value <= -5),
            ("-5~-3", lambda value: -5 < value <= -3),
            ("-3~-1", lambda value: -3 < value <= -1),
            ("-1~0", lambda value: -1 < value < 0),
            ("平", lambda value: -0.005 <= value <= 0.005),
            ("0~1", lambda value: 0 < value < 1),
            ("1~3", lambda value: 1 <= value < 3),
            ("3~5", lambda value: 3 <= value < 5),
            ("5~8", lambda value: 5 <= value < 8),
            ("≥8", lambda value: 8 <= value < 9.5),
            ("涨停", lambda value: value >= 9.5),
        ]
        distribution = [
            {"label": label, "count": sum(1 for value in changes if predicate(value))}
            for label, predicate in bins
        ]
        total_amount = sum(self._number(row.get("f6")) for row in spots)

        # Keep up/flat/down on one universe so cards match the histogram.
        # Overview limit counts are preferred (handles 20%/5% boards better than ±9.5%).
        if len(changes) >= 4000:
            breadth_up, breadth_flat, breadth_down = up, flat, down
        else:
            breadth_up = overview_counts.get("up") or up
            breadth_down = overview_counts.get("down") or down
            breadth_flat = flat if not overview_counts else max(0, len(changes) - breadth_up - breadth_down)

        breadth = {
            "up": breadth_up,
            "flat": breadth_flat,
            "down": breadth_down,
            "limitUp": overview_counts.get("limitUp")
            or sum(1 for value in changes if value >= 9.5),
            "limitDown": overview_counts.get("limitDown")
            or sum(1 for value in changes if value <= -9.5),
            "distribution": distribution,
        }
        turnover = {
            "current": total_amount,
            "forecast": self._forecast_turnover(total_amount),
            "previous": 0,
            "delta": 0,
            "avg5": 0,
            "avg20": 0,
            "avg60": 0,
        }
        return breadth, turnover

    def _fetch_turnover_averages(self, current: float) -> dict[str, float]:
        cached = self._get_component_cache("turnover-averages")
        if cached is not None:
            return {**cached, "delta": current - cached.get("previous", 0)}
        secids = ("1.000001", "0.399106")
        by_date: dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self._fetch_daily_amounts, secid) for secid in secids]
            for future in futures:
                try:
                    daily = future.result()
                except Exception:
                    daily = {}
                for day, amount in daily.items():
                    by_date[day] = by_date.get(day, 0) + amount

        amounts = [amount for _, amount in sorted(by_date.items()) if amount > 0]
        if not amounts:
            return {"previous": 0, "delta": 0, "avg5": 0, "avg20": 0, "avg60": 0}
        previous = amounts[-1]
        result = {
            "previous": previous,
            "delta": current - previous,
            "avg5": self._average(amounts[-5:]),
            "avg20": self._average(amounts[-20:]),
            "avg60": self._average(amounts[-60:]),
        }
        self._set_component_cache("turnover-averages", result, ttl=300)
        return result

    def _fetch_daily_amounts(self, secid: str) -> dict[str, float]:
        payload = self._request_json(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {
                "secid": secid,
                "klt": 101,
                "fqt": 0,
                "lmt": 70,
                "end": "20500101",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            },
        )
        result = {}
        for row in (payload.get("data") or {}).get("klines") or []:
            parts = str(row).split(",")
            if len(parts) > 6:
                result[parts[0]] = self._number(parts[6])
        return result

    def _fetch_limit_pool(self, spots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cached = self._get_component_cache("limit-pool")
        if cached is not None:
            return cached
        now = datetime.now(SHANGHAI_TZ)
        try:
            payload = self._request_json(
                "https://push2ex.eastmoney.com/getTopicZTPool",
                {
                    "ut": "7eea3edcaed734bea9cbfc24409ed989",
                    "dpt": "wz.ztzt",
                    "Pageindex": 0,
                    "pagesize": 30,
                    "sort": "fbt:asc",
                    "date": now.strftime("%Y%m%d"),
                },
            )
            pool = (payload.get("data") or {}).get("pool") or []
            result = []
            for row in pool[:15]:
                result.append(
                    {
                        "code": str(row.get("c") or ""),
                        "name": str(row.get("n") or ""),
                        "changePct": self._number(row.get("zdp")),
                        "consecutive": int(self._number(row.get("lbc"))) or 1,
                        "industry": str(row.get("hybk") or "--"),
                        "amount": self._number(row.get("amount")),
                        "firstLimitTime": self._format_limit_time(row.get("fbt")),
                    }
                )
            if result:
                self._set_component_cache("limit-pool", result, ttl=30)
                return result
        except Exception:
            pass

        candidates = sorted(spots, key=lambda row: self._number(row.get("f3")), reverse=True)
        result = [
            {
                "code": str(row.get("f12") or ""),
                "name": str(row.get("f14") or ""),
                "changePct": self._number(row.get("f3")),
                "consecutive": 1,
                "industry": "--",
                "amount": self._number(row.get("f6")),
                "firstLimitTime": "--",
            }
            for row in candidates[:15]
            if self._number(row.get("f3")) > 7
        ]
        self._set_component_cache("limit-pool", result, ttl=20)
        return result

    def _save_last_dashboard(self, payload: dict[str, Any]) -> None:
        try:
            self.last_dashboard_path.parent.mkdir(parents=True, exist_ok=True)
            self.last_dashboard_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _load_last_dashboard(self) -> dict[str, Any] | None:
        try:
            if not self.last_dashboard_path.exists():
                return None
            payload = json.loads(self.last_dashboard_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("sectors") and payload.get("indices"):
                return payload
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return None

    @staticmethod
    def _number(value: Any) -> float:
        try:
            if value in (None, "", "-"):
                return 0.0
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _format_limit_time(value: Any) -> str:
        """East Money first-limit time is usually HHMMSS as int, e.g. 92500 -> 09:25:00."""
        if value in (None, "", "-", "--"):
            return "--"
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        if not digits:
            return str(value)
        digits = digits.zfill(6)[-6:]
        return f"{digits[0:2]}:{digits[2:4]}:{digits[4:6]}"

    def _get_component_cache(self, key: str, allow_stale: bool = False) -> Any | None:
        with self._lock:
            cached = self._component_cache.get(key)
            if cached and (allow_stale or cached.expires_at > time.time()):
                return cached.value
        return None

    def _set_component_cache(self, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            self._component_cache[key] = CacheEntry(value, time.time() + ttl)

    @staticmethod
    def _average(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _is_trading_time(now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        current = now.time()
        return day_time(9, 15) <= current <= day_time(11, 30) or day_time(13, 0) <= current <= day_time(15, 0)

    def _forecast_turnover(self, current: float) -> float:
        now = datetime.now(SHANGHAI_TZ)
        current_minutes = 0
        if now.time() >= day_time(9, 30):
            morning_end = min(now, now.replace(hour=11, minute=30, second=0, microsecond=0))
            morning_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
            current_minutes += max(0, int((morning_end - morning_start).total_seconds() / 60))
        if now.time() >= day_time(13, 0):
            afternoon_end = min(now, now.replace(hour=15, minute=0, second=0, microsecond=0))
            afternoon_start = now.replace(hour=13, minute=0, second=0, microsecond=0)
            current_minutes += max(0, int((afternoon_end - afternoon_start).total_seconds() / 60))
        if current_minutes < 15 or current_minutes >= 240:
            return current
        return current * 240 / current_minutes
