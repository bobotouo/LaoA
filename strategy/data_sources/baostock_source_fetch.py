from __future__ import annotations

import argparse
import json
from typing import Any


def _bs_code(code: str) -> str:
    text = str(code).strip().lower()
    if "." in text:
        symbol, market = text.split(".", 1)
        return f"{market}.{symbol}"
    if text.startswith(("600", "601", "603", "605", "688")):
        return f"sh.{text}"
    if text.startswith(("8", "9")):
        return f"bj.{text}"
    return f"sz.{text}"


def _full_code(code: str) -> str:
    text = str(code).strip().upper()
    if "." in text:
        return text
    if text.startswith(("600", "601", "603", "605", "688")):
        return f"{text}.SH"
    if text.startswith(("8", "9")):
        return f"{text}.BJ"
    return f"{text}.SZ"


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _query_one(bs: Any, code: str, start: str, end: str, adjustflag: str) -> list[dict[str, Any]]:
    fields = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn"
    result = bs.query_history_k_data_plus(
        _bs_code(code),
        fields,
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag=adjustflag,
    )
    if result.error_code != "0":
        return []
    rows: list[dict[str, Any]] = []
    while result.next():
        row = dict(zip(result.fields, result.get_row_data()))
        rows.append(
            {
                "code": _full_code(code),
                "date": row.get("date"),
                "open": _number(row.get("open")),
                "high": _number(row.get("high")),
                "low": _number(row.get("low")),
                "close": _number(row.get("close")),
                "prev_close": _number(row.get("preclose")),
                # BaoStock volume is shares; project volume is hands.
                "volume": (_number(row.get("volume")) or 0.0) / 100.0,
                "amount": _number(row.get("amount")),
                "turnover_rate": _number(row.get("turn")),
                "data_source": "baostock",
                "adjustment": {"1": "hfq", "2": "qfq", "3": "none"}.get(adjustflag, "none"),
            }
        )
    return rows


def _stock_list(bs: Any) -> list[dict[str, Any]]:
    result = bs.query_stock_basic()
    if result.error_code != "0":
        return []
    rows: list[dict[str, Any]] = []
    while result.next():
        row = dict(zip(result.fields, result.get_row_data()))
        raw_code = str(row.get("code", ""))
        symbol = raw_code.split(".")[-1]
        if row.get("type") != "1" or not symbol.startswith(("0", "3", "6")):
            continue
        suffix = ".SH" if raw_code.startswith("sh.") else ".SZ"
        rows.append(
            {
                "code": symbol,
                "full_code": symbol + suffix,
                "name": row.get("code_name", ""),
                "market": 1 if suffix == ".SH" else 0,
                "list_status": row.get("status", ""),
                "list_date": row.get("ipoDate", ""),
                "out_date": row.get("outDate", ""),
                "data_source": "baostock",
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Free BaoStock historical data helper.")
    parser.add_argument("codes", nargs="*")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--adjust", choices=["none", "qfq", "hfq"], default="qfq")
    parser.add_argument("--stock-list", action="store_true")
    args = parser.parse_args()

    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        print("[]")
        return 0
    try:
        if args.stock_list:
            rows = _stock_list(bs)
        else:
            if not args.codes or not args.start or not args.end:
                raise ValueError("history requests require codes, --start, and --end")
            adjustflag = {"none": "3", "qfq": "2", "hfq": "1"}[args.adjust]
            rows = []
            for code in args.codes:
                rows.extend(_query_one(bs, code, args.start, args.end, adjustflag))
    finally:
        bs.logout()
    print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
