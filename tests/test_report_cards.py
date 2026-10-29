import copy
import json
import unittest

from report_cards import message_card, scheduled_card, signal_button


class ReportCardTests(unittest.TestCase):
    def test_summary_renders_index_breadth_holdings_and_market_color(self):
        report = {
            "label": "收盘总结", "analysis": {
                "trade_date": "20261029", "data_as_of": "15:00",
                "market": {"state": "red", "label": "红灯"},
                "indices": [{"name": "沪深300", "price": 4000, "change_pct": -1.2}],
                "breadth": {"up": 3, "down": 5, "flat": 1, "average_change_pct": -0.5},
            },
        }
        original = copy.deepcopy(report)
        card = scheduled_card(report, "实际持仓浮动盈亏：+100元")
        self.assertEqual(card["header"]["template"], "red")
        self.assertEqual(card["schema"], "2.0")
        text = json.dumps(card, ensure_ascii=False)
        for expected in ("沪深300", "-1.20%", "上涨 3", "下跌 5", "-0.50%", "+100元"):
            self.assertIn(expected, text)
        self.assertEqual(report, original)

    def test_summary_handles_missing_and_zero_changes(self):
        card = scheduled_card({"analysis": {"indices": [{"name": "A", "change_pct": None}, {
            "name": "B", "change_pct": 0,
        }]}}, "报告")
        self.assertEqual(card["header"]["template"], "yellow")
        self.assertIn("—", json.dumps(card, ensure_ascii=False))

    def test_shared_card_does_not_mutate_input_lines(self):
        lines = ["正文"]
        card = message_card("标题", "blue", lines, "标签")
        self.assertEqual(lines, ["正文"])
        self.assertEqual(card["header"]["text_tag_list"][0]["text"]["content"], "标签")

    def test_signal_callback_identity_preserved(self):
        action = {"name": "测试", "code": "300398", "side": "sell", "notification_fingerprint": "identity"}
        self.assertEqual(signal_button(action)["behaviors"][0]["value"], {
            "action": "confirm_sell", "code": "300398", "signal_id": "identity",
        })
