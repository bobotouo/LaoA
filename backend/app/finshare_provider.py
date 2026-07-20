from __future__ import annotations

import importlib.util
import os
import threading
from collections import defaultdict
from typing import Any


class FinshareProvider:
    """Small adapter around FinShare's TDX source.

    FinShare remains an optional runtime dependency so the API can still start
    before the data package is installed; East Money is used as the primary path.
    """

    INDEX_CODES = {
        "000001": (1, "上证指数"),
        "399001": (0, "深证成指"),
        "399006": (0, "创业板指"),
    }

    # TongdaXin industry indices used only as East Money fallback.
    # Unsupported entries are skipped at runtime.
    INDUSTRY_INDICES = {
        "880301": "农林牧渔",
        "880305": "煤炭",
        "880306": "石油",
        "880310": "有色金属",
        "880318": "贵金属",
        "880319": "化工",
        "880324": "钢铁",
        "880330": "建材",
        "880335": "建筑",
        "880344": "房地产",
        "880350": "电力",
        "880355": "供气供热",
        "880360": "水务",
        "880367": "港口",
        "880380": "航运",
        "880387": "医药",
        "880390": "机械",
        "880398": "电器仪表",
        "880405": "军工",
        "880406": "汽车类",
        "880414": "家用电器",
        "880421": "纺织服饰",
        "880422": "化纤",
        "880430": "造纸",
        "880437": "传媒",
        "880446": "食品",
        "880447": "酿酒",
        "880452": "商业连锁",
        "880465": "银行",
        "880471": "证券",
        "880472": "保险",
        "880473": "多元金融",
        "880482": "软件服务",
        "880489": "半导体",
        "880490": "元器件",
        "880491": "通信设备",
        "880492": "电脑",
        "880493": "电信运营",
        "880494": "互联网",
    }

    def __init__(self) -> None:
        self.available = importlib.util.find_spec("finshare") is not None
        self.version = "not-installed"
        self._source: Any = None
        self._lock = threading.RLock()
        self._catalog: list[dict[str, Any]] | None = None

        # TDX sockets are unreliable on Vercel/serverless; keep East Money only.
        if os.getenv("VERCEL") or os.getenv("DISABLE_FINSHARE") == "1":
            self.available = False
            self.version = "disabled-on-serverless"
            return

        if not self.available:
            return

        try:
            import finshare
            from finshare.sources import tdx_source

            # FinShare ships a long public-server list. Keeping the recently
            # verified hosts first avoids waiting through several stale servers.
            tdx_source.TDX_SERVERS = [
                ("115.238.56.198", 7709),
                ("218.75.126.9", 7709),
                ("101.227.73.20", 7709),
            ]
            self.version = getattr(finshare, "__version__", "unknown")
            self._source = finshare.get_tdx_source()
        except Exception:
            self.available = False
            self._source = None
            self.version = "init-failed"

    def market_snapshot_rows(self) -> list[dict[str, Any]]:
        if not self.available:
            return []
        with self._lock:
            catalog = self._security_catalog()
            codes = [row["code"] for row in catalog]
            snapshots = self._source.get_batch_snapshots(codes)

        names = {row["code"]: row["name"] for row in catalog}
        rows = []
        for key, snapshot in snapshots.items():
            code = str(key).split(".")[0]
            price = self._number(getattr(snapshot, "last_price", 0))
            previous = self._number(getattr(snapshot, "prev_close", 0))
            if price <= 0 or previous <= 0:
                continue
            rows.append(
                {
                    "f2": price,
                    "f3": round((price / previous - 1) * 100, 4),
                    "f6": self._number(getattr(snapshot, "amount", 0)),
                    "f12": code,
                    "f14": names.get(code, code),
                    "f15": self._number(getattr(snapshot, "day_high", 0)),
                    "f16": self._number(getattr(snapshot, "day_low", 0)),
                    "f17": self._number(getattr(snapshot, "day_open", 0)),
                    "f18": previous,
                    "dataSource": "finshare-tdx",
                }
            )
        return rows

    def index_snapshots(self) -> list[dict[str, Any]]:
        if not self.available:
            return []
        result = []
        with self._lock:
            self._ensure_connected()
            for code, (market, name) in self.INDEX_CODES.items():
                bars = self._source._api.get_index_bars(8, market, code, 0, 800) or []
                parsed = self.series_from_bars(bars)
                current_rows, previous_close = self._current_rows_and_previous(bars)
                if not parsed or not current_rows or previous_close <= 0:
                    continue
                prices = [self._number(row.get("close")) for row in current_rows]
                price = prices[-1]
                result.append(
                    {
                        "code": code,
                        "name": name,
                        "price": round(price, 3),
                        "changePct": parsed[-1]["changePct"],
                        "change": round(price - previous_close, 3),
                        "high": round(max(self._number(row.get("high")) for row in current_rows), 3),
                        "low": round(min(self._number(row.get("low")) for row in current_rows), 3),
                        "open": round(self._number(current_rows[0].get("open")), 3),
                        "previousClose": round(previous_close, 3),
                        "amount": sum(self._number(row.get("amount")) for row in current_rows),
                        "series": parsed,
                        "dataSource": "finshare-tdx",
                    }
                )
        return result

    def industry_series(self, top: int = 10, bottom: int = 10) -> list[dict[str, Any]]:
        if not self.available:
            return []
        sectors = []
        with self._lock:
            self._ensure_connected()
            for code, name in self.INDUSTRY_INDICES.items():
                bars = self._source._api.get_index_bars(8, 1, code, 0, 800) or []
                series = self.series_from_bars(bars)
                if not series:
                    continue
                sectors.append(
                    {
                        "code": code,
                        "name": name,
                        "changePct": series[-1]["changePct"],
                        "type": "行业",
                        "series": [
                            {"time": point["time"], "value": point["changePct"]}
                            for point in series
                        ],
                        "leaders": [],
                        "dataSource": "finshare-tdx",
                    }
                )

        sectors.sort(key=lambda row: row["changePct"], reverse=True)
        selected = sectors[:top] + sectors[-bottom:]
        return list({row["code"]: row for row in selected}.values())

    def _security_catalog(self) -> list[dict[str, Any]]:
        if self._catalog is not None:
            return self._catalog
        self._ensure_connected()
        rows = []
        for market in (0, 1):
            count = self._source._api.get_security_count(market)
            for offset in range(0, count, 1000):
                for item in self._source._api.get_security_list(market, offset) or []:
                    code = str(item.get("code") or "")
                    if self._is_a_share(market, code):
                        rows.append(
                            {
                                "market": market,
                                "code": code,
                                "name": str(item.get("name") or code),
                            }
                        )
        self._catalog = list({row["code"]: row for row in rows}.values())
        return self._catalog

    def _ensure_connected(self) -> None:
        if not self._source or not self._source._ensure_connected():
            raise RuntimeError("FinShare TDX data source is unavailable")

    @staticmethod
    def _is_a_share(market: int, code: str) -> bool:
        if market == 1:
            return code.startswith(("600", "601", "603", "605", "688", "689"))
        return code.startswith(("000", "001", "002", "003", "300", "301", "302"))

    @staticmethod
    def series_from_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current, previous_close = FinshareProvider._current_rows_and_previous(bars)
        if previous_close <= 0:
            return []
        result = []
        for row in current:
            price = FinshareProvider._number(row.get("close"))
            if price <= 0:
                continue
            result.append(
                {
                    "time": str(row.get("datetime"))[-5:],
                    "value": round(price, 3),
                    "changePct": round((price / previous_close - 1) * 100, 3),
                }
            )
        return result

    @staticmethod
    def _current_rows_and_previous(
        bars: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], float]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in bars:
            stamp = str(row.get("datetime") or "")
            if len(stamp) >= 16:
                grouped[stamp[:10]].append(row)
        if not grouped:
            return [], 0.0
        dates = sorted(grouped)
        current = sorted(grouped[dates[-1]], key=lambda row: str(row.get("datetime")))
        if not current:
            return [], 0.0
        if len(dates) > 1:
            previous_rows = sorted(grouped[dates[-2]], key=lambda row: str(row.get("datetime")))
            previous_close = FinshareProvider._number(previous_rows[-1].get("close"))
        else:
            previous_close = FinshareProvider._number(current[0].get("open"))
        return current, previous_close

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0
