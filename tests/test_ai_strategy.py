import copy
import io
import json
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import ai_strategy
import server
import signal_review
import strategy_rules
import trade_review
from tests.test_delivery import NOW
from tests import test_server as fixtures


def answer(codes=("000001", "000002")):
    return {"buys": [{"code": code, "approve": True, "reason": "趋势与风险可接受"} for code in codes], "sells": []}


def analysis_with_ai(response):
    """Run the real analysis/AI pipeline using deterministic offline market fixtures."""
    stocks = {
        code: {
            "code": code, "name": code, "price": 10, "score": 90, "failures": [], "warnings": [],
            "invalidation": 9, "resistance": 13, "last_time": "1000", "change_pct": 1,
        } for code in ("000001", "000002")
    }
    with ExitStack() as stack:
        mocks = {
            "effective_watchlist": list(stocks),
            "load_trading_state": server.empty_trading_state(),
            "fetch_minute": {"trade_date": "20260825"},
            "fetch_history": {"raw_rows": []},
            "parse_minute": {"last_time": "1000", "price": 10, "change_pct": 1, "tail_return_pct": 1},
            "synthesize_bars": [{"date": "2026-08-25", "close": 10}],
            "market_state": {"state": "green", "label": "绿灯", "summary": "核验通过"},
            "fetch_announcements": {"verified": True, "risk_titles": [], "recent": []},
        }
        for name, value in mocks.items():
            stack.enter_context(patch(f"server.{name}", return_value=value))
        stack.enter_context(patch("server.analyze_security", side_effect=lambda code, *args: copy.deepcopy(stocks[code])))
        stack.enter_context(patch("llm.chat", return_value=json.dumps(response) if not isinstance(response, str) else response))
        return server.run_analysis(now=NOW, cutoff_minutes=600, mode="intraday-scan", persist=False)


