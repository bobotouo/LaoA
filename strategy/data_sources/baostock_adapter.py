from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pandas as pd

from data_sources.finshare_adapter import FINSHARE_PYTHON, ROOT, FinshareAdapter


BAOSTOCK_CACHE_DIR = ROOT / "data" / "cache_baostock"
BAOSTOCK_FETCH_SCRIPT = ROOT / "src" / "data_sources" / "baostock_source_fetch.py"


@dataclass(frozen=True)
class BaoStockPaths:
    python: Path = FINSHARE_PYTHON
    fetch_script: Path = BAOSTOCK_FETCH_SCRIPT


class BaoStockAdapter:
    """Free, tokenless BaoStock adapter using one login per batch."""

    def __init__(self, paths: BaoStockPaths | None = None, home_dir: Path | None = None) -> None:
        self.paths = paths or BaoStockPaths()
        self.home_dir = home_dir or ROOT
        BAOSTOCK_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def fetch_stock_list(self, market: str = "all", limit: int = 0) -> pd.DataFrame:
        payload = self._run_json(["--stock-list"], refresh=True)
        df = pd.DataFrame(payload)
        if df.empty:
            return df
        if market == "sh":
            df = df[df["full_code"].astype(str).str.endswith(".SH")]
        elif market == "sz":
            df = df[df["full_code"].astype(str).str.endswith(".SZ")]
        if limit > 0:
            df = df.head(limit)
        for column in (
            "price",
            "change_pct",
            "amount",
            "volume",
            "open",
            "high",
            "low",
            "close",
            "prev_close",
        ):
            df[column] = pd.NA
        df["data_date"] = pd.Timestamp.now().normalize()
        return df.reset_index(drop=True)

    def fetch_kline(
        self,
        code: str,
        start: str,
        end: str,
        adjust: str = "qfq",
        refresh: bool = False,
    ) -> pd.DataFrame:
        return self.fetch_market_history([code], start=start, end=end, adjust=adjust, refresh=refresh)

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
        args = [
            "--start",
            start,
            "--end",
            end,
            "--adjust",
            adjust,
            *codes,
        ]
        payload = self._run_json(args, refresh=refresh)
        df = pd.DataFrame(payload)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        for column in ("open", "high", "low", "close", "prev_close", "volume", "amount", "turnover_rate"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        return df.sort_values(["code", "date"]).reset_index(drop=True)

    def _run_json(self, args: list[str], refresh: bool) -> list | dict:
        cache_file = BAOSTOCK_CACHE_DIR / f"{hashlib.sha1(' '.join(args).encode()).hexdigest()}.json"
        if cache_file.exists() and not refresh:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        result = subprocess.run(
            [str(self.paths.python), str(self.paths.fetch_script), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home_dir)},
            capture_output=True,
            text=True,
            check=False,
            timeout=max(120, min(7200, 2 * len(args))),
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"BaoStock command failed | {stderr}")
        payload = json.loads(FinshareAdapter._extract_json(result.stdout))
        if payload:
            cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload
