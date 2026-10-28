import copy
import io
import json
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import server
import trade_review


END = date(2026, 10, 28)


def lot(**changes):
    return {
        "id": "entry1", "code": "300398", "name": "飞凯材料", "buy_date": "20261028",
        "buy_price": 28.16, "shares": 500, **changes,
    }


class TradeReviewTests(unittest.TestCase):
    def test_daily_new_buy_uses_actual_cost_not_previous_close(self):
        state = server.empty_trading_state()
        state["actual_holdings"] = [lot()]
        original = copy.deepcopy(state)
        report = trade_review.build_review(state, "daily", END, {
            "300398": {"before": 27, "close": 29},
        })
        self.assertEqual(report["period_pnl"], 420)
        self.assertEqual(report["floating_pnl"], 420)
        self.assertEqual(report["realized_pnl"], 0)
        self.assertEqual(report["buys"][0]["shares"], 500)
        self.assertEqual(state, original)

    def test_older_holding_daily_gain_differs_from_lifetime_floating(self):
        state = server.empty_trading_state()
        state["actual_holdings"] = [lot(buy_date="20261021")]
        report = trade_review.build_review(state, "daily", END, {
            "300398": {"before": 29, "close": 28},
        })
        self.assertEqual(report["period_pnl"], -500)
        self.assertEqual(report["floating_pnl"], -80)
        self.assertEqual(report["buys"], [])

    def test_partial_sale_counts_original_buy_once_and_profit_separately(self):
        state = server.empty_trading_state()
        state["actual_holdings"] = [lot(shares=300, buy_date="20261027", buy_price=20)]
        state["actual_history"] = [lot(
            shares=200, buy_date="20261027", buy_price=20, sell_date="20261028", sell_price=22,
        )]
        prices = {"300398": {"before": 21, "close": 23}}
        daily = trade_review.build_review(state, "daily", END, prices)
        self.assertEqual(daily["realized_pnl"], 400)
        self.assertEqual(daily["period_pnl"], 800)
        self.assertEqual(daily["floating_pnl"], 900)
        weekly = trade_review.build_review(state, "weekly", END, prices)
        self.assertEqual(weekly["start"], "2026-10-26")
        self.assertEqual(weekly["buys"][0]["shares"], 500)
        self.assertEqual(weekly["period_pnl"], 1300)

    def test_missing_prices_make_total_unknown_not_zero(self):
        state = server.empty_trading_state()
        state["actual_holdings"] = [lot(buy_date="20261021")]
        report = trade_review.build_review(state, "daily", END, {})
        self.assertIsNone(report["period_pnl"])
        self.assertIsNone(report["floating_pnl"])
        self.assertTrue(report["missing_data"])

    def test_sale_without_open_holding_includes_realized_loss(self):
        state = server.empty_trading_state()
        state["actual_history"] = [lot(buy_date="20261021", sell_date="20261028", sell_price=27)]
        report = trade_review.build_review(state, "daily", END, {"300398": {"before": 28}})
        self.assertEqual(report["period_pnl"], -500)
        self.assertEqual(report["realized_pnl"], -580)
        self.assertEqual(report["losing_sales"], 1)

    def test_apply_rules_persists_without_send_and_retry_does_not_change_again(self):
        night = datetime(2026, 10, 28, 20, tzinfo=server.SHANGHAI)
        with TemporaryDirectory() as directory, patch("server.TRADING_STATE_PATH", Path(directory) / "state.json"), \
             patch("sys.argv", ["trade_review.py", "--period", "daily", "--apply-rules"]), \
             patch("trade_review.close_prices", return_value={"300398": {"before": 28}}), \
             patch("trade_review.datetime") as clock:
            clock.now.return_value = night
            state = server.empty_trading_state()
            state["actual_history"] = [lot(buy_date="20261021", sell_date="20261028", sell_price=27)]
            server.save_trading_state(state)
            for status in ("applied", "already_applied"):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(trade_review.main(), 0)
                report = json.loads(output.getvalue())
                self.assertEqual(report["rule_update"]["status"], status)
                self.assertEqual(report["rule_update"]["after"]["minimum_reward_risk"], 2.2)
            self.assertEqual(len(server.load_trading_state()["strategy_adjustments"]), 1)

    def test_historical_review_restores_later_sold_position(self):
        state = server.empty_trading_state()
        state["actual_history"] = [lot(sell_date="20261029", sell_price=30)]
        report = trade_review.build_review(state, "daily", END, {"300398": {"close": 29}})
        self.assertEqual(report["sells"], [])
        self.assertEqual(report["holdings"][0]["shares"], 500)
        self.assertEqual(report["period_pnl"], 420)

    def test_empty_ledger_and_assumed_positions_do_not_fabricate_actual_trades(self):
        state = server.empty_trading_state()
        state["holdings"] = [lot()]
        report = trade_review.build_review(state, "weekly", date(2026, 10, 30), {})
        self.assertEqual(report["period_pnl"], 0)
        self.assertEqual(report["buys"], [])
        self.assertEqual(report["assumed_holdings_count"], 1)
        self.assertIn("规则更新", json.dumps(trade_review.review_card(report), ensure_ascii=False))

    def test_incomplete_minute_does_not_count_as_close(self):
        history = {"raw_rows": [["2026-10-27", 27, 28, 29, 26, 100]]}
        with patch("server.fetch_history", return_value=history), patch(
            "server.fetch_minute", return_value={"trade_date": "20261028"},
        ), patch("server.parse_minute", return_value={"last_time": "1459", "price": 30}):
            prices = trade_review.close_prices({"300398"}, END, END)
        self.assertEqual(prices["300398"]["before"], 28)
        self.assertIsNone(prices["300398"]["close"])

    def test_complete_close_used_when_daily_bars_lag(self):
        with patch("server.fetch_history", return_value={"raw_rows": []}), patch(
            "server.fetch_minute", return_value={"trade_date": "20261028"},
        ), patch("server.parse_minute", return_value={"last_time": "1500", "price": 30}):
            prices = trade_review.close_prices({"300398"}, END, END)
        self.assertEqual(prices["300398"]["close"], 30)

    def test_minute_failure_preserves_verified_daily_close(self):
        history = {"raw_rows": [
            ["2026-10-27", 27, 28, 29, 26, 100],
            ["2026-10-28", 28, 29, 30, 27, 100],
        ]}
        with patch("server.fetch_history", return_value=history), \
             patch("server.fetch_minute", side_effect=OSError("offline")), \
             patch("sys.stderr", new_callable=io.StringIO):
            prices = trade_review.close_prices({"300398"}, END, END)
        self.assertEqual(prices["300398"]["close"], 29)
        self.assertEqual(prices["300398"]["before"], 28)

    def test_cli_json_only_and_successful_delivery_is_deduplicated(self):
        with TemporaryDirectory() as directory, patch("server.TRADING_STATE_PATH", Path(directory) / "state.json"), \
             patch("sys.argv", ["trade_review.py", "--period", "daily", "--send"]), \
             patch("trade_review.close_prices", return_value={}), \
             patch("trade_review.send_card", return_value={"data": {"message_id": "om_review"}}) as send:
            with patch("sys.stdout", new_callable=io.StringIO) as first:
                self.assertEqual(trade_review.main(), 0)
            self.assertEqual(json.loads(first.getvalue())["delivery"]["status"], "sent")
            with patch("sys.stdout", new_callable=io.StringIO) as second:
                self.assertEqual(trade_review.main(), 0)
            self.assertEqual(json.loads(second.getvalue())["delivery"]["status"], "already_sent")
            self.assertEqual(send.call_count, 1)

    def test_send_failure_is_not_saved_as_success(self):
        with TemporaryDirectory() as directory, patch("server.TRADING_STATE_PATH", Path(directory) / "state.json"), \
             patch("sys.argv", ["trade_review.py", "--period", "weekly", "--send"]), \
             patch("trade_review.close_prices", return_value={}), \
             patch("trade_review.send_card", side_effect=RuntimeError("failed")), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(trade_review.main(), 1)
            self.assertEqual(json.loads(output.getvalue())["status"], "error")
            self.assertNotIn("actual_review_deliveries", server.load_trading_state())
