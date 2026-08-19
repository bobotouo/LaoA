from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
import urllib.request
from urllib.parse import urlencode

import pandas as pd

from data_sources.finshare_adapter import ROOT


SINA_PAGE_CACHE_DIR = ROOT / "data" / "sina_lists"
EASTMONEY_CACHE_DIR = ROOT / "data" / "eastmoney_klines"
SINA_LIST_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
EASTMONEY_HOST = "push2his.eastmoney.com"
EASTMONEY_KLINE_PATH = "/api/qt/stock/kline/get"
EASTMONEY_FALLBACK_IPS = (
    "103.220.167.80",
    "140.207.67.156",
    "117.184.40.129",
)
TENCENT_HOST = "web.ifzq.gtimg.cn"
TENCENT_KLINE_PATH = "/appstock/app/fqkline/get"
TENCENT_FALLBACK_IPS = ("43.154.254.89", "43.154.254.185")
SINA_KLINE_URL = (
    "https://quotes.sina.cn/cn/api/jsonp_v2.php/",
    "var%20_=/CN_MarketDataService.getKLineData"
)


class FreeWebAdapter:
    """Tokenless Sina stock list plus EastMoney adjusted daily bars."""

    def __init__(self, workers: int | None = None) -> None:
        self.workers = workers or int(os.getenv("A_SHARE_WEB_WORKERS", "16"))
        SINA_PAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        EASTMONEY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def fetch_stock_list(
        self,
        market: str = "all",
        limit: int = 0,
        refresh: bool = False,
    ) -> pd.DataFrame:
        files = sorted(SINA_PAGE_CACHE_DIR.glob("page_*.json"))
        current_cache = files and all(
            date.fromtimestamp(path.stat().st_mtime) == date.today() for path in files
        )
        if refresh and not current_cache:
            self._download_sina_pages()
            files = sorted(SINA_PAGE_CACHE_DIR.glob("page_*.json"))
        elif not files:
            self._download_sina_pages()
            files = sorted(SINA_PAGE_CACHE_DIR.glob("page_*.json"))

        rows: list[dict[str, Any]] = []
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, list):
                rows.extend(payload)

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            symbol = str(row.get("symbol", "")).lower()
            if not symbol.startswith(("sh", "sz")):
                continue
            code = str(row.get("code", "")).zfill(6)
            suffix = "SH" if symbol.startswith("sh") else "SZ"
            full_code = f"{code}.{suffix}"
            if full_code in seen:
                continue
            seen.add(full_code)
            normalized.append(
                {
                    "code": code,
                    "full_code": full_code,
                    "name": str(row.get("name", "")),
                    "market": 1 if suffix == "SH" else 0,
                    "price": self._number(row.get("trade")),
                    "change_pct": self._number(row.get("changepercent")),
                    "amount": self._number(row.get("amount")),
                    "volume": self._number(row.get("volume")),
                    "open": self._number(row.get("open")),
                    "high": self._number(row.get("high")),
                    "low": self._number(row.get("low")),
                    "close": self._number(row.get("trade")),
                    "prev_close": self._number(row.get("settlement")),
                    "data_source": "sina",
                    "data_date": pd.Timestamp.now().normalize(),
                }
            )
        df = pd.DataFrame(normalized)
        if market == "sh":
            df = df[df["full_code"].str.endswith(".SH")]
        elif market == "sz":
            df = df[df["full_code"].str.endswith(".SZ")]
        if limit > 0:
            df = df.head(limit)
        return df.reset_index(drop=True)

    def fetch_kline(
        self,
        code: str,
        start: str,
        end: str,
        adjust: str = "qfq",
        refresh: bool = False,
    ) -> pd.DataFrame:
        return self.fetch_market_history(
            [code], start=start, end=end, adjust=adjust, refresh=refresh
        )

    def fetch_market_history(
        self,
        codes: list[str],
        start: str,
        end: str,
        adjust: str = "qfq",
        refresh: bool = False,
    ) -> pd.DataFrame:
        if not codes:
            return pd.DataFrame()
        unique_codes = list(dict.fromkeys(str(code) for code in codes))
        frames: list[pd.DataFrame] = []
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            jobs = {
                executor.submit(self._fetch_one, code, start, end, adjust, refresh): code
                for code in unique_codes
            }
            for completed, future in enumerate(as_completed(jobs), start=1):
                code = jobs[future]
                try:
                    frame = future.result()
                    if frame.empty:
                        failures.append(code)
                    else:
                        frames.append(frame)
                except Exception:
                    failures.append(code)
                if completed % 250 == 0 or completed == len(jobs):
                    print(
                        f"free_web_history={completed}/{len(jobs)} failures={len(failures)}",
                        file=sys.stderr,
                        flush=True,
                    )

        coverage = len(frames) / len(unique_codes)
        if coverage < 0.95:
            raise RuntimeError(
                f"EastMoney history coverage too low: {len(frames)}/{len(unique_codes)}"
            )
        result = pd.concat(frames, ignore_index=True)
        result.attrs["requested_codes"] = len(unique_codes)
        result.attrs["failed_codes"] = failures
        result.attrs["coverage"] = coverage
        return result.sort_values(["code", "date"]).reset_index(drop=True)

    def _fetch_one(
        self,
        code: str,
        start: str,
        end: str,
        adjust: str,
        refresh: bool,
    ) -> pd.DataFrame:
        safe_code = code.replace(".", "_")
        cache_file = EASTMONEY_CACHE_DIR / (
            f"{safe_code}_{start.replace('-', '')}_{end.replace('-', '')}_{adjust}.json"
        )
        if cache_file.exists() and not refresh:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            try:
                payload = self._fetch_tencent_payload(code, start, end, adjust)
            except Exception:
                try:
                    payload = self._fetch_eastmoney_payload(code, start, end, adjust)
                except Exception:
                    payload = {"_provider": "sina", "klines": self._fetch_sina_payload(code, start, end)}
            if self._payload_has_bars(payload):
                cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        payload = self._backfill_sina_days(payload, code, end)
        if cache_file.exists() and self._payload_has_bars(payload):
            provider = payload.get("_provider")
            klines_now = payload.get("klines") if provider in ("tencent", "sina") else (payload.get("data") or {}).get("klines")
            try:
                old = json.loads(cache_file.read_text(encoding="utf-8"))
                old_provider = old.get("_provider")
                old_klines = old.get("klines") if old_provider in ("tencent", "sina") else (old.get("data") or {}).get("klines")
                if len(klines_now) != len(old_klines or []):
                    cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except (OSError, json.JSONDecodeError):
                pass
        if payload.get("_provider") in ("tencent", "sina"):
            klines = payload.get("klines") or []
            source = payload.get("_provider", "tencent")
        else:
            data = payload.get("data") or {}
            klines = data.get("klines") or []
            source = "eastmoney"
        rows: list[dict[str, Any]] = []
        for line in klines:
            fields = line if isinstance(line, list) else str(line).split(",")
            if len(fields) < 6:
                continue
            rows.append(
                {
                    "code": code,
                    "date": fields[0],
                    "open": self._number(fields[1]),
                    "close": self._number(fields[2]),
                    "high": self._number(fields[3]),
                    "low": self._number(fields[4]),
                    "volume": self._number(fields[5]),
                    "amount": self._number(fields[6]) if len(fields) > 6 else None,
                    "amplitude": self._number(fields[7]) if len(fields) > 7 else None,
                    "change_pct": self._number(fields[8]) if len(fields) > 8 else None,
                    "change": self._number(fields[9]) if len(fields) > 9 else None,
                    "turnover_rate": self._number(fields[10]) if len(fields) > 10 else None,
                    "data_source": source,
                    "adjustment": adjust,
                }
            )
        frame = pd.DataFrame(rows)
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        return frame

    def _fetch_tencent_payload(
        self, code: str, start: str, end: str, adjust: str
    ) -> dict[str, Any]:
        symbol, _, suffix = code.upper().partition(".")
        raw_symbol = ("sh" if suffix == "SH" or symbol.startswith("6") else "sz") + symbol
        adjustment = {"none": "", "qfq": "qfq", "hfq": "hfq"}.get(adjust, "qfq")
        param = f"{raw_symbol},day,{start},{end},200,{adjustment}"
        url = f"https://{TENCENT_HOST}{TENCENT_KLINE_PATH}?{urlencode({'param': param})}"
        configured = os.getenv("TENCENT_RESOLVE_IPS", "")
        ips = [item.strip() for item in configured.split(",") if item.strip()]
        ips.extend(ip for ip in TENCENT_FALLBACK_IPS if ip not in ips)
        last_error = "no endpoint attempted"
        key = {"none": "day", "qfq": "qfqday", "hfq": "hfqday"}.get(adjust, "qfqday")
        for ip in ips:
            result = subprocess.run(
                [
                    "curl",
                    "--resolve",
                    f"{TENCENT_HOST}:443:{ip}",
                    "--max-time",
                    "15",
                    "--silent",
                    "--show-error",
                    url,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                last_error = result.stderr.strip()
                continue
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                last_error = result.stdout[:100]
                continue
            klines = ((payload.get("data") or {}).get(raw_symbol) or {}).get(key) or []
            if klines:
                return {"_provider": "tencent", "klines": klines}
            last_error = str(payload)[:100]
        # 兜底: 所有 IP 直连失败时尝试直接 DNS 解析（适用于海外 runner）
        try:
            direct = subprocess.run(
                ["curl", "--max-time", "20", "--silent", "--show-error", url],
                capture_output=True, text=True, check=False,
            )
            if direct.returncode == 0:
                payload = json.loads(direct.stdout)
                klines = ((payload.get("data") or {}).get(raw_symbol) or {}).get(key) or []
                if klines:
                    return {"_provider": "tencent", "klines": klines}
                last_error = str(payload)[:100]
            else:
                last_error = direct.stderr.strip() or last_error
        except Exception as exc:
            last_error = str(exc)
        raise RuntimeError(f"Tencent request failed for {code}: {last_error}")

    def _fetch_eastmoney_payload(
        self, code: str, start: str, end: str, adjust: str
    ) -> dict[str, Any]:
        symbol, _, suffix = code.upper().partition(".")
        market = "1" if suffix == "SH" or symbol.startswith(("6", "688")) else "0"
        params = {
            "secid": f"{market}.{symbol}",
            "klt": "101",
            "fqt": {"none": "0", "qfq": "1", "hfq": "2"}.get(adjust, "1"),
            "beg": start.replace("-", ""),
            "end": end.replace("-", ""),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        }
        url = f"https://{EASTMONEY_HOST}{EASTMONEY_KLINE_PATH}?{urlencode(params)}"
        configured = os.getenv("EASTMONEY_RESOLVE_IPS", "")
        ips = [item.strip() for item in configured.split(",") if item.strip()]
        ips.extend(ip for ip in EASTMONEY_FALLBACK_IPS if ip not in ips)
        last_error = "no endpoint attempted"
        for ip in ips:
            command = [
                "curl",
                "--resolve",
                f"{EASTMONEY_HOST}:443:{ip}",
                "--max-time",
                "15",
                "--silent",
                "--show-error",
                url,
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                last_error = result.stderr.strip()
                continue
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                last_error = result.stdout[:100]
                continue
            if payload.get("data") is not None:
                return payload
            last_error = str(payload)[:100]
        # 兜底: 所有 IP 直连失败时尝试直接 DNS 解析（适用于海外 runner）
        try:
            direct = subprocess.run(
                ["curl", "--max-time", "20", "--silent", "--show-error", url],
                capture_output=True, text=True, check=False,
            )
            if direct.returncode == 0:
                payload = json.loads(direct.stdout)
                if payload.get("data") is not None:
                    return payload
                last_error = str(payload)[:100]
            else:
                last_error = direct.stderr.strip() or last_error
        except Exception as exc:
            last_error = str(exc)
        raise RuntimeError(f"EastMoney request failed for {code}: {last_error}")

    def _download_sina_pages(self) -> None:
        def download(page: int) -> None:
            params = {
                "page": page,
                "num": 100,
                "sort": "symbol",
                "asc": 1,
                "node": "hs_a",
                "symbol": "",
            }
            result = subprocess.run(
                [
                    "curl",
                    "--max-time",
                    "20",
                    "--silent",
                    "--show-error",
                    "-A",
                    "Mozilla/5.0",
                    f"{SINA_LIST_URL}?{urlencode(params)}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip())
            payload = json.loads(result.stdout)
            path = SINA_PAGE_CACHE_DIR / f"page_{page:02d}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        with ThreadPoolExecutor(max_workers=min(self.workers, 8)) as executor:
            futures = [executor.submit(download, page) for page in range(1, 71)]
            for future in as_completed(futures):
                future.result()

    def _backfill_sina_days(
        self, payload: dict[str, Any], code: str, end: str
    ) -> dict[str, Any]:
        """Append Sina daily bars when the cached payload lags behind the
        requested end date. Tencent/EastMoney often lag one trading day.
        """
        if payload.get("_provider") in ("tencent", "sina"):
            klines = payload.get("klines") or []
        else:
            data = payload.get("data") or {}
            klines = data.get("klines") or []
        if not klines:
            return payload
        last_line = klines[-1]
        last_date = last_line[0] if isinstance(last_line, list) else str(last_line).split(",")[0]
        if last_date >= end:
            return payload
        try:
            sina_klines = self._fetch_sina_payload(code, last_date, end)
        except Exception:
            return payload
        if not sina_klines:
            return payload
        existing_dates = {
            line[0] if isinstance(line, list) else str(line).split(",")[0]
            for line in klines
        }
        extra = [line for line in sina_klines if line[0] not in existing_dates]
        if not extra:
            return payload
        if payload.get("_provider") == "tencent":
            payload["klines"] = klines + extra
        else:
            payload["data"]["klines"] = klines + extra
        return payload

    def _fetch_sina_payload(
        self, code: str, start: str, end: str
    ) -> list[list[str]]:
        """Fetch unadjusted daily bars from Sina for the missing tail.

        Returns rows shaped like Tencent: [date, open, close, high, low, volume].
        """
        symbol, _, suffix = code.upper().partition(".")
        raw_symbol = ("sh" if suffix == "SH" or symbol.startswith("6") else "sz") + symbol
        url = f"{''.join(SINA_KLINE_URL)}?{urlencode({'symbol': raw_symbol, 'scale': '240', 'ma': 'no', 'datalen': '20'})}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            text = response.read().decode("utf-8", "ignore")
        match = re.search(r"\(\[(.*)\]\)", text, re.S)
        if not match:
            raise RuntimeError(f"Sina KLine parse failed for {code}: {text[:120]}")
        rows = json.loads(f"[{match.group(1)}]")
        result: list[list[str]] = []
        for row in rows:
            day = str(row.get("day", ""))
            if start <= day <= end:
                result.append(
                    [
                        day,
                        str(row.get("open", "")),
                        str(row.get("close", "")),
                        str(row.get("high", "")),
                        str(row.get("low", "")),
                        str(row.get("volume", "")),
                    ]
                )
        return result

    @staticmethod
    def _payload_has_bars(payload: dict[str, Any]) -> bool:
        if payload.get("_provider") in ("tencent", "sina"):
            return bool(payload.get("klines"))
        return bool((payload.get("data") or {}).get("klines"))
    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value) if value not in (None, "", "-") else None
        except (TypeError, ValueError):
            return None
