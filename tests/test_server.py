import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import server


class DashboardLogicTests(unittest.TestCase):
    def test_symbol_mapping(self):
        self.assertEqual(server.symbol_for("600000"), "sh600000")
        self.assertEqual(server.symbol_for("300847"), "sz300847")
        self.assertEqual(server.symbol_for("561360"), "sh561360")

    def test_phase_clock(self):
        decision = datetime(2026, 7, 28, 14, 30, tzinfo=server.SHANGHAI)
        exit_time = datetime(2026, 7, 28, 9, 35, tzinfo=server.SHANGHAI)
        closed = datetime(2026, 7, 28, 15, 30, tzinfo=server.SHANGHAI)
        self.assertEqual(server.determine_phase(decision, "20260728")[0], "decision")
        self.assertEqual(server.determine_phase(exit_time, "20260728")[0], "exit")
        self.assertEqual(server.determine_phase(closed, "20260728")[0], "closed")

    def test_minute_cutoff_and_vwap(self):
        quote = [""] * 6
        quote[1] = "测试股票"
        quote[4] = "10.00"
        raw = {
            "symbol": "sz000001",
            "trade_date": "20260728",
            "quote": quote,
            "raw_records": [
                "0930 10.00 100 100000.00",
                "1400 10.10 200 201000.00",
                "1430 10.20 300 303000.00",
                "1431 12.00 400 423000.00",
            ],
        }
        parsed = server.parse_minute(raw, 14 * 60 + 30)
        self.assertEqual(parsed["last_time"], "1430")
        self.assertEqual(parsed["price"], 10.2)
        self.assertAlmostEqual(parsed["vwap"], 10.1)
        self.assertEqual(len(parsed["records"]), 3)

    def test_indicators_on_rising_series(self):
        bars = []
        for index in range(45):
            close = 10 + index * 0.08
            bars.append({
                "date": f"2026-06-{index + 1:02d}",
                "open": close - 0.03,
                "close": close,
                "high": close + 0.08,
                "low": close - 0.08,
                "volume": 1000 + index * 10,
            })
        indicators = server.calculate_indicators(bars)
        self.assertEqual(indicators["trend"], "多头排列")
        self.assertGreater(indicators["dif"], indicators["dea"])
        self.assertEqual(len(indicators["chart_bars"]), 45)

    def test_market_red_gate(self):
        rows = [
            {"symbol": "sh000001", "change_pct": -1.8, "tail_return_pct": -0.2},
            {"symbol": "sz399001", "change_pct": -2.3, "tail_return_pct": -0.5},
            {"symbol": "sz399006", "change_pct": -3.2, "tail_return_pct": -0.7},
            {"symbol": "sh000688", "change_pct": -2.8, "tail_return_pct": -0.6},
        ]
        self.assertEqual(server.market_state(rows)["state"], "red")

    def test_wecom_webhook_validation(self):
        official = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=00000000-0000-0000-0000-000000000000"
        self.assertEqual(server.validate_wecom_webhook(official), official)
        with self.assertRaises(ValueError):
            server.validate_wecom_webhook("https://example.com/cgi-bin/webhook/send?key=00000000-0000-0000-0000-000000000000")

    def test_push_status_never_exposes_webhook(self):
        official = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=00000000-0000-0000-0000-000000000000"
        with TemporaryDirectory() as directory:
            data_dir = Path(directory)
            config_path = data_dir / "wecom-push.json"
            with patch.object(server, "DATA_DIR", data_dir), patch.object(server, "PUSH_CONFIG_PATH", config_path):
                status = server.save_push_config({"webhook": official, "enabled": True})
                self.assertTrue(status["configured"])
                self.assertTrue(status["enabled"])
                self.assertNotIn("webhook", status)
                self.assertNotIn("key=", str(status))

    def test_wecom_message_contains_decision_and_candidate(self):
        analysis = {
            "trade_date": "20260728",
            "headline": "没有满足条件的买点，建议空仓",
            "market": {"label": "偏弱"},
            "position_pct": 0,
            "data_as_of": "2026-07-28 14:30",
            "candidates": [{
                "name": "测试股份",
                "code": "000001",
                "action": "观察",
                "price": 10.2,
                "position_pct": 0,
                "reward_risk": 1.2,
                "failures": ["量价条件不足"],
            }],
        }
        message = server.format_wecom_message(analysis)
        self.assertIn(analysis["headline"], message)
        self.assertIn("测试股份 000001", message)

    def test_push_once_skips_stale_market_data(self):
        now = datetime(2026, 7, 28, 14, 31, tzinfo=server.SHANGHAI)
        analysis = {"trade_date": "20260727", "phase": "closed"}
        official = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=00000000-0000-0000-0000-000000000000"
        with patch("server.get_analysis", return_value=analysis), patch("server.send_wecom_push") as sender:
            result = server.push_once(official, now=now)
        self.assertFalse(result["sent"])
        sender.assert_not_called()

    def test_push_once_sends_current_decision(self):
        now = datetime(2026, 7, 28, 14, 31, tzinfo=server.SHANGHAI)
        analysis = {
            "trade_date": "20260728",
            "phase": "decision",
            "headline": "空仓，仓位 0%",
            "position_pct": 0,
        }
        official = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=00000000-0000-0000-0000-000000000000"
        with patch("server.get_analysis", return_value=analysis), patch("server.send_wecom_push") as sender:
            result = server.push_once(official, now=now)
        self.assertTrue(result["sent"])
        sender.assert_called_once_with(analysis, webhook=official)


if __name__ == "__main__":
    unittest.main()
