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
                report = server.build_scheduled_report("tail-decision", analysis, state)
                state["pending_report"] = server._build_pending(
                    report, "c" * 32, server.format_lightclaw_message(report),
                    now.isoformat(timespec="seconds"), False, {}, "scheduled",
                )
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

    def test_scan_window_boundaries(self):
        expected = {
            (9, 29): False, (9, 30): False, (9, 35): True,
            (11, 30): True, (11, 31): False, (12, 59): False,
            (13, 0): True, (15, 0): True, (15, 1): False,
        }
        for (hour, minute), allowed in expected.items():
            with self.subTest(hour=hour, minute=minute):
                now = datetime(2026, 8, 25, hour, minute, tzinfo=server.SHANGHAI)
                self.assertEqual(server.scan_window_status(now)[0], allowed)
        weekend = datetime(2026, 8, 29, 10, 0, tzinfo=server.SHANGHAI)
        self.assertFalse(server.scan_window_status(weekend)[0])

    def scan_analysis(self, *, candidates=None, universe=None, trading_dates=None):
        value = self.analysis(
            trade_date="20260825", candidates=candidates, universe=universe,
            trading_dates=trading_dates or ["20260824", "20260825"],
        )
        value.update({
            "generated_at": "2026-08-25T10:00:00+08:00", "market_data_time": "1000",
            "strategy_version": server.STRATEGY_VERSION, "parameters": dict(server.DEFAULT_PARAMETERS),
            "universe_version": server.UNIVERSE_VERSION, "stock_pool": list(server.WATCHLIST),
            "stock_pool_hash": "fixture", "capital": 50_000,
        })
        for stock in value["candidates"]:
            stock.setdefault("last_time", "1000")
        return value

    def test_scan_same_buy_side_ignores_shares_price_and_rule_detail_changes(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        first_candidate = {**self.candidate(), "shares": 100, "price": 10.0, "last_time": "1000"}
        first_analysis = self.scan_analysis(
            candidates=[first_candidate], universe=[{"code": "000001", "price": 10.0, "last_time": "1000"}],
        )
        first_report, first_states, _ = server.build_intraday_scan_report(
            first_analysis, server.empty_trading_state(), now,
        )
        self.assertEqual([(item["side"], item["shares"]) for item in first_report["actions"]], [("buy", 100)])

        changed_candidate = {
            **first_candidate, "shares": 200, "price": 10.8, "entry_range": "10.70-10.90",
            "invalidation": 10.1, "resistance": 12.0,
        }
        changed_analysis = self.scan_analysis(
            candidates=[changed_candidate], universe=[{"code": "000001", "price": 10.8, "last_time": "1000"}],
        )
        state = {
            **server.empty_trading_state(), "signal_trade_date": "20260825", "signal_states": first_states,
        }
        with patch.dict(server.SCAN_REASON_CODES, {"buy": "strict-entry-detail-v2", "sell": "holding-exit-v1"}, clear=True):
            changed_report, changed_states, _ = server.build_intraday_scan_report(changed_analysis, state, now)
        self.assertEqual(changed_report["actions"], [])
        self.assertEqual(changed_report["event_id"], "")
        self.assertEqual(changed_states["000001"]["fingerprint"], first_states["000001"]["fingerprint"])

        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", side_effect=[first_analysis, changed_analysis]):
                prepared = server.lightclaw_scan_prepare(now, state_path)
                self.assertEqual(prepared["status"], "prepared")
                server.lightclaw_commit(prepared["commit_token"], now, state_path)
                self.assertEqual(server.lightclaw_scan_prepare(now, state_path)["status"], "no_signal")
            committed = server.load_trading_state(state_path)
        self.assertEqual(committed["holdings"][0]["shares"], 100)
        self.assertEqual(committed["holdings"][0]["buy_price"], 10.0)

    def test_scan_stale_symbol_preserves_buy_until_fresh_same_side(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        fingerprint = server._signal_fingerprint("20260825", "000001", "buy")
        state = {
            **server.empty_trading_state(), "signal_trade_date": "20260825",
            "signal_states": {"000001": {"side": "buy", "fingerprint": fingerprint}},
        }
        stale_candidate = {**self.candidate(), "shares": 200, "price": 10.8, "last_time": "0959"}
        stale_analysis = self.scan_analysis(
            candidates=[stale_candidate],
            universe=[
                {"code": "000001", "price": 10.8, "last_time": "0959"},
                {"code": "000002", "price": 8.0, "last_time": "1000"},
            ],
        )
        stale_report, stale_states, evaluated = server.build_intraday_scan_report(stale_analysis, state, now)
        self.assertEqual(stale_report["actions"], [])
        self.assertNotIn("000001", evaluated)
        self.assertEqual(stale_states["000001"], state["signal_states"]["000001"])

        fresh_candidate = {**stale_candidate, "last_time": "1000"}
        fresh_analysis = self.scan_analysis(
            candidates=[fresh_candidate], universe=[{"code": "000001", "price": 10.8, "last_time": "1000"}],
        )
        fresh_report, fresh_states, evaluated = server.build_intraday_scan_report(fresh_analysis, state, now)
        self.assertEqual(fresh_report["actions"], [])
        self.assertIn("000001", evaluated)
        self.assertEqual(fresh_states["000001"]["side"], "buy")

    def test_scan_fresh_none_clears_buy_and_later_buy_notifies_again(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        fingerprint = server._signal_fingerprint("20260825", "000001", "buy")
        state = {
            **server.empty_trading_state(), "signal_trade_date": "20260825",
            "signal_states": {"000001": {"side": "buy", "fingerprint": fingerprint}},
        }
        no_buy = self.scan_analysis(universe=[{"code": "000001", "price": 10.0, "last_time": "1000"}])
        no_buy_report, none_states, evaluated = server.build_intraday_scan_report(no_buy, state, now)
        self.assertEqual(no_buy_report["actions"], [])
        self.assertIn("000001", evaluated)
        self.assertEqual(none_states["000001"]["side"], "none")

        candidate = {**self.candidate(), "last_time": "1000"}
        buy = self.scan_analysis(
            candidates=[candidate], universe=[{"code": "000001", "price": 10.0, "last_time": "1000"}],
        )
        rebuy_state = {**state, "signal_states": none_states}
        rebuy_report, _, _ = server.build_intraday_scan_report(buy, rebuy_state, now)
        self.assertEqual([(item["side"], item["code"]) for item in rebuy_report["actions"]], [("buy", "000001")])

    def test_scan_sell_notification_identity_tracks_holding_id(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        quote = {"code": "000001", "price": 9.0, "vwap": 10, "change_pct": -2,
                 "tail_return_pct": -1, "trend": "空头排列", "last_time": "1000"}
        old_holding = self.holding(buy_date="20260822")
        old_fingerprint = server._signal_fingerprint(
            "20260825", "000001", "sell", old_holding["id"],
        )
        previous = {
            **server.empty_trading_state(), "holdings": [old_holding], "signal_trade_date": "20260825",
            "signal_states": {"000001": {"side": "sell", "fingerprint": old_fingerprint}},
        }
        same_report, _, _ = server.build_intraday_scan_report(
            self.scan_analysis(universe=[quote], trading_dates=["20260822", "20260825"]), previous, now,
        )
        self.assertEqual(same_report["actions"], [])

        new_holding = self.holding(buy_date="20260821")
        replacement = {**previous, "holdings": [new_holding]}
        new_report, _, _ = server.build_intraday_scan_report(
            self.scan_analysis(universe=[quote], trading_dates=["20260821", "20260825"]), replacement, now,
        )
        self.assertEqual([(item["side"], item["holding_id"]) for item in new_report["actions"]],
                         [("sell", new_holding["id"])])

    def test_scan_none_buy_persist_none_and_rebuy(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        candidate = self.candidate()
        candidate["last_time"] = "1000"
        no_buy = self.scan_analysis(universe=[{"code": "000001", "price": 10, "last_time": "1000"}])
        buy = self.scan_analysis(candidates=[candidate], universe=[{"code": "000001", "price": 10, "last_time": "1000"}])
        later_candidate = {**candidate, "last_time": "1005"}
        buy_later = self.scan_analysis(candidates=[later_candidate], universe=[{"code": "000001", "price": 10, "last_time": "1005"}])
        buy_later["market_data_time"] = "1005"
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", side_effect=[no_buy, buy, buy, no_buy, buy_later]):
                self.assertEqual(server.lightclaw_scan_prepare(now, state_path)["status"], "no_signal")
                first = server.lightclaw_scan_prepare(now, state_path)
                self.assertEqual(first["status"], "prepared")
                server.lightclaw_commit(first["commit_token"], now, state_path)
                cleared = server.load_trading_state(state_path)
                cleared["holdings"] = []
                server.save_trading_state(cleared, state_path)
                self.assertEqual(server.lightclaw_scan_prepare(now, state_path)["status"], "no_signal")
                self.assertEqual(server.lightclaw_scan_prepare(now, state_path)["status"], "no_signal")
                second = server.lightclaw_scan_prepare(now + timedelta(minutes=5), state_path)
        self.assertEqual(second["status"], "prepared")
        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_scan_same_minute_buy_none_buy_has_distinct_stable_archives(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        candidate = {**self.candidate(), "last_time": "1000"}
        buy = self.scan_analysis(
            candidates=[candidate], universe=[{"code": "000001", "price": 10, "last_time": "1000"}],
        )
        none = self.scan_analysis(universe=[{"code": "000001", "price": 10, "last_time": "1000"}])
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", side_effect=[buy, none, buy]):
                first = server.lightclaw_scan_prepare(now, state_path)
                retry = server.lightclaw_scan_prepare(now, state_path)
                self.assertEqual(retry["event_id"], first["event_id"])
                self.assertEqual(retry["commit_token"], first["commit_token"])
                server.lightclaw_commit(first["commit_token"], now, state_path)
                state = server.load_trading_state(state_path)
                state["holdings"] = []
                server.save_trading_state(state, state_path)
                self.assertEqual(server.lightclaw_scan_prepare(now, state_path)["status"], "no_signal")
                second = server.lightclaw_scan_prepare(now, state_path)
                second_retry = server.lightclaw_scan_prepare(now, state_path)
                self.assertEqual(second_retry["event_id"], second["event_id"])
                self.assertEqual(second_retry["commit_token"], second["commit_token"])
                server.lightclaw_commit(second["commit_token"], now, state_path)
            archives = sorted((state_path.parent / "formal-events").glob("*.json"))
            final_state = server.load_trading_state(state_path)
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertEqual(len(archives), 2)
        self.assertEqual(final_state["signal_states"]["000001"]["generation"], 2)

    def test_scan_sell_priority_t_plus_one_and_missing_holding_quote_blocks_buy(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        buy_candidate = self.candidate("000002")
        buy_candidate["last_time"] = "1000"
        t0 = self.holding(buy_date="20260825")
        old = self.holding(buy_date="20260822")
        sell_stock = {"code": "000001", "price": 9.0, "vwap": 10, "change_pct": -2,
                      "tail_return_pct": -1, "trend": "空头排列", "last_time": "1000"}
        analysis = self.scan_analysis(candidates=[buy_candidate], universe=[sell_stock, {"code": "000002", "last_time": "1000"}], trading_dates=["20260822", "20260825"])
        t0_report, _, _ = server.build_intraday_scan_report(analysis, {**server.empty_trading_state(), "holdings": [t0]}, now)
        self.assertEqual(t0_report["actions"], [])
        sell_report, _, _ = server.build_intraday_scan_report(analysis, {**server.empty_trading_state(), "holdings": [old]}, now)
        self.assertEqual([item["side"] for item in sell_report["actions"]], ["sell"])
        missing_analysis = self.scan_analysis(candidates=[buy_candidate], universe=[{"code": "000002", "last_time": "1000"}])
        missing_report, states, _ = server.build_intraday_scan_report(
            missing_analysis,
            {**server.empty_trading_state(), "holdings": [old], "signal_trade_date": "20260825",
             "signal_states": {"000001": {"side": "sell", "fingerprint": "keep"}}}, now,
        )
        self.assertEqual(missing_report["actions"], [])
        self.assertEqual(states["000001"]["fingerprint"], "keep")

    def test_scan_multiple_sells_and_buys_are_stably_sorted_and_batched(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        holdings = [self.holding("000002", "20260822"), self.holding("000001", "20260822")]
        sell_universe = [
            {"code": code, "price": 9.0, "vwap": 10, "change_pct": -2,
             "tail_return_pct": -1, "trend": "空头排列", "last_time": "1000"}
            for code in ("000002", "000001")
        ]
        sell_report, _, _ = server.build_intraday_scan_report(
            self.scan_analysis(universe=sell_universe, trading_dates=["20260822", "20260825"]),
            {**server.empty_trading_state(), "holdings": holdings}, now,
        )
        self.assertEqual([(item["side"], item["code"]) for item in sell_report["actions"]],
                         [("sell", "000001"), ("sell", "000002")])
        buys = [self.candidate("000002"), self.candidate("000001")]
        for item in buys:
            item["last_time"] = "1000"
        buy_report, _, _ = server.build_intraday_scan_report(
            self.scan_analysis(candidates=buys, universe=[{"code": "000002"}, {"code": "000001"}]),
            server.empty_trading_state(), now,
        )
        self.assertEqual([(item["side"], item["code"]) for item in buy_report["actions"]],
                         [("buy", "000001"), ("buy", "000002")])
        self.assertEqual(buy_report["trade_actions"], 2)
        self.assertTrue(buy_report["event_id"].startswith("20260825:scan:1000:"))

    def test_scan_same_stock_cannot_be_rebought_after_same_day_round_trip(self):
        now = datetime(2026, 8, 25, 13, 0, tzinfo=server.SHANGHAI)
        candidate = self.candidate()
        candidate["last_time"] = "1300"
        state = server.empty_trading_state()
        state["history"] = [{**self.holding(buy_date="20260825"), "status": "closed"}]
        report, _, _ = server.build_intraday_scan_report(
            self.scan_analysis(candidates=[candidate], universe=[{"code": "000001", "last_time": "1300"}]),
            state, now,
        )
        self.assertEqual(report["actions"], [])

    def test_scan_pending_reuse_conflict_and_no_webhook_access(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        candidate = self.candidate()
        candidate["last_time"] = "1000"
        analysis = self.scan_analysis(candidates=[candidate], universe=[{"code": "000001", "last_time": "1000"}])
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch.dict("os.environ", {"WECOM_WEBHOOK": "must-not-read", "RESOURCE_STATS_FEISHU_WEBHOOK": "must-not-read"}, clear=True), \
                 patch("server.run_analysis", return_value=analysis) as analyzer, \
                 patch("server.send_wecom_push") as sender, patch("urllib.request.urlopen") as network:
                first = server.lightclaw_scan_prepare(now, state_path)
                reused = server.lightclaw_scan_prepare(now, state_path)
                conflict = server.lightclaw_scan_prepare(now + timedelta(minutes=5), state_path)
            mode = state_path.stat().st_mode & 0o777
        self.assertEqual(first["status"], "prepared")
        self.assertEqual(reused["status"], "already_pending")
        self.assertEqual(reused["commit_token"], first["commit_token"])
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(analyzer.call_count, 1)
        self.assertEqual(mode, 0o600)
        sender.assert_not_called()
        network.assert_not_called()

    def test_scan_pending_corruption_fails_stop_without_analysis_or_overwrite(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        candidate = {**self.candidate(), "last_time": "1000"}
        analysis = self.scan_analysis(candidates=[candidate], universe=[{"code": "000001", "last_time": "1000"}])
        for mutation in ("bogus", "hash_mismatch", "forged_fingerprint"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                state_path = Path(directory) / "state.json"
                with patch("server.run_analysis", return_value=analysis):
                    server.lightclaw_scan_prepare(now, state_path)
                state = server.load_trading_state(state_path)
                if mutation == "bogus":
                    state["pending_report"] = {"event_id": "20260825:scan:1000:bogus", "commit_token": ""}
                elif mutation == "hash_mismatch":
                    state["pending_report"]["report"]["actions"][0]["shares"] += 100
                else:
                    pending = state["pending_report"]
                    pending["report"]["actions"][0]["shares"] += 100
                    pending["integrity_hash"] = server._pending_integrity_hash(pending)
                server.save_trading_state(state, state_path)
                before = state_path.read_bytes()
                with patch("server.run_analysis") as analyzer:
                    with self.assertRaises(server.CorruptLocalData):
                        server.lightclaw_scan_prepare(now, state_path)
                self.assertEqual(state_path.read_bytes(), before)
                analyzer.assert_not_called()

    def test_pending_non_none_invalid_values_fail_stop_all_entries(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        for invalid in ({}, [], "x"):
            with self.subTest(invalid=invalid), TemporaryDirectory() as directory:
                state_path = Path(directory) / "state.json"
                state = server.empty_trading_state()
                state["pending_report"] = invalid
                server.save_trading_state(state, state_path)
                before = state_path.read_bytes()
                with patch("server.run_analysis") as analyzer, patch("server.send_wecom_push") as sender:
                    for entry in (
                        lambda: server.lightclaw_scan_prepare(now, state_path),
                        lambda: server.lightclaw_prepare("morning-summary", now, state_path),
                        lambda: server.lightclaw_commit("c" * 32, now, state_path),
                        lambda: server.scheduled_push("morning-summary", self.WEBHOOK, now, state_path),
                    ):
                        with self.assertRaises(server.CorruptLocalData):
                            entry()
                self.assertEqual(state_path.read_bytes(), before)
                analyzer.assert_not_called()
                sender.assert_not_called()

    def test_corrupt_empty_pending_cli_is_single_json_exit_two(self):
        output = io.StringIO()
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state = server.empty_trading_state()
            state["pending_report"] = {}
            server.save_trading_state(state, state_path)
            with patch.object(sys, "argv", ["server.py", "--lightclaw-scan-prepare"]), patch(
                "server.TRADING_STATE_PATH", state_path,
            ), patch("server.run_analysis") as analyzer, patch("sys.stdout", output):
                self.assertEqual(server.main(), 2)
            analyzer.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "corrupt_data")
        self.assertEqual(output.getvalue().count("\n"), 1)

    def test_scheduled_push_corrupt_pending_cli_fails_stop_with_single_json_exit_two(self):
        for invalid in ({}, [], "x"):
            with self.subTest(invalid=invalid), TemporaryDirectory() as directory:
                state_path = Path(directory) / "state.json"
                state = server.empty_trading_state()
                state["pending_report"] = invalid
                server.save_trading_state(state, state_path)
                before = state_path.read_bytes()
                output = io.StringIO()
                with patch.object(sys, "argv", ["server.py", "--scheduled-push", "morning-summary"]), patch(
                    "server.TRADING_STATE_PATH", state_path,
                ), patch.dict("os.environ", {"WECOM_WEBHOOK": self.WEBHOOK}, clear=True), patch(
                    "server.run_analysis",
                ) as analyzer, patch("server.send_wecom_push") as sender, patch(
                    "server.save_trading_state",
                ) as state_writer, patch("urllib.request.urlopen") as network, patch("sys.stdout", output):
                    self.assertEqual(server.main(), 2)
                self.assertEqual(state_path.read_bytes(), before)
                self.assertEqual(output.getvalue().count("\n"), 1)
                self.assertEqual(json.loads(output.getvalue())["sent"], False)
                analyzer.assert_not_called()
                sender.assert_not_called()
                state_writer.assert_not_called()
                network.assert_not_called()

    def test_pending_rejects_rehashed_forged_message_on_prepare_and_commit(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        candidate = {**self.candidate(), "last_time": "1000"}
        analysis = self.scan_analysis(candidates=[candidate], universe=[{"code": "000001", "last_time": "1000"}])
        for entry_name in ("prepare", "commit"):
            with self.subTest(entry=entry_name), TemporaryDirectory() as directory:
                state_path = Path(directory) / "state.json"
                with patch("server.run_analysis", return_value=analysis):
                    prepared = server.lightclaw_scan_prepare(now, state_path)
                state = server.load_trading_state(state_path)
                state["pending_report"]["message"] = "FORGED MESSAGE"
                state["pending_report"]["integrity_hash"] = server._pending_integrity_hash(state["pending_report"])
                server.save_trading_state(state, state_path)
                before = state_path.read_bytes()
                with patch("server.run_analysis") as analyzer:
                    with self.assertRaises(server.CorruptLocalData):
                        if entry_name == "prepare":
                            server.lightclaw_scan_prepare(now, state_path)
                        else:
                            server.lightclaw_commit(prepared["commit_token"], now, state_path)
                self.assertEqual(state_path.read_bytes(), before)
                self.assertFalse(server.archive_path_for(state_path, prepared["event_id"]).exists())
                analyzer.assert_not_called()

    def test_pending_rejects_rehashed_forged_execution_rows_on_prepare_and_commit(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        candidate = {**self.candidate(), "last_time": "1000"}
        analysis = self.scan_analysis(candidates=[candidate], universe=[{"code": "000001", "last_time": "1000"}])
        source = {"sz000001": {"records": [{"time": "1001", "price": 10.02, "volume": 2000, "suspended": False, "at_limit": False}]}}
        for entry_name in ("prepare", "commit"):
            with self.subTest(entry=entry_name), TemporaryDirectory() as directory:
                state_path = Path(directory) / "state.json"

                def analyze(**kwargs):
                    kwargs["persistence_data"]["execution_rows"] = source
                    return analysis

                with patch("server.run_analysis", side_effect=analyze):
                    prepared = server.lightclaw_scan_prepare(now, state_path)
                state = server.load_trading_state(state_path)
                state["pending_report"]["execution_rows"]["sz000001"]["records"][0].update(
                    {"price": 99.99, "volume": 999999},
                )
                state["pending_report"]["integrity_hash"] = server._pending_integrity_hash(state["pending_report"])
                server.save_trading_state(state, state_path)
                before = state_path.read_bytes()
                with patch("server.run_analysis") as analyzer:
                    with self.assertRaises(server.CorruptLocalData):
                        if entry_name == "prepare":
                            server.lightclaw_scan_prepare(now, state_path)
                        else:
                            server.lightclaw_commit(prepared["commit_token"], now, state_path)
                self.assertEqual(state_path.read_bytes(), before)
                self.assertFalse(server.archive_path_for(state_path, prepared["event_id"]).exists())
                analyzer.assert_not_called()

    def test_scan_corrupt_pending_cli_is_single_json_exit_two(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["server.py", "--lightclaw-scan-prepare"]), patch(
            "server.lightclaw_scan_prepare", side_effect=server.CorruptLocalData("pending integrity hash 无效"),
        ), patch("sys.stdout", output):
            self.assertEqual(server.main(), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "corrupt_data")
        self.assertEqual(output.getvalue().count("\n"), 1)

    def test_scan_stale_and_incomplete_market_are_noop_without_signal_reset(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        stale = self.scan_analysis()
        stale["trade_date"] = "20260822"
        early = self.scan_analysis()
        early["market_data_time"] = "0959"
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            initial = server.empty_trading_state()
            initial.update({"signal_trade_date": "20260822", "signal_states": {"000001": {"side": "buy", "fingerprint": "old"}}})
            server.save_trading_state(initial, state_path)
            before = state_path.read_bytes()
            with patch("server.run_analysis", side_effect=[stale, early]):
                self.assertEqual(server.lightclaw_scan_prepare(now, state_path)["status"], "no_signal")
                self.assertEqual(server.lightclaw_scan_prepare(now, state_path)["status"], "no_signal")
            self.assertEqual(state_path.read_bytes(), before)

    def test_scan_pending_archives_strict_next_minute_execution_evidence(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        candidate = {**self.candidate(), "last_time": "1000"}
        analysis = self.scan_analysis(candidates=[candidate], universe=[{"code": "000001", "last_time": "1000"}])
        rows = {"sz000001": {"records": [{"time": "1001", "price": 10.02, "volume": 2000, "suspended": False, "at_limit": False}]}}
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"

            def analyze(**kwargs):
                kwargs["persistence_data"]["execution_rows"] = rows
                return analysis

            with patch("server.run_analysis", side_effect=analyze):
                prepared = server.lightclaw_scan_prepare(now, state_path)
            pending = server.load_trading_state(state_path)["pending_report"]
            server.lightclaw_commit(prepared["commit_token"], now, state_path)
            archive = json.loads(server.archive_path_for(state_path, prepared["event_id"]).read_text(encoding="utf-8"))
        self.assertEqual(pending["execution_rows"], rows)
        self.assertEqual(archive["actions"][0]["expected_execution_minute"], "1001")
        self.assertEqual(archive["actions"][0]["raw_next_minute"]["time"], "1001")

    def test_scan_missing_strict_next_minute_commits_as_unfilled(self):
        now = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)
        candidate = {**self.candidate(), "last_time": "1000"}
        analysis = self.scan_analysis(candidates=[candidate], universe=[{"code": "000001", "last_time": "1000"}])
        later_only = {"sz000001": {"records": [{"time": "1002", "price": 10.03, "volume": 3000, "suspended": False, "at_limit": False}]}}
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"

            def analyze(**kwargs):
                kwargs["persistence_data"]["execution_rows"] = later_only
                return analysis

            with patch("server.run_analysis", side_effect=analyze):
                prepared = server.lightclaw_scan_prepare(now, state_path)
            pending = server.load_trading_state(state_path)["pending_report"]
            reused = server.lightclaw_scan_prepare(now, state_path)
            server.lightclaw_commit(prepared["commit_token"], now, state_path)
            archive = json.loads(server.archive_path_for(state_path, prepared["event_id"]).read_text(encoding="utf-8"))
        self.assertEqual(reused["status"], "already_pending")
        self.assertEqual(reused["commit_token"], prepared["commit_token"])
        self.assertEqual(pending["execution_rows"], {"sz000001": {"records": []}})
        self.assertEqual(archive["actions"][0]["expected_execution_minute"], "1001")
        self.assertIsNone(archive["actions"][0]["raw_next_minute"])

    def test_scan_commit_uses_real_time_and_is_idempotent(self):
        now = datetime(2026, 8, 25, 13, 5, tzinfo=server.SHANGHAI)
        candidate = self.candidate()
        candidate["last_time"] = "1305"
        analysis = self.scan_analysis(candidates=[candidate], universe=[{"code": "000001", "last_time": "1305"}])
        analysis["market_data_time"] = "1305"
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch("server.run_analysis", return_value=analysis):
                prepared = server.lightclaw_scan_prepare(now, state_path)
            first = server.lightclaw_commit(prepared["commit_token"], now, state_path)
            second = server.lightclaw_commit(prepared["commit_token"], now, state_path)
            state = server.load_trading_state(state_path)
        self.assertEqual(first["status"], "committed")
        self.assertEqual(second["status"], "already_committed")
        self.assertEqual(state["holdings"][0]["buy_time"], "1305")
        self.assertIn("仅供研究参考，不构成个性化投资建议", prepared["message"])
        self.assertIn("本地假定持仓", prepared["message"])

    def test_scan_cli_stdout_is_single_json_and_conflict_nonzero(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["server.py", "--lightclaw-scan-prepare"]), \
             patch("server.lightclaw_scan_prepare", return_value={"status": "conflict"}), \
             patch.dict("os.environ", {"WECOM_WEBHOOK": "sentinel"}, clear=True), patch("sys.stdout", output):
            self.assertEqual(server.main(), 3)
        self.assertEqual(json.loads(output.getvalue()), {"status": "conflict"})
        self.assertEqual(output.getvalue().count("\n"), 1)

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
