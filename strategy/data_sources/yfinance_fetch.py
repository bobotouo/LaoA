from __future__ import annotations

import argparse
import json
import sys

import pandas as pd
import yfinance as yf


def to_yahoo_symbol(code: str) -> str:
    text = str(code).strip().upper()
    if text.endswith(".SH"):
        return text[:-3] + ".SS"
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("code")
    parser.add_argument("--period", default="60d")
    parser.add_argument("--interval", default="60m")
    args = parser.parse_args()

    symbol = to_yahoo_symbol(args.code)
    df = yf.Ticker(symbol).history(period=args.period, interval=args.interval)
    if df.empty:
        print("[]")
        return 0
    df = df.reset_index()
    rows = []
    for _, row in df.iterrows():
        dt_value = row.get("Datetime", row.get("Date"))
        timestamp = pd.Timestamp(dt_value)
        rows.append(
            {
                "code": args.code,
                "datetime": timestamp.isoformat(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            }
        )
    print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
