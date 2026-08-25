import io
import json
import sys
import threading
import unittest
from datetime import datetime, timedelta
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

    def test_lightclaw_prepare_never_sends_or_applies_state(self):
        now = datetime(2026, 7, 29, 14, 30, tzinfo=server.SHANGHAI)
        analysis = self.analysis(candidates=[self.candidate()])
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch.dict("os.environ", {
                "WECOM_WEBHOOK": self.WEBHOOK,
                "RESOURCE_STATS_FEISHU_WEBHOOK": "must-not-be-read",
            }), patch("server.run_analysis", return_value=analysis), patch(
                "server.send_wecom_push",
            ) as sender, patch("urllib.request.urlopen") as post:
                result = server.lightclaw_prepare(
                    "tail-decision", now=now, state_path=state_path, test_message=True,
                )
            state = server.load_trading_state(state_path)
        self.assertEqual(result["status"], "prepared")
        self.assertIn("LightClaw 测试", result["message"])
        self.assertIn("当前飞书会话确认送达后", result["message"])
        self.assertNotIn("企业微信机器人", result["message"])
        self.assertEqual(state["holdings"], [])
        self.assertEqual(state["processed_events"], [])
        self.assertEqual(state["pending_report"]["event_id"], result["event_id"])
        sender.assert_not_called()
        post.assert_not_called()

    def test_lightclaw_commit_applies_and_duplicate_is_idempotent(self):
        now = datetime(2026, 7, 29, 14, 30, tzinfo=server.SHANGHAI)
        analysis = self.analysis(candidates=[self.candidate()])
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", return_value=analysis):
                prepared = server.lightclaw_prepare("tail-decision", now=now, state_path=state_path)
            first = server.lightclaw_commit(prepared["commit_token"], now=now, state_path=state_path)
            second = server.lightclaw_commit(prepared["commit_token"], now=now, state_path=state_path)
            state = server.load_trading_state(state_path)
        self.assertEqual(first["status"], "committed")
        self.assertEqual(second["status"], "already_committed")
        self.assertEqual(state["committed_deliveries"], [])
        self.assertEqual(state["committed_delivery_tokens"], {prepared["commit_token"]: prepared["event_id"]})
        self.assertEqual(len(state["holdings"]), 1)
        self.assertEqual(state["processed_events"], [prepared["event_id"]])
        self.assertIsNone(state["pending_report"])

    def test_lightclaw_oldest_token_remains_idempotent_after_121_more_commits(self):
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state = server.empty_trading_state()
            state["committed_deliveries"] = [
                {
                    "event_id": f"202601{index:02d}:tail-decision",
                    "commit_token": f"token-{index}",
                    "committed_at": "2026-01-01T14:30:00+08:00",
                }
                for index in range(1, 123)
            ]
            server.save_trading_state(state, state_path)
            result = server.lightclaw_commit("token-1", state_path=state_path)
            loaded = server.load_trading_state(state_path)

        self.assertEqual(result["status"], "already_committed")
        self.assertEqual(result["event_id"], "20260101:tail-decision")
        self.assertEqual(len(loaded["committed_delivery_tokens"]), 122)

    def test_lightclaw_commit_reads_existing_minimal_token_map(self):
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state = server.empty_trading_state()
            state["committed_delivery_tokens"] = {"old-token": "20251231:close-summary"}
            server.save_trading_state(state, state_path)

            result = server.lightclaw_commit("old-token", state_path=state_path)

        self.assertEqual(result["status"], "already_committed")
        self.assertEqual(result["event_id"], "20251231:close-summary")

    def test_lightclaw_prepare_skips_closed_or_early_market_without_state(self):
        now = datetime(2026, 7, 29, 14, 30, tzinfo=server.SHANGHAI)
        closed = self.analysis(trade_date="20260728")
        early = self.analysis()
        early["market_data_time"] = "1429"
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", return_value=closed):
                closed_result = server.lightclaw_prepare("tail-decision", now=now, state_path=state_path)
            with patch("server.run_analysis", return_value=early):
                early_result = server.lightclaw_prepare("tail-decision", now=now, state_path=state_path)
            self.assertFalse(state_path.exists())
        self.assertEqual(closed_result["status"], "skipped")
        self.assertEqual(early_result["status"], "skipped")

    def test_lightclaw_pending_over_twenty_minutes_reuses_and_old_token_commits(self):
        now = datetime(2026, 7, 29, 14, 30, tzinfo=server.SHANGHAI)
        delayed = now + timedelta(minutes=21)
        analysis = self.analysis(candidates=[self.candidate()])
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", return_value=analysis) as analyzer:
                first = server.lightclaw_prepare("tail-decision", now=now, state_path=state_path)
                reused = server.lightclaw_prepare("tail-decision", now=delayed, state_path=state_path)
            committed = server.lightclaw_commit(first["commit_token"], now=delayed, state_path=state_path)
            state = server.load_trading_state(state_path)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["commit_token"], first["commit_token"])
        self.assertEqual(reused["message"], first["message"])
        self.assertEqual(analyzer.call_count, 1)
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(state["processed_events"], [first["event_id"]])
        self.assertEqual(len(state["holdings"]), 1)

    def test_scheduled_push_conflicts_with_pending_without_send_or_apply(self):
        now = datetime(2026, 7, 29, 14, 30, tzinfo=server.SHANGHAI)
        analysis = self.analysis(candidates=[self.candidate()])
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", return_value=analysis):
                prepared = server.lightclaw_prepare("tail-decision", now=now, state_path=state_path)
            before = state_path.read_bytes()
            with patch("server.run_analysis", return_value=analysis) as analyzer, patch(
                "server.send_wecom_push",
            ) as sender, patch("server.persist_decision") as persister, patch(
                "server.apply_scheduled_report",
            ) as applier, patch("server.save_trading_state") as saver:
                result = server.scheduled_push(
                    "tail-decision", self.WEBHOOK, now=now, state_path=state_path,
                )
            after = state_path.read_bytes()
            committed = server.lightclaw_commit(prepared["commit_token"], now=now, state_path=state_path)
            state = server.load_trading_state(state_path)
        self.assertEqual(result["status"], "pending_conflict")
        self.assertFalse(result["sent"])
        self.assertEqual(before, after)
        analyzer.assert_not_called()
        sender.assert_not_called()
        persister.assert_not_called()
        applier.assert_not_called()
        saver.assert_not_called()
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(state["processed_events"], [prepared["event_id"]])

    def test_scheduled_push_rechecks_pending_after_analysis_before_side_effects(self):
        now = datetime(2026, 7, 29, 14, 30, tzinfo=server.SHANGHAI)
        analysis = self.analysis(candidates=[self.candidate()])
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"

            def create_concurrent_pending(*args, **kwargs):
                state = server.empty_trading_state()
                state["pending_report"] = {
                    "event_id": "20260729:tail-decision",
                    "commit_token": "concurrent-token",
                }
                with server.trading_state_transaction(state_path) as locked_path:
                    server.save_trading_state(state, locked_path)
                return analysis

            with patch("server.run_analysis", side_effect=create_concurrent_pending) as analyzer, patch(
                "server.send_wecom_push",
            ) as sender, patch("server.persist_decision") as persister, patch(
                "server.apply_scheduled_report",
            ) as applier:
                result = server.scheduled_push(
                    "tail-decision", self.WEBHOOK, now=now, state_path=state_path,
                )

        self.assertEqual(result["status"], "pending_conflict")
        self.assertEqual(analyzer.call_count, 1)
        sender.assert_not_called()
        persister.assert_not_called()
        applier.assert_not_called()

    def test_scheduled_push_skips_event_already_committed_by_lightclaw(self):
        now = datetime(2026, 7, 29, 14, 30, tzinfo=server.SHANGHAI)
        analysis = self.analysis(candidates=[self.candidate()])
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", return_value=analysis):
                prepared = server.lightclaw_prepare("tail-decision", now=now, state_path=state_path)
            server.lightclaw_commit(prepared["commit_token"], now=now, state_path=state_path)
            with patch("server.run_analysis", return_value=analysis), patch("server.send_wecom_push") as sender:
                result = server.scheduled_push(
                    "tail-decision", self.WEBHOOK, now=now, state_path=state_path,
                )
            state = server.load_trading_state(state_path)
        self.assertFalse(result["sent"])
        sender.assert_not_called()
        self.assertEqual(state["processed_events"], [prepared["event_id"]])
        self.assertEqual(len(state["holdings"]), 1)

    def test_lightclaw_formal_pending_conflicts_with_test_prepare(self):
        self.assert_lightclaw_message_mode_conflict(first_test=False, second_test=True)

    def test_lightclaw_test_pending_conflicts_with_formal_prepare(self):
        self.assert_lightclaw_message_mode_conflict(first_test=True, second_test=False)

    def assert_lightclaw_message_mode_conflict(self, first_test, second_test):
        now = datetime(2026, 7, 29, 14, 30, tzinfo=server.SHANGHAI)
        analysis = self.analysis(candidates=[self.candidate()])
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", return_value=analysis) as analyzer:
                first = server.lightclaw_prepare(
                    "tail-decision", now=now, state_path=state_path, test_message=first_test,
                )
                conflict = server.lightclaw_prepare(
                    "tail-decision", now=now, state_path=state_path, test_message=second_test,
                )
            state = server.load_trading_state(state_path)
        self.assertEqual(conflict["status"], "pending_conflict")
        self.assertEqual(conflict["pending_test_message"], first_test)
        self.assertEqual(conflict["test_message"], second_test)
        self.assertEqual(state["pending_report"]["commit_token"], first["commit_token"])
        self.assertEqual(state["pending_report"]["test_message"], first_test)
        self.assertEqual(analyzer.call_count, 1)

    def test_lightclaw_cli_conflict_is_json_nonzero_and_does_not_read_webhook(self):
        conflict = {"status": "pending_conflict", "reason": "pending"}
        output = io.StringIO()
        environ = {"WECOM_WEBHOOK": "must-not-be-read", "RESOURCE_STATS_FEISHU_WEBHOOK": "must-not-be-read"}
        with patch.object(sys, "argv", ["server.py", "--lightclaw-prepare", "tail-decision"]), patch(
            "server.lightclaw_prepare", return_value=conflict,
        ), patch.dict("os.environ", environ, clear=True), patch("sys.stdout", output):
            exit_code = server.main()
        self.assertEqual(exit_code, 3)
        self.assertEqual(json.loads(output.getvalue()), conflict)

    def test_save_trading_state_fsyncs_temp_then_replaces_then_fsyncs_parent(self):
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            events = []
            original_replace = Path.replace

            def track_replace(source, target):
                events.append("replace")
                return original_replace(source, target)

            def track_fsync(descriptor):
                events.append("fsync_parent" if "replace" in events else "fsync_temp")

            with patch("server.os.fsync", side_effect=track_fsync), patch.object(
                Path, "replace", track_replace,
            ):
                server.save_trading_state(server.empty_trading_state(), state_path)

        self.assertEqual(events, ["fsync_temp", "replace", "fsync_parent"])

    def test_save_trading_state_parent_fsync_failure_is_reported_and_fd_closed(self):
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            closed = []
            fsync_calls = 0
            original_close = server.os.close

            def fail_parent_fsync(descriptor):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError("directory sync failed")

            def track_close(descriptor):
                closed.append(descriptor)
                return original_close(descriptor)

            with patch("server.os.fsync", side_effect=fail_parent_fsync), patch(
                "server.os.close", side_effect=track_close,
            ):
                with self.assertRaisesRegex(RuntimeError, "交易状态目录无法同步"):
                    server.save_trading_state(server.empty_trading_state(), state_path)

        self.assertEqual(fsync_calls, 2)
        self.assertEqual(len(closed), 1)

    def test_trading_state_files_are_private_without_changing_directory_mode(self):
        with TemporaryDirectory() as directory:
            data_dir = Path(directory)
            state_path = data_dir / "trading-state.json"
            temporary = state_path.with_suffix(state_path.suffix + ".tmp")
            lock_path = state_path.with_suffix(state_path.suffix + ".lock")
            state_path.write_text("{}", encoding="utf-8")
            temporary.write_text("old", encoding="utf-8")
            lock_path.write_text("", encoding="utf-8")
            for path in (state_path, temporary, lock_path):
                path.chmod(0o644)
            directory_mode = data_dir.stat().st_mode & 0o777
            temporary_modes = []
            original_replace = Path.replace

            def capture_temporary_mode(source, target):
                temporary_modes.append(source.stat().st_mode & 0o777)
                return original_replace(source, target)

            server.load_trading_state(state_path)
            with patch.object(Path, "replace", capture_temporary_mode):
                with server.trading_state_transaction(state_path):
                    server.save_trading_state(server.empty_trading_state(), state_path)
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(temporary_modes, [0o600])
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(data_dir.stat().st_mode & 0o777, directory_mode)

    def test_lightclaw_concurrent_prepare_has_single_pending_token(self):
        now = datetime(2026, 7, 29, 14, 30, tzinfo=server.SHANGHAI)
        analysis = self.analysis(candidates=[self.candidate()])
        results = []
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            barrier = threading.Barrier(3)

            def prepare():
                barrier.wait()
                results.append(server.lightclaw_prepare("tail-decision", now=now, state_path=state_path))

            with patch("server.run_analysis", return_value=analysis):
                threads = [threading.Thread(target=prepare) for _ in range(2)]
                for thread in threads:
                    thread.start()
                barrier.wait()
                for thread in threads:
                    thread.join()
            state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(results), 2)
        self.assertEqual({item["commit_token"] for item in results}, {state["pending_report"]["commit_token"]})
        self.assertEqual(sum(not item["reused"] for item in results), 1)


if __name__ == "__main__":
    unittest.main()
