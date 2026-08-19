from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime
import json
from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    if is_dataclass(value):
        return asdict(value)
    return dict(value)


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Call a specific finshare source.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    kline = subparsers.add_parser("tdx-kline")
    kline.add_argument("code")
    kline.add_argument("--start", required=True)
    kline.add_argument("--end", required=True)

    snapshots = subparsers.add_parser("tdx-batch-snapshot")
    snapshots.add_argument("codes", nargs="+")

    args = parser.parse_args()
    from finshare.sources.tdx_source import TdxDataSource
    from finshare.models.data_models import AdjustmentType

    source = TdxDataSource()
    if args.command == "tdx-kline":
        rows = source.get_historical_data(
            args.code,
            datetime.strptime(args.start, "%Y-%m-%d").date(),
            datetime.strptime(args.end, "%Y-%m-%d").date(),
            AdjustmentType.NONE,
        )
        payload = [_as_dict(row) for row in rows]
    else:
        payload = {
            code: _as_dict(snapshot)
            for code, snapshot in source.get_batch_snapshots(args.codes).items()
            if snapshot is not None
        }
    print(json.dumps(payload, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
