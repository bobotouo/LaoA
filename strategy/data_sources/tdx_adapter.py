from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pandas as pd

from data_sources.finshare_adapter import FINSHARE_PYTHON, ROOT, FinshareAdapter


TDX_CACHE_DIR = ROOT / "data" / "cache_tdx"
TDX_FETCH_SCRIPT = ROOT / "src" / "data_sources" / "finshare_source_fetch.py"


@dataclass(frozen=True)
class TdxPaths:
    python: Path = FINSHARE_PYTHON
    fetch_script: Path = TDX_FETCH_SCRIPT


class TdxAdapter:
    def __init__(self, paths: TdxPaths | None = None, home_dir: Path | None = None) -> None:
        self.paths = paths or TdxPaths()
        self.home_dir = home_dir or ROOT
        TDX_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def fetch_kline(
        self,
        code: str,
        start: str,
        end: str,
        refresh: bool = False,
    ) -> pd.DataFrame:
        payload = self._run_json(
            ["tdx-kline", code, "--start", start, "--end", end],
            refresh=refresh,
        )
        df = pd.DataFrame(payload)
        if df.empty:
            return df
        df = df.rename(
            columns={
                "trade_date": "date",
                "open_price": "open",
                "high_price": "high",
                "low_price": "low",
                "close_price": "close",
            }
        )
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        for column in ("open", "high", "low", "close", "volume", "amount"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df["code"] = code
        df["data_source"] = "tdx"
        df["adjustment"] = "none"
        df["data_quality"] = "unadjusted_fallback"
        return df.sort_values("date").reset_index(drop=True)

    def fetch_batch_snapshots(self, codes: list[str]) -> pd.DataFrame:
        if not codes:
            return pd.DataFrame()
        payload = self._run_json(["tdx-batch-snapshot", *codes], refresh=True)
        rows = list(payload.values()) if isinstance(payload, dict) else payload
        df = pd.DataFrame(rows or [])
        if df.empty:
            return df
        for column in (
            "last_price",
            "prev_close",
            "day_open",
            "day_high",
            "day_low",
            "volume",
            "amount",
            "bid1_price",
            "ask1_price",
        ):
            if column in df.columns:
                df[column] = pd.to_numeric(df[column], errors="coerce")
        df["data_source"] = "tdx"
        if "timestamp" in df.columns:
            df["data_date"] = pd.to_datetime(df["timestamp"], errors="coerce").dt.normalize()
        return df

    def _run_json(self, args: list[str], refresh: bool) -> list | dict:
        cache_file = TDX_CACHE_DIR / f"{hashlib.sha1(' '.join(args).encode()).hexdigest()}.json"
        if cache_file.exists() and not refresh:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        result = subprocess.run(
            [str(self.paths.python), str(self.paths.fetch_script), *args],
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home_dir)},
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"TDX command failed: {' '.join(args)} | {stderr}")
        payload_text = FinshareAdapter._extract_json(result.stdout)
        payload = json.loads(payload_text)
        if payload:
            cache_file.write_text(payload_text, encoding="utf-8")
        return payload
