from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import json
import os
import subprocess

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "cache_yfinance"
AGU_PYTHON = Path("/Users/bobo/Desktop/project/agu-skill/.venv/bin/python")
FETCH_SCRIPT = ROOT / "src" / "data_sources" / "yfinance_fetch.py"


class YFinanceAdapter:
    def __init__(self, python_path: Path = AGU_PYTHON) -> None:
        self.python_path = python_path
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def fetch_history(
        self,
        code: str,
        period: str,
        interval: str,
        refresh: bool = False,
    ) -> pd.DataFrame:
        args = [code, "--period", period, "--interval", interval]
        cache_file = CACHE_DIR / f"{self._cache_key(args)}.json"
        if cache_file.exists() and not refresh:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            command = [str(self.python_path), str(FETCH_SCRIPT), *args]
            result = subprocess.run(
                command,
                cwd=ROOT,
                env={**os.environ, "PYTHONNOUSERSITE": "1"},
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"yfinance fetch failed: {code} {period} {interval} | {stderr}")
            payload = json.loads(result.stdout)
            cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        df = pd.DataFrame(payload)
        if df.empty:
            return df
        df["datetime"] = pd.to_datetime(df["datetime"])
        df["date"] = df["datetime"].dt.tz_localize(None).dt.normalize()
        df["time"] = df["datetime"].dt.tz_localize(None).dt.strftime("%H:%M")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["amount"] = df["close"] * df["volume"]
        return df.sort_values("datetime").reset_index(drop=True)

    @staticmethod
    def _cache_key(args: list[str]) -> str:
        return hashlib.sha1(" ".join(args).encode("utf-8")).hexdigest()
