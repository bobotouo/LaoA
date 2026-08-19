from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
from typing import Any
import hashlib
import json
import re
import subprocess

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "cache"
FINSHARE_PYTHON = Path("/Users/bobo/Desktop/project/agu-skill/.venv/bin/python")
FINSHARE_CLI = Path(
    "/Users/bobo/Desktop/project/agu-skill/skills/a-share-finshare/scripts/finshare_cli.py"
)


@dataclass(frozen=True)
class FinsharePaths:
    python: Path = FINSHARE_PYTHON
    cli: Path = FINSHARE_CLI


class FinshareAdapter:
    def __init__(self, paths: FinsharePaths | None = None, home_dir: Path | None = None) -> None:
        self.paths = paths or FinsharePaths()
        self.home_dir = home_dir or ROOT
        self._last_fetch_time: pd.Timestamp | None = None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def fetch_stock_list(
        self,
        market: str = "all",
        limit: int = 120,
        refresh: bool = False,
    ) -> pd.DataFrame:
        try:
            payload = self._run_json(
                ["stock-list", "--market", market, "--limit", str(limit), "--json", "--quiet"],
                refresh=refresh,
                cache_on_empty=False,
            )
        except Exception:
            payload = self._latest_cached_stock_list(market=market, limit=limit)
        if not payload:
            payload = self._latest_cached_stock_list(market=market, limit=limit)
        df = pd.DataFrame(payload)
        if df.empty:
            return df
        df["data_source"] = "finshare"
        if self._last_fetch_time is not None:
            df["data_date"] = self._last_fetch_time.normalize()
        df["full_code"] = df.apply(self._full_code_from_row, axis=1)
        df["name"] = df["name"].astype(str)
        for col in ("price", "change_pct", "amount", "volume", "prev_close"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def _latest_cached_stock_list(self, market: str, limit: int) -> list[dict[str, Any]]:
        """Reuse the newest non-empty stock-list cache across different limits."""
        candidates: list[tuple[float, Path, list[dict[str, Any]]]] = []
        for cache_file in CACHE_DIR.glob("*.json"):
            try:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, list) or not payload:
                continue
            if not isinstance(payload[0], dict) or not {"code", "name", "price"}.issubset(payload[0]):
                continue
            candidates.append((cache_file.stat().st_mtime, cache_file, payload))
        if not candidates:
            return []
        _, cache_file, payload = max(candidates, key=lambda item: item[0])
        self._last_fetch_time = pd.Timestamp(cache_file.stat().st_mtime, unit="s")
        if market not in {"sh", "sz"}:
            return payload[:limit] if limit > 0 else payload
        filtered = [
            row
            for row in payload
            if self._full_code_from_row(pd.Series(row)).endswith(f".{market.upper()}")
        ]
        return filtered[:limit] if limit > 0 else filtered

    def fetch_batch_snapshots(self, codes: list[str]) -> pd.DataFrame:
        if not codes:
            return pd.DataFrame()
        payload = self._run_json(
            ["batch-snapshot", *codes, "--json", "--quiet"],
            refresh=True,
            cache_on_empty=False,
        )
        if isinstance(payload, dict):
            rows = list(payload.values())
        else:
            rows = payload
        df = pd.DataFrame(rows or [])
        if df.empty:
            return df
        if "code" not in df.columns:
            df["code"] = codes[: len(df)]
        for col in (
            "last_price",
            "change",
            "change_pct",
            "prev_close",
            "day_open",
            "day_high",
            "day_low",
            "volume",
            "amount",
            "bid1_price",
            "ask1_price",
            "bid1_volume",
            "ask1_volume",
        ):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def fetch_kline(
        self,
        code: str,
        start: str,
        end: str,
        period: str = "daily",
        adjust: str = "qfq",
        refresh: bool = False,
    ) -> pd.DataFrame:
        payload = self._run_json(
            [
                "kline",
                code,
                "--start",
                start,
                "--end",
                end,
                "--period",
                period,
                "--adjust",
                adjust,
                "--json",
                "--quiet",
            ],
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
        df["date"] = pd.to_datetime(df["date"])
        for col in ("open", "high", "low", "close", "volume", "amount", "turnover_rate"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        df["code"] = code
        return df

    def fetch_minutely(
        self,
        code: str,
        start: str,
        end: str,
        freq: int = 5,
        adjust: str = "qfq",
        refresh: bool = False,
    ) -> pd.DataFrame:
        payload = self._run_json(
            [
                "minutely",
                code,
                "--start",
                start,
                "--end",
                end,
                "--freq",
                str(freq),
                "--adjust",
                adjust,
                "--json",
                "--quiet",
            ],
            refresh=refresh,
        )
        df = pd.DataFrame(payload)
        if df.empty:
            return df
        df = df.rename(
            columns={
                "trade_time": "datetime",
                "open_price": "open",
                "high_price": "high",
                "low_price": "low",
                "close_price": "close",
            }
        )
        if "datetime" not in df.columns:
            time_col = next((col for col in ("time", "date", "timestamp") if col in df.columns), None)
            if time_col is None:
                raise RuntimeError(f"minutely payload has no time column: {list(df.columns)}")
            df = df.rename(columns={time_col: "datetime"})
        df["datetime"] = pd.to_datetime(df["datetime"])
        df["date"] = df["datetime"].dt.normalize()
        df["time"] = df["datetime"].dt.strftime("%H:%M")
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("datetime").reset_index(drop=True)
        df["code"] = code
        return df

    def _run_json(
        self,
        args: list[str],
        refresh: bool,
        cache_on_empty: bool = True,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        cache_file = CACHE_DIR / f"{self._cache_key(args)}.json"
        if cache_file.exists() and not refresh:
            self._last_fetch_time = pd.Timestamp(cache_file.stat().st_mtime, unit="s")
            return json.loads(cache_file.read_text(encoding="utf-8"))

        command = [str(self.paths.python), str(self.paths.cli), *args]
        result = subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home_dir)},
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"finshare command failed: {' '.join(args)} | {stderr}")

        payload_text = self._extract_json(result.stdout)
        payload = json.loads(payload_text)
        self._last_fetch_time = pd.Timestamp.now()
        if cache_on_empty or payload:
            cache_file.write_text(payload_text, encoding="utf-8")
        return payload

    @staticmethod
    def _extract_json(stdout: str) -> str:
        if "(empty)" in stdout:
            return "[]"
        cleaned = re.sub(r"\x1b\[[0-9;]*m", "", stdout)
        decoder = json.JSONDecoder()
        for index, character in enumerate(cleaned):
            if character not in "[{":
                continue
            try:
                _, end = decoder.raw_decode(cleaned[index:])
                return cleaned[index : index + end]
            except json.JSONDecodeError:
                continue
        raise RuntimeError(f"unable to parse finshare json payload: {stdout[:200]}")

    @staticmethod
    def _cache_key(args: list[str]) -> str:
        digest = hashlib.sha1(" ".join(args).encode("utf-8")).hexdigest()
        return digest

    @staticmethod
    def _full_code_from_row(row: pd.Series) -> str:
        code = str(row.get("code", "")).strip()
        market = str(row.get("market", "")).strip()
        if "." in code:
            return code
        if market == "1":
            return f"{code}.SH"
        if market == "0":
            return f"{code}.SZ"
        if code.startswith(("600", "601", "603", "605", "688")):
            return f"{code}.SH"
        return f"{code}.SZ"


def date_to_text(value: date) -> str:
    return value.strftime("%Y-%m-%d")
