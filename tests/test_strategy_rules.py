import unittest
from datetime import date, datetime
from unittest.mock import patch

import server
import strategy_rules
from tests.test_trade_review import lot, END
from tests import test_server as fixtures


NIGHT = datetime(2026, 10, 28, 20, 0, tzinfo=server.SHANGHAI)


class StrategyRuleTests(unittest.TestCase):
    def losing_state(self):
        state = server.empty_trading_state()
        state["actual_history"] = [lot(buy_date="20261021", sell_date="20261028", sell_price=27)]
        return state

    def test_loss_applies_tomorrow_not_today_and_keeps_fixed_constraints(self):
        state = self.losing_state()
        result = strategy_rules.apply_update(state, END, NIGHT)
        self.assertEqual(result["after"], {"minimum_reward_risk": 2.2, "max_holding_sessions": 2})
        self.assertEqual(strategy_rules.effective_parameters(state, "20261028"), strategy_rules.BASELINE)
        self.assertEqual(strategy_rules.effective_parameters(state, "20261029"), result["after"])
        self.assertEqual(set(result["after"]), {"minimum_reward_risk", "max_holding_sessions"})

    def test_repeated_daily_and_weekly_apply_only_once(self):
        state = self.losing_state()
        first = strategy_rules.apply_update(state, END, NIGHT)
        second = strategy_rules.apply_update(state, END, NIGHT)
        self.assertEqual(second["status"], "already_applied")
        self.assertEqual(first["after"], second["after"])
        self.assertEqual(len(state["strategy_adjustments"]), 1)

    def test_changed_evidence_after_apply_fails_explicitly(self):
        state = self.losing_state()
        strategy_rules.apply_update(state, END, NIGHT)
        state["actual_history"][0]["sell_price"] = 30
        with self.assertRaisesRegex(ValueError, "发生变化"):
            strategy_rules.apply_update(state, END, NIGHT)
        self.assertEqual(len(state["strategy_adjustments"]), 1)

    def test_no_samples_preserves_rules_and_does_not_invent_improvement(self):
        state = server.empty_trading_state()
        result = strategy_rules.apply_update(state, END, NIGHT)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(result["after"], strategy_rules.BASELINE)

    def test_historical_and_early_apply_rejected(self):
        for end, now in ((END, NIGHT.replace(hour=19)), (date(2026, 10, 27), NIGHT)):
            with self.assertRaises(ValueError):
                strategy_rules.apply_update(server.empty_trading_state(), end, now)

    def test_friday_update_skips_weekend(self):
        state = self.losing_state()
        state["actual_history"][0]["sell_date"] = "20261030"
        result = strategy_rules.apply_update(state, date(2026, 10, 30), NIGHT.replace(day=30))
        self.assertEqual(result["effective_date"], "2026-11-02")

    def test_weekend_no_trade_does_not_reset_queued_monday_rules(self):
        state = self.losing_state()
        state["actual_history"][0]["sell_date"] = "20261030"
        friday = strategy_rules.apply_update(state, date(2026, 10, 30), NIGHT.replace(day=30))
        saturday = strategy_rules.apply_update(state, date(2026, 10, 31), NIGHT.replace(day=31))
        self.assertEqual(saturday["after"], friday["after"])
        self.assertEqual(strategy_rules.effective_parameters(state, "20261102"), friday["after"])

    def test_pending_report_keeps_its_original_parameters_after_rule_update(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from tests.test_delivery import NOW, prepare
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            prepare(path)
            state = server.load_trading_state(path)
            state["actual_history"] = [lot(buy_date="20260824", sell_date="20260825", sell_price=27)]
            strategy_rules.apply_update(state, NOW.date(), NOW.replace(hour=20))
            server._validate_pending(state["pending_report"], state)
            self.assertEqual(state["pending_report"]["report"]["analysis"]["parameters"]["max_holding_sessions"], 3)

    def test_parameters_cannot_change_capital_or_exceed_bounds(self):
        for values in (
            {**strategy_rules.BASELINE, "capital": 999999},
            {"minimum_reward_risk": 1.8, "max_holding_sessions": 3},
            {"minimum_reward_risk": 2.0, "max_holding_sessions": 4},
            {"minimum_reward_risk": float("nan"), "max_holding_sessions": 3},
            {"minimum_reward_risk": 2.0, "max_holding_sessions": True},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                strategy_rules.validated(values)

    def test_profit_moves_towards_baseline_without_loosening_beyond_it(self):
        state = self.losing_state()
        strategy_rules.apply_update(state, END, NIGHT)
        state["actual_history"] = [lot(buy_date="20261021", sell_date="20261029", sell_price=30)]
        result = strategy_rules.apply_update(state, date(2026, 10, 29), NIGHT.replace(day=29))
        self.assertEqual(result["after"], {"minimum_reward_risk": 2.1, "max_holding_sessions": 3})

    def test_shorter_holding_rule_is_used_but_t_plus_one_survives(self):
        state = server.empty_trading_state()
        state["holdings"] = [fixtures.DashboardLogicTests.holding(buy_date="20261027")]
        analysis = fixtures.DashboardLogicTests.analysis(
            trade_date="20261028", universe=[{"code": "000001", "price": 10}],
            trading_dates=["20261027", "20261028"],
        )
        analysis["parameters"] = {"max_holding_sessions": 1}
        self.assertEqual(server.review_holdings(state, analysis)[0]["decision"], "sell")
        state["holdings"][0]["buy_date"] = "20261028"
        self.assertEqual(server.review_holdings(state, analysis)[0]["decision"], "hold")

    def test_entry_gate_uses_adjusted_reward_risk(self):
        bars = [{"close": 10, "low": 9.5, "high": 11.1, "volume": 1000}] * 30
        indicators = {
            "atr14": 0.5, "ma5": 10, "ma10": 9, "ma20": 9, "trend": "多头排列",
            "dif": 1, "dea": 0.5, "histogram": 1, "histogram_previous": 0.5,
            "macd": "up", "chart_bars": [],
        }
        minute = {
            "price": 10, "volume": 1000, "high": 10.1, "low": 9.9, "open": 10,
            "name": "测试", "amount": 100000000, "vwap": 10, "tail_return_pct": 0,
            "tail_low_delta_pct": 0, "tail_pullback_pct": 0, "spike_5m_pct": 0,
            "change_pct": 0, "symbol": "sz000001", "last_time": "1000", "records": [],
        }
        with patch("server.synthesize_bars", return_value=bars), patch("server.calculate_indicators", return_value=indicators):
            baseline = server.analyze_security("000001", minute, {}, {}, 600)
            changed = server.analyze_security("000001", minute, {}, {}, 600, {
                "minimum_reward_risk": 2.4, "max_holding_sessions": 2,
            })
        self.assertAlmostEqual(baseline["reward_risk"], 2.2)
        self.assertFalse(any("空间不足" in text for text in baseline["failures"]))
        self.assertTrue(any("不足2.4R" in text for text in changed["failures"]))
