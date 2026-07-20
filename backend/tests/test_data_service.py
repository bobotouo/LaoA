import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.data_service import MarketDataError, MarketDataService


SHANGHAI = ZoneInfo("Asia/Shanghai")


class MarketDataServiceTests(unittest.TestCase):
    def test_format_limit_time(self) -> None:
        self.assertEqual(MarketDataService._format_limit_time(92500), "09:25:00")
        self.assertEqual(MarketDataService._format_limit_time(130501), "13:05:01")
        self.assertEqual(MarketDataService._format_limit_time(None), "--")

    def test_trading_session_detection(self) -> None:
        open_time = datetime(2026, 7, 20, 10, 30, tzinfo=SHANGHAI)
        closed_time = datetime(2026, 7, 20, 16, 0, tzinfo=SHANGHAI)
        weekend = datetime(2026, 7, 19, 10, 30, tzinfo=SHANGHAI)

        self.assertTrue(MarketDataService._is_trading_time(open_time))
        self.assertFalse(MarketDataService._is_trading_time(closed_time))
        self.assertFalse(MarketDataService._is_trading_time(weekend))

    def test_stale_fallback_uses_last_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last_dashboard.json"
            service = MarketDataService(last_dashboard_path=path)
            snapshot = {
                "meta": {
                    "mode": "live",
                    "stale": False,
                    "source": "test",
                    "updatedAt": "2026-07-20T10:00:00+08:00",
                    "isTrading": True,
                    "message": "ok",
                },
                "indices": [{"code": "000001", "name": "上证指数"}],
                "sectors": [{"code": "BK0001", "name": "测试板块"}],
                "breadth": {"up": 1, "flat": 0, "down": 0, "distribution": []},
                "turnover": {"current": 1},
                "limitPool": [],
            }
            service._save_last_dashboard(snapshot)
            service._build_live_dashboard = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

            result = service.get_dashboard(refresh=True)

            self.assertTrue(result["meta"]["stale"])
            self.assertEqual(result["sectors"][0]["name"], "测试板块")
            self.assertIn("最近一次成功行情", result["meta"]["message"])

    def test_failure_without_snapshot_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            service = MarketDataService(last_dashboard_path=path)
            service._build_live_dashboard = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

            with self.assertRaises(MarketDataError):
                service.get_dashboard(refresh=True)


if __name__ == "__main__":
    unittest.main()
