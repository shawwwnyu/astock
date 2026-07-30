import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import server


class DashboardLogicTests(unittest.TestCase):
    WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=00000000-0000-0000-0000-000000000000"

    @staticmethod
    def analysis(
        *,
        trade_date="20260729",
        candidates=None,
        universe=None,
        trading_dates=None,
        market_state="green",
    ):
        return {
            "trade_date": trade_date,
            "data_as_of": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 10:30",
            "market": {"state": market_state, "label": "绿灯", "summary": "市场结构稳定"},
            "market_data_time": "1500",
            "indices": [],
            "breadth": {"up": 1, "down": 0, "flat": 0, "average_change_pct": 1.0},
            "candidates": candidates or [],
            "universe": universe or [],
            "trading_dates": trading_dates or [trade_date],
        }

    @staticmethod
    def candidate(code="000001"):
        return {
            "code": code,
            "name": "测试股份",
            "price": 10.0,
            "entry_range": "9.97-10.03",
            "shares": 3000,
            "position_pct": 60,
            "capital_used": 30000,
            "invalidation": 9.5,
            "resistance": 11.2,
        }

    @staticmethod
    def holding(code="000001", buy_date="20260728", buy_price=10.0):
        return {
            "id": f"{buy_date}-1430-{code}",
            "code": code,
            "name": "测试股份",
            "buy_date": buy_date,
            "buy_time": "1430",
            "buy_price": buy_price,
            "shares": 3000,
            "capital_used": 30000,
            "invalidation": 9.5,
            "target_price": 11.2,
            "source": "tail",
            "status": "open",
        }

    def test_symbol_mapping(self):
        self.assertEqual(server.symbol_for("600000"), "sh600000")
        self.assertEqual(server.symbol_for("300847"), "sz300847")
        self.assertEqual(server.symbol_for("561360"), "sh561360")

    def test_board_lot_sizes(self):
        self.assertEqual(server.lot_size_for("688037"), 200)
        self.assertEqual(server.lot_size_for("300847"), 100)

    def test_phase_clock(self):
        decision = datetime(2026, 7, 28, 14, 30, tzinfo=server.SHANGHAI)
        morning = datetime(2026, 7, 28, 10, 30, tzinfo=server.SHANGHAI)
        closed = datetime(2026, 7, 28, 15, 30, tzinfo=server.SHANGHAI)
        self.assertEqual(server.determine_phase(decision, "20260728")[0], "decision")
        self.assertEqual(server.determine_phase(morning, "20260728")[0], "morning-decision")
        self.assertEqual(server.determine_phase(closed, "20260728")[0], "closed")

    def test_scheduled_stage_cutoffs(self):
        self.assertEqual(server.SCHEDULED_STAGES["morning-summary"]["cutoff"], 10 * 60)
        self.assertEqual(server.SCHEDULED_STAGES["morning-decision"]["cutoff"], 10 * 60 + 30)
        self.assertEqual(server.SCHEDULED_STAGES["tail-decision"]["cutoff"], 14 * 60 + 30)
        self.assertEqual(server.SCHEDULED_STAGES["close-summary"]["cutoff"], 15 * 60)

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
        self.assertEqual(server.validate_wecom_webhook(self.WEBHOOK), self.WEBHOOK)
        with self.assertRaises(ValueError):
            server.validate_wecom_webhook("https://example.com/cgi-bin/webhook/send?key=00000000-0000-0000-0000-000000000000")

    def test_push_status_never_exposes_webhook(self):
        with TemporaryDirectory() as directory:
            data_dir = Path(directory)
            config_path = data_dir / "wecom-push.json"
            with patch.object(server, "DATA_DIR", data_dir), patch.object(server, "PUSH_CONFIG_PATH", config_path):
                status = server.save_push_config({"webhook": self.WEBHOOK, "enabled": True})
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

    def test_scheduled_push_skips_stale_market_data(self):
        now = datetime(2026, 7, 29, 14, 30, tzinfo=server.SHANGHAI)
        analysis = self.analysis(trade_date="20260728")
        with patch("server.run_analysis", return_value=analysis), patch("server.send_wecom_push") as sender:
            result = server.scheduled_push("tail-decision", self.WEBHOOK, now=now)
        self.assertFalse(result["sent"])
        sender.assert_not_called()

    def test_scheduled_push_skips_before_stage_cutoff(self):
        now = datetime(2026, 7, 29, 12, 30, tzinfo=server.SHANGHAI)
        analysis = self.analysis()
        analysis["market_data_time"] = "1130"
        with patch("server.run_analysis", return_value=analysis), patch("server.send_wecom_push") as sender:
            result = server.scheduled_push("tail-decision", self.WEBHOOK, now=now)
        self.assertFalse(result["sent"])
        self.assertEqual(result["required_time"], "14:30")
        sender.assert_not_called()

    def test_successful_tail_push_creates_assumed_holding(self):
        now = datetime(2026, 7, 29, 14, 30, tzinfo=server.SHANGHAI)
        analysis = self.analysis(candidates=[self.candidate()])
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", return_value=analysis), patch("server.send_wecom_push") as sender:
                result = server.scheduled_push(
                    "tail-decision", self.WEBHOOK, now=now, state_path=state_path,
                )
            state = server.load_trading_state(state_path)
        self.assertTrue(result["sent"])
        self.assertEqual(state["holdings"][0]["buy_price"], 10.0)
        self.assertEqual(state["holdings"][0]["buy_time"], "1430")
        sender.assert_called_once()

    def test_failed_push_does_not_create_state(self):
        now = datetime(2026, 7, 29, 14, 30, tzinfo=server.SHANGHAI)
        analysis = self.analysis(candidates=[self.candidate()])
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", return_value=analysis), patch(
                "server.send_wecom_push", side_effect=RuntimeError("send failed"),
            ):
                with self.assertRaises(RuntimeError):
                    server.scheduled_push(
                        "tail-decision", self.WEBHOOK, now=now, state_path=state_path,
                    )
            self.assertFalse(state_path.exists())

    def test_failed_push_preserves_existing_holding(self):
        now = datetime(2026, 7, 29, 10, 30, tzinfo=server.SHANGHAI)
        holding = self.holding(buy_date="20260724")
        stock = {
            "code": "000001", "price": 11.0, "vwap": 10.9, "change_pct": 1,
            "tail_return_pct": 0.2, "trend": "多头排列",
        }
        analysis = self.analysis(
            universe=[stock],
            trading_dates=["20260724", "20260727", "20260728", "20260729"],
        )
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            initial = server.empty_trading_state()
            initial["holdings"] = [holding]
            server.save_trading_state(initial, state_path)
            before = state_path.read_text(encoding="utf-8")
            with patch("server.run_analysis", return_value=analysis), patch(
                "server.send_wecom_push", side_effect=RuntimeError("send failed"),
            ):
                with self.assertRaises(RuntimeError):
                    server.scheduled_push(
                        "morning-decision", self.WEBHOOK, now=now, state_path=state_path,
                    )
            after = state_path.read_text(encoding="utf-8")
        self.assertEqual(after, before)

    def test_t_plus_one_without_sell_signal_keeps_holding(self):
        holding = self.holding()
        stock = {
            "code": "000001", "price": 10.4, "vwap": 10.3, "change_pct": 1,
            "tail_return_pct": 0.2, "trend": "多头排列",
        }
        analysis = self.analysis(universe=[stock], trading_dates=["20260728", "20260729"])
        reviews = server.review_holdings({"holdings": [holding]}, analysis)
        self.assertEqual(reviews[0]["decision"], "hold")
        self.assertEqual(reviews[0]["holding_session"], 1)
        self.assertEqual(reviews[0]["return_pct"], 4.0)

    def test_third_sellable_day_forces_sale_and_reports_profit(self):
        holding = self.holding(buy_date="20260724")
        stock = {
            "code": "000001", "price": 11.0, "vwap": 10.9, "change_pct": 1,
            "tail_return_pct": 0.2, "trend": "多头排列",
        }
        analysis = self.analysis(
            universe=[stock],
            trading_dates=["20260724", "20260727", "20260728", "20260729"],
        )
        report = server.build_scheduled_report(
            "morning-decision", analysis, {"holdings": [holding]},
        )
        review = report["holding_reviews"][0]
        self.assertEqual(review["decision"], "sell")
        self.assertEqual(review["holding_session"], 3)
        self.assertEqual(review["return_pct"], 10.0)
        self.assertIn("+10.00%", server.format_scheduled_message(report))

    def test_successful_sell_moves_holding_to_history(self):
        now = datetime(2026, 7, 29, 10, 30, tzinfo=server.SHANGHAI)
        holding = self.holding(buy_date="20260724")
        stock = {
            "code": "000001", "price": 11.0, "vwap": 10.9, "change_pct": 1,
            "tail_return_pct": 0.2, "trend": "多头排列",
        }
        analysis = self.analysis(
            universe=[stock],
            trading_dates=["20260724", "20260727", "20260728", "20260729"],
        )
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            initial = server.empty_trading_state()
            initial["holdings"] = [holding]
            server.save_trading_state(initial, state_path)
            with patch("server.run_analysis", return_value=analysis), patch("server.send_wecom_push"):
                server.scheduled_push(
                    "morning-decision", self.WEBHOOK, now=now, state_path=state_path,
                )
            state = server.load_trading_state(state_path)
        self.assertEqual(state["holdings"], [])
        self.assertEqual(state["history"][0]["return_pct"], 10.0)

    def test_open_holding_blocks_tail_buy(self):
        analysis = self.analysis(candidates=[self.candidate("000002")])
        report = server.build_scheduled_report(
            "tail-decision", analysis, {"holdings": [self.holding()]},
        )
        self.assertEqual(report["buys"], [])
        self.assertEqual(report["trade_actions"], 0)

    def test_no_signal_message_has_no_candidate_execution(self):
        report = server.build_scheduled_report(
            "tail-decision", self.analysis(), server.empty_trading_state(),
        )
        message = server.format_scheduled_message(report)
        self.assertIn("本次没有买点和卖点，不操作", message)
        self.assertNotIn("**买入", message)
        self.assertNotIn("**卖出", message)

    def test_successful_event_is_idempotent(self):
        now = datetime(2026, 7, 29, 15, 30, tzinfo=server.SHANGHAI)
        analysis = self.analysis()
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", return_value=analysis), patch("server.send_wecom_push") as sender:
                first = server.scheduled_push(
                    "close-summary", self.WEBHOOK, now=now, state_path=state_path,
                )
                second = server.scheduled_push(
                    "close-summary", self.WEBHOOK, now=now, state_path=state_path,
                )
        self.assertTrue(first["sent"])
        self.assertFalse(second["sent"])
        self.assertEqual(sender.call_count, 1)


if __name__ == "__main__":
    unittest.main()