class AIDecisionTests(unittest.TestCase):
    def test_ai_ranks_and_vetoes_before_position_sizing(self):
        response = answer(("000002", "000001"))
        ranked = analysis_with_ai(response)
        self.assertEqual([item["code"] for item in ranked["candidates"]], ["000002", "000001"])
        response["buys"][1]["approve"] = False
        filtered = analysis_with_ai(response)
        self.assertEqual([item["code"] for item in filtered["candidates"]], ["000002"])
        self.assertEqual(filtered["position_pct"], 60)
        self.assertEqual(filtered["candidates"][0]["shares"], 3000)
        self.assertEqual(filtered["ai_decision"]["status"], "ok")

    def test_unknown_omitted_duplicate_and_invalid_votes_fail_closed(self):
        bad = [
            answer(("999999", "000001")), answer(("000001",)), answer(("000001", "000001")),
            {"buys": [{"code": "000001", "approve": "yes", "reason": "bad"}], "sells": []},
        ]
        for response in bad:
            with self.subTest(response=response), patch("sys.stderr", new_callable=io.StringIO):
                result = analysis_with_ai(response)
                self.assertEqual(result["ai_decision"]["status"], "unavailable")
                self.assertEqual(result["candidates"], [])

    def test_missing_key_and_invalid_json_do_not_fall_back_to_rule_buy(self):
        for text in ("", "```json\n{}\n```", '{"buys":[],"buys":[],"sells":[]}'):
            with patch("sys.stderr", new_callable=io.StringIO):
                result = analysis_with_ai(text)
            self.assertEqual(result["candidates"], [])
            self.assertIn("AI决策不可用", result["summary"])

    def test_no_candidates_does_not_call_model(self):
        with patch("llm.chat") as chat:
            self.assertEqual(ai_strategy.decide([], [], {}, {}, "time")["status"], "not_needed")
        chat.assert_not_called()

    def test_ai_cannot_override_hard_exit_or_t_plus_one(self):
        holding = fixtures.DashboardLogicTests.holding(buy_date="20260824")
        state = {"holdings": [holding]}
        stock = {"code": "000001", "price": 10, "vwap": 11, "tail_return_pct": -1}
        analysis = fixtures.DashboardLogicTests.analysis(
            trade_date="20260825", trading_dates=["20260824", "20260825"], universe=[stock],
        )
        analysis["parameters"] = dict(strategy_rules.BASELINE)
        analysis["ai_decision"] = {
            "status": "ok", "buys": [],
            "sells": [{"holding_id": holding["id"], "approve": False, "reason": "不确认技术退出"}],
        }
        holding["invalidation"] = 8
        holding["target_price"] = 15
        self.assertEqual(server.review_holdings(state, analysis)[0]["decision"], "hold")
        analysis["ai_decision"]["sells"][0]["approve"] = True
        self.assertEqual(server.review_holdings(state, analysis)[0]["decision"], "sell")
        analysis["ai_decision"]["status"] = "unavailable"
        analysis["ai_decision"]["sells"] = []
        for changes in ({"invalidation": 10}, {"target_price": 10}):
            with self.subTest(changes=changes):
                original = copy.deepcopy(holding)
                holding.update(changes)
                self.assertEqual(server.review_holdings(state, analysis)[0]["decision"], "sell")
                holding.update(original)
        analysis["parameters"]["max_holding_sessions"] = 1
        self.assertEqual(server.review_holdings(state, analysis)[0]["decision"], "sell")
        holding["buy_date"] = "20260825"
        self.assertEqual(server.review_holdings(state, analysis)[0]["decision"], "hold")

    def test_pending_recompute_never_calls_model_and_freezes_ai_vote(self):
        analysis = analysis_with_ai(answer())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with patch("server.run_analysis", return_value=analysis):
                first = server.lightclaw_scan_prepare(NOW, path)
            state = server.load_trading_state(path)
            self.assertEqual(len(state["signal_observations"]), 2)
            with patch("llm.chat") as chat:
                server._validate_pending(state["pending_report"], state)
                resumed = server.lightclaw_scan_prepare(NOW.replace(hour=16), path, resume_pending=True)
            chat.assert_not_called()
            self.assertEqual(resumed["commit_token"], first["commit_token"])
            self.assertIn("AI确认", first["actions"][0]["reasons"][-1])

    def test_vetoed_signal_is_recorded_even_without_notification(self):
        response = answer()
        for row in response["buys"]:
            row["approve"] = False
        analysis = analysis_with_ai(response)
        with TemporaryDirectory() as directory, patch("server.run_analysis", return_value=analysis):
            path = Path(directory) / "state.json"
            self.assertEqual(server.lightclaw_scan_prepare(NOW, path)["status"], "no_signal")
            state = server.load_trading_state(path)
            self.assertEqual(len(state["signal_observations"]), 2)
            self.assertTrue(all(item["status"] == "vetoed" for item in state["signal_observations"].values()))


