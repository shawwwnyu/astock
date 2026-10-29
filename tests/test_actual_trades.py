import copy
import json
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import actual_trades
import bot_ws
import server
import tests.test_server as fixtures
from tests.test_delivery import NOW, prepare


class ActualTradeTests(unittest.TestCase):
    def setUp(self):
        chat = patch("bot_ws.TARGET_CHAT_ID", "oc_test")
        chat.start()
        self.addCleanup(chat.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        patched = patch("server.TRADING_STATE_PATH", self.path)
        patched.start()
        self.addCleanup(patched.stop)

    def trade(self, side="buy", price=10, shares=100, request_id="buy", now=NOW, **kwargs):
        return actual_trades.record_trade(
            "000001", side, price, shares, request_id=request_id, now=now, **kwargs,
        )

    def test_explicit_quantity_cost_and_no_zero_target(self):
        self.trade(price="10.5", shares="100")
        state = server.load_trading_state()
        holding = state["actual_holdings"][0]
        self.assertEqual((holding["shares"], holding["buy_price"], holding["capital_used"]), (100, 10.5, 1050))
        self.assertIsNone(holding["target_price"])
        self.assertIsNone(holding["invalidation"])
        self.assertEqual(state["holdings"], [])

    def test_invalid_fills_never_write_state(self):
        for price in (None, True, "nan", "inf", "-inf", 0, -1, "bad"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                self.trade(price=price)
        for shares in (None, True, 0, -1, 0.5, "1.5", "nan", "100.0"):
            with self.subTest(shares=shares), self.assertRaises(ValueError):
                self.trade(shares=shares)
        self.assertFalse(self.path.exists())

    def test_retries_are_idempotent_and_changed_fill_is_explicit_error(self):
        self.assertFalse(self.trade()["duplicate"])
        self.assertTrue(self.trade()["duplicate"])
        with self.assertRaisesRegex(ValueError, "不同"):
            self.trade(price=11)
        self.assertEqual(len(server.load_trading_state()["actual_holdings"]), 1)

    def test_t_plus_one_oversell_partial_and_final_sale(self):
        self.trade(shares=200)
        with self.assertRaisesRegex(ValueError, "T\\+1"):
            self.trade("sell", request_id="sell")
        later = NOW + timedelta(days=1)
        with self.assertRaisesRegex(ValueError, "超过"):
            self.trade("sell", shares=201, request_id="oversell", now=later)
        self.trade("sell", price=11, shares=50, request_id="partial", now=later)
        state = server.load_trading_state()
        self.assertEqual(state["actual_holdings"][0]["shares"], 150)
        self.assertEqual(state["actual_holdings"][0]["capital_used"], 1500)
        self.assertEqual(state["actual_history"][0]["shares"], 50)
        self.assertEqual(state["actual_history"][0]["return_pct"], 10)
        self.trade("sell", price=11, shares=150, request_id="final", now=later)
        self.assertEqual(server.load_trading_state()["actual_holdings"], [])

    def test_assumed_position_alone_is_not_sellable_actual_position(self):
        state = server.empty_trading_state()
        state["holdings"] = [fixtures.DashboardLogicTests.holding()]
        server.save_trading_state(state)
        with self.assertRaisesRegex(ValueError, "无已确认"):
            self.trade("sell")
        self.assertEqual(server.load_trading_state()["holdings"], state["holdings"])

    def test_confirmation_before_commit_preserves_pending_then_survives_commit(self):
        result = prepare(self.path)
        signal_id = result["actions"][0]["notification_fingerprint"]
        before = copy.deepcopy(server.load_trading_state())
        self.trade(price=10.4, shares=100, signal_id=signal_id)
        state = server.load_trading_state()
        for key in ("holdings", "history", "pending_report", "signal_states"):
            self.assertEqual(state[key], before[key])
        server._validate_pending(state["pending_report"], state)
        server.lightclaw_commit(result["commit_token"], NOW, self.path)
        state = server.load_trading_state()
        self.assertEqual(state["holdings"][0]["shares"], 3000)
        self.assertEqual(state["actual_holdings"][0]["shares"], 100)
        self.assertTrue(self.trade(price=10.4, signal_id=signal_id)["duplicate"])

    def test_confirmation_after_commit_and_stale_or_wrong_signal(self):
        result = prepare(self.path)
        signal_id = result["actions"][0]["notification_fingerprint"]
        server.lightclaw_commit(result["commit_token"], NOW, self.path)
        with self.assertRaisesRegex(ValueError, "过期"):
            self.trade(signal_id=signal_id, now=NOW + timedelta(days=1))
        with self.assertRaisesRegex(ValueError, "方向"):
            self.trade("sell", signal_id=signal_id)
        self.trade(signal_id=signal_id)
        self.assertEqual(server.load_trading_state()["actual_holdings"][0]["target_price"], 11.2)

    def test_concurrent_updates_keep_both_actual_positions_and_pending(self):
        prepare(self.path)
        pending = server.load_trading_state()["pending_report"]
        def write(code):
            return actual_trades.record_trade(code, "buy", 10, 100, request_id=code, now=NOW)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(write, ("000002", "000003")))
        state = server.load_trading_state()
        self.assertEqual({item["code"] for item in state["actual_holdings"]}, {"000002", "000003"})
        self.assertEqual(state["pending_report"], pending)
        server._validate_pending(pending, state)

    def test_separate_processes_preserve_each_others_updates(self):
        prepare(self.path)
        script = (
            "import sys; from pathlib import Path; from datetime import datetime; "
            "import actual_trades; actual_trades.record_trade("
            "sys.argv[2], 'buy', 10, 100, request_id=sys.argv[2], "
            "now=datetime.fromisoformat(sys.argv[3]), state_path=Path(sys.argv[1]))"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-B", "-c", script, str(self.path), code, NOW.isoformat()],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for code in ("000002", "000003")
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        state = server.load_trading_state()
        self.assertEqual({item["code"] for item in state["actual_holdings"]}, {"000002", "000003"})
        server._validate_pending(state["pending_report"], state)

    def test_signal_sale_requires_matching_entry_and_supports_retry(self):
        result = prepare(self.path)
        buy_id = result["actions"][0]["notification_fingerprint"]
        server.lightclaw_commit(result["commit_token"], NOW, self.path)
        self.trade(signal_id=buy_id)
        state = server.load_trading_state()
        sale_id = "b" * 64
        state["delivered_signals"][sale_id] = {
            "side": "sell", "code": "000001", "name": "测试", "trade_date": "20260826",
            "event_id": "sale", "notification_fingerprint": sale_id, "holding_id": "wrong-entry",
        }
        server.save_trading_state(state)
        later = NOW + timedelta(days=1)
        with self.assertRaisesRegex(ValueError, "批次"):
            self.trade("sell", signal_id=sale_id, request_id="sale", now=later)
        state["delivered_signals"][sale_id]["holding_id"] = state["actual_holdings"][0]["strategy_holding_id"]
        server.save_trading_state(state)
        self.trade("sell", signal_id=sale_id, request_id="sale", now=later)
        self.assertTrue(self.trade("sell", signal_id=sale_id, request_id="retry", now=later)["duplicate"])
        self.assertEqual(len(server.load_trading_state()["actual_history"]), 1)

    def test_legacy_zero_risk_levels_do_not_create_immediate_exit(self):
        fixture = fixtures.DashboardLogicTests()
        state = server.empty_trading_state()
        state["holdings"] = [{**fixture.holding(buy_date="20260824"), "target_price": 0, "invalidation": 0}]
        review = server.review_holdings(state, fixture.scan_analysis(
            universe=[{"code": "000001", "price": 10, "last_time": "1000"}],
        ))[0]
        self.assertEqual(review["decision"], "hold")

    @staticmethod
    def event(*, name="", value=None, form=None, chat="oc_test"):
        return SimpleNamespace(event=SimpleNamespace(
            context=SimpleNamespace(open_chat_id=chat),
            action=SimpleNamespace(name=name, value=value, form_value=form),
        ))

    def test_manual_form_requires_both_inputs_and_does_not_write(self):
        card = json.loads(bot_ws.handle_buy("000001"))
        form = next(item for item in card["body"]["elements"] if item["tag"] == "form")
        inputs = [item for item in form["elements"] if item["tag"] == "input"]
        self.assertEqual({item["name"] for item in inputs}, {"price", "shares"})
        self.assertTrue(all(item["required"] for item in inputs))
        button = form["elements"][-1]
        self.assertNotIn("behaviors", button)
        self.assertEqual(button["form_action_type"], "submit")
        self.assertTrue(button["name"].startswith("trade_buy_000001_manual"))
        self.assertFalse(self.path.exists())

    def test_callback_by_form_name_records_and_rejects_bad_price_or_chat(self):
        name = "trade_buy_000001_manual" + "a" * 32
        with patch("bot_ws.card_response", side_effect=lambda payload: payload), \
             patch("actual_trades.datetime") as clock:
            clock.now.return_value = NOW
            bad = bot_ws.do_card_action(self.event(name=name, form={"price": "nan", "shares": "100"}))
            self.assertEqual(bad["toast"]["type"], "error")
            self.assertNotIn("card", bad)
            wrong_chat = bot_ws.do_card_action(self.event(name=name, form={"price": "10", "shares": "100"}, chat="other"))
            self.assertEqual(wrong_chat["toast"]["type"], "error")
            self.assertFalse(self.path.exists())
            good = bot_ws.do_card_action(self.event(name=name, form={"price": "10", "shares": "100"}))
            self.assertEqual(good["toast"]["type"], "success")
            retry = bot_ws.do_card_action(self.event(name=name, form={"price": "10", "shares": "100"}))
            self.assertIn("未重复写入", retry["toast"]["content"])

    def test_multi_stock_form_and_receipt_preserve_other_buttons(self):
        result = prepare(self.path, ("000001", "000002"))
        signal_id = result["actions"][0]["notification_fingerprint"]
        with patch("bot_ws.datetime") as clock, patch("actual_trades.datetime") as ledger_clock, \
             patch("bot_ws.card_response", side_effect=lambda payload: payload):
            clock.now.return_value = NOW
            ledger_clock.now.return_value = NOW
            opened = bot_ws.do_card_action(self.event(value={
                "action": "confirm_buy", "code": "000001", "signal_id": signal_id,
            }))
            card = opened["card"]["data"]
            other = [item for item in card["body"]["elements"] if item["tag"] == "button"]
            self.assertEqual(other[0]["behaviors"][0]["value"]["code"], "000002")
            receipt = bot_ws.do_card_action(self.event(
                name=f"trade_buy_000001_{signal_id}", form={"price": "10", "shares": "100"},
            ))
            other = [item for item in receipt["card"]["data"]["body"]["elements"] if item["tag"] == "button"]
            self.assertEqual(other[0]["behaviors"][0]["value"]["code"], "000002")

    def test_legacy_cards_rejected_and_non_target_messages_ignored(self):
        with patch("bot_ws.card_response", side_effect=lambda payload: payload):
            response = bot_ws.do_card_action(self.event(value={"action": "confirm_buy", "code": "000001"}))
        self.assertEqual(response["toast"]["type"], "error")
        event = SimpleNamespace(event=SimpleNamespace(message=SimpleNamespace(chat_id="other", message_type="text")))
        with patch("bot_ws.send_reply") as sender:
            bot_ws.do_message(event)
        sender.assert_not_called()

    def test_send_reply_raises_on_token_or_send_business_failure(self):
        with patch("bot_ws.send_card", side_effect=RuntimeError("发送失败")), self.assertRaises(RuntimeError):
            bot_ws.send_reply(bot_ws.TARGET_CHAT_ID, "{}")