class SignalPerformanceTests(unittest.TestCase):
    def state(self):
        state = server.empty_trading_state()
        state["signal_observations"] = {"test": {
            "id": "test", "code": "000001", "side": "buy", "trade_date": "20260825",
            "price": 10, "status": "approved", "reason": "confirmed", "notification_ids": ["sig"],
        }}
        return state

    def test_horizons_exclude_same_day_and_unknown_is_not_loss(self):
        state = self.state()
        bars = [{"date": day, "close": price} for day, price in (
            ("2026-08-25", 100), ("2026-08-26", 11), ("2026-08-27", 9), ("2026-08-28", 12),
        )]
        result = signal_review.evaluate(state, date(2026, 8, 28), date(2026, 8, 28), {"000001": bars})
        sample = result["samples"][0]
        self.assertEqual(sample["horizons"]["1"]["return_pct"], 10)
        self.assertFalse(sample["horizons"]["2"]["correct"])
        self.assertEqual(result["metrics"]["buy_approved"]["directional_hit_rate"], 1)
        early = signal_review.evaluate(state, date(2026, 8, 25), date(2026, 8, 25), {"000001": bars})
        self.assertEqual(early["pending_count"], 1)
        self.assertIsNone(early["metrics"]["buy_approved"]["directional_hit_rate"])

    def test_missing_exchange_session_is_not_replaced_by_later_stock_bar(self):
        state = self.state()
        bars = [{"date": day, "close": 12} for day in ("2026-08-26", "2026-08-28", "2026-08-31")]
        calendar = ["2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31"]
        result = signal_review.evaluate(state, date(2026, 8, 25), date(2026, 8, 31),
                                        {"000001": bars}, calendar)
        self.assertNotIn("2", result["samples"][0]["horizons"])
        self.assertEqual(result["samples"][0]["horizons"]["3"]["date"], "2026-08-28")
        self.assertTrue(result["missing_data"])

    def test_sell_veto_and_linked_execution_are_separate_from_buy_accuracy(self):
        state = self.state()
        observation = state["signal_observations"]["test"]
        observation.update({"side": "sell", "status": "vetoed"})
        state["actual_trade_receipts"]["signal:sig"] = {}
        bars = [{"date": day, "close": 9} for day in ("2026-08-26", "2026-08-27", "2026-08-28")]
        result = signal_review.evaluate(state, date(2026, 8, 25), date(2026, 8, 28), {"000001": bars})
        self.assertEqual(result["metrics"]["sell_vetoed"]["directional_hit_rate"], 1)
        self.assertEqual(result["metrics"]["buy_approved"]["count"], 0)
        self.assertEqual(result["samples"][0]["execution"], "linked_fill")

    def test_observations_deduplicate_without_overwriting_first_price(self):
        analysis = analysis_with_ai(answer())
        state = server.empty_trading_state()
        signal_review.observe(state, analysis, [])
        analysis["universe"][0]["price"] = 30
        signal_review.observe(state, analysis, [])
        self.assertEqual(len(state["signal_observations"]), 2)
        self.assertTrue(all(row["price"] == 10 for row in state["signal_observations"].values()))

    def test_adjusted_returns_use_signal_day_actual_close_not_original_price_basis(self):
        state = self.state()
        state["signal_observations"]["test"]["signal_day_close"] = 10
        # A 2:1 split is not a 50% loss; subsequent adjusted close 6 is a 20% gain.
        bars = [{"date": day, "close": price} for day, price in (
            ("2026-08-25", 5), ("2026-08-26", 5.2), ("2026-08-27", 5.5), ("2026-08-28", 6),
        )]
        result = signal_review.evaluate(state, date(2026, 8, 25), date(2026, 8, 28),
                                        {"000001": bars}, [bar["date"] for bar in bars], adjusted=True)
        self.assertEqual(result["samples"][0]["horizons"]["3"]["return_pct"], 20)
        del state["signal_observations"]["test"]["signal_day_close"]
        missing = signal_review.evaluate(state, date(2026, 8, 25), date(2026, 8, 28),
                                         {"000001": bars}, [bar["date"] for bar in bars], adjusted=True)
        self.assertEqual(missing["matured_count"], 0)
        self.assertTrue(missing["missing_data"])

    def test_signal_day_close_is_captured_without_mutating_ledger(self):
        state = self.state()
        original = copy.deepcopy(state)
        history = {"raw_rows": [["2026-08-25", 10, 11, 12, 9, 100]], "adjustment": "qfq"}
        with patch("server.fetch_history", return_value=history), \
             patch("server.fetch_minute", return_value={"trade_date": "20260825"}), \
             patch("server.parse_minute", return_value={"last_time": "1500", "price": 11}):
            performance = trade_review.signal_performance(state, NOW.date(), NOW.date())
        self.assertEqual(performance["signal_closes"], {"test": 11})
        self.assertEqual(state, original)


class AIReviewTests(unittest.TestCase):
    def report(self, count=10):
        state = server.empty_trading_state()
        end = NOW.date()
        report = trade_review.build_review(state, "daily", end, {})
        report["signal_review"] = {
            "samples": [{"code": f"{code:06d}", "trade_date": "20260820", "side": "buy",
                         "matured": True, "status": "approved"} for code in range(count)],
            "missing_data": [], "metrics": {},
        }
        return state, report

    def result(self, **parameters):
        return {"analysis": "成熟买入信号误报偏多，收紧筛选。", "reason": "依据10个成熟独立样本。",
                "after": {**strategy_rules.BASELINE, **parameters}}

    def test_ai_adjustment_is_bounded_next_day_and_daily_weekly_once(self):
        state, report = self.report()
        with patch("ai_strategy.structured", return_value=self.result(minimum_reward_risk=2.2)):
            plan = strategy_rules.plan_ai_update(state, report)
        first = strategy_rules.apply_ai_update(state, plan, NOW.replace(hour=20))
        self.assertEqual(first["status"], "applied")
        self.assertEqual(strategy_rules.effective_parameters(state, "20260825")["minimum_reward_risk"], 2)
        self.assertEqual(strategy_rules.effective_parameters(state, "20260826")["minimum_reward_risk"], 2.2)
        again = strategy_rules.apply_ai_update(state, plan, NOW.replace(hour=20))
        self.assertEqual(again["status"], "already_applied")
        self.assertEqual(len(state["strategy_adjustments"]), 1)

    def test_insufficient_samples_and_missing_data_prevent_changes(self):
        for count, missing in ((9, []), (10, ["missing"])):
            state, report = self.report(count)
            report["signal_review"]["missing_data"] = missing
            with patch("ai_strategy.structured", return_value=self.result(minimum_reward_risk=2.2)):
                planned = strategy_rules.plan_ai_update(state, report)
            self.assertEqual(planned["after"], strategy_rules.BASELINE)
            self.assertEqual(planned["ai_status"], "ok")

    def test_invalid_ai_parameters_and_failure_do_not_use_pnl_fallback(self):
        for after in (
            {"minimum_reward_risk": 2.8, "max_holding_sessions": 3},
            {**strategy_rules.BASELINE, "capital": 999999},
            {"minimum_reward_risk": 2, "max_holding_sessions": 0},
        ):
            state, report = self.report()
            with patch("ai_strategy.structured", return_value={**self.result(), "after": after}), \
                 patch("sys.stderr", new_callable=io.StringIO):
                planned = strategy_rules.plan_ai_update(state, report)
            self.assertEqual(planned["ai_status"], "unavailable")
            self.assertEqual(planned["after"], strategy_rules.BASELINE)
            self.assertEqual(strategy_rules.apply_ai_update(state, planned, NOW.replace(hour=20))["status"], "unavailable")
            self.assertEqual(state["strategy_adjustments"], [])

    def test_changed_ledger_after_applied_review_is_rejected(self):
        state, report = self.report()
        with patch("ai_strategy.structured", return_value=self.result(minimum_reward_risk=2.2)):
            planned = strategy_rules.plan_ai_update(state, report)
        strategy_rules.apply_ai_update(state, planned, NOW.replace(hour=20))
        changed = {**planned, "ledger_hash": "different"}
        with self.assertRaisesRegex(ValueError, "发生变化"):
            strategy_rules.apply_ai_update(state, changed, NOW.replace(hour=20))

    def test_daily_insufficient_samples_does_not_block_weekly_adjustment(self):
        state, report = self.report(count=1)
        with patch("ai_strategy.structured", return_value=self.result()):
            daily = strategy_rules.plan_ai_update(state, report)
        self.assertEqual(strategy_rules.apply_ai_update(state, daily, NOW.replace(hour=20))["status"], "unchanged")
        self.assertEqual(state["strategy_adjustments"], [])
        _, weekly_report = self.report(count=10)
        weekly_report["period"] = "weekly"
        with patch("ai_strategy.structured", return_value=self.result(minimum_reward_risk=2.2)):
            weekly = strategy_rules.plan_ai_update(state, weekly_report)
        self.assertEqual(strategy_rules.apply_ai_update(state, weekly, NOW.replace(hour=20))["status"], "applied")
        self.assertEqual(len(state["strategy_adjustments"]), 1)

    def test_cli_applies_and_reuses_frozen_review_without_calling_model_twice(self):
        state, report = self.report()
        with TemporaryDirectory() as directory, patch("server.TRADING_STATE_PATH", Path(directory) / "state.json"), \
             patch("sys.argv", ["trade_review.py", "--period", "daily", "--apply-rules"]), \
             patch("trade_review.datetime") as clock, patch("trade_review.close_prices", return_value={}), \
             patch("trade_review.signal_performance", return_value=report["signal_review"]), \
             patch("ai_strategy.structured", return_value=self.result(minimum_reward_risk=2.2)) as model:
            clock.now.return_value = NOW.replace(hour=20)
            server.save_trading_state(state)
            for expected in ("applied", "already_applied"):
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    self.assertEqual(trade_review.main(), 0)
                self.assertEqual(json.loads(stdout.getvalue())["rule_update"]["status"], expected)
            self.assertEqual(model.call_count, 1)
            latest = server.load_trading_state()
            self.assertEqual(len(latest["ai_reviews"]), 1)
            self.assertEqual(latest["strategy_adjustments"][0]["after"]["minimum_reward_risk"], 2.2)

    def test_cli_stops_if_ledger_changes_while_waiting_for_ai(self):
        state, report = self.report()
        with TemporaryDirectory() as directory, patch("server.TRADING_STATE_PATH", Path(directory) / "state.json"), \
             patch("sys.argv", ["trade_review.py", "--period", "daily", "--apply-rules"]), \
             patch("trade_review.datetime") as clock, patch("trade_review.close_prices", return_value={}), \
             patch("trade_review.signal_performance", return_value=report["signal_review"]):
            clock.now.return_value = NOW.replace(hour=20)
            server.save_trading_state(state)

            def change_ledger(*args):
                with server.trading_state_transaction() as path:
                    latest = server.load_trading_state(path)
                    latest["actual_holdings"] = [{"code": "changed"}]
                    server.save_trading_state(latest, path)
                return self.result(minimum_reward_risk=2.2)

            with patch("ai_strategy.structured", side_effect=change_ledger), \
                 patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(trade_review.main(), 1)
            self.assertIn("发生变化", json.loads(stdout.getvalue())["error"])
            self.assertEqual(server.load_trading_state()["strategy_adjustments"], [])

    def test_concurrent_daily_adjustment_does_not_prevent_weekly_report(self):
        state, report = self.report()
        with patch("ai_strategy.structured", return_value=self.result(minimum_reward_risk=2.2)):
            daily_plan = strategy_rules.plan_ai_update(state, report)
        with TemporaryDirectory() as directory, patch("server.TRADING_STATE_PATH", Path(directory) / "state.json"), \
             patch("sys.argv", ["trade_review.py", "--period", "weekly", "--apply-rules"]), \
             patch("trade_review.datetime") as clock, patch("trade_review.close_prices", return_value={}), \
             patch("trade_review.signal_performance", return_value=report["signal_review"]):
            clock.now.return_value = NOW.replace(hour=20)
            server.save_trading_state(state)

            def finish_daily_while_weekly_waits(*args):
                with server.trading_state_transaction() as path:
                    latest = server.load_trading_state(path)
                    strategy_rules.apply_ai_update(latest, daily_plan, NOW.replace(hour=20))
                    server.save_trading_state(latest, path)
                return self.result(minimum_reward_risk=2.1)

            with patch("ai_strategy.structured", side_effect=finish_daily_while_weekly_waits), \
                 patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(trade_review.main(), 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["rule_update"]["status"], "already_applied")
            self.assertEqual(result["rule_update"]["after"]["minimum_reward_risk"], 2.2)
            self.assertEqual(len(server.load_trading_state()["strategy_adjustments"]), 1)
