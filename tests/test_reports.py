import copy
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import llm
import server
import tests.test_server as fixtures
from tests.test_delivery import NOW


class ReportTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"DEEPSEEK_API_KEY": "", "ASTOCK_MANUAL_HOLDINGS": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def analysis(self):
        fixture = fixtures.DashboardLogicTests()
        stock = {**fixture.candidate(), "eligible": True, "failures": []}
        return fixture.scan_analysis(universe=[stock])

    def test_missing_api_key_disables_network(self):
        with patch("urllib.request.urlopen") as network:
            self.assertEqual(llm.chat("system", "user"), "")
            self.assertEqual(llm.report_sections(self.analysis()), {})
        network.assert_not_called()

    def test_llm_failure_is_explicit_and_never_prints_to_stdout(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}), \
             patch("urllib.request.urlopen", side_effect=OSError("offline")), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            with self.assertRaises(RuntimeError):
                llm.chat("system", "user")
        self.assertEqual(stdout.getvalue(), "")

    def test_llm_rejects_empty_or_malformed_success_response(self):
        for payload in ({}, {"choices": []}, {"choices": [{"message": {"content": None}}]}):
            with self.subTest(payload=payload), patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}), \
                 patch("urllib.request.urlopen") as network, self.assertRaises(RuntimeError):
                network.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
                llm.chat("system", "user")

    def test_ai_candidates_filtered_and_input_analysis_unchanged(self):
        analysis = self.analysis()
        analysis["universe"].append({"code": "000002", "eligible": False, "failures": ["risk"]})
        analysis["universe"][0]["chart"] = ["large data not needed"]
        original = copy.deepcopy(analysis)
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}), \
             patch("llm.daily_market_summary", return_value="summary"), \
             patch("llm.pick_top_picks", return_value="picks") as picks:
            sections = llm.report_sections(analysis)
        self.assertEqual(len(sections), 2)
        candidates = picks.call_args.args[0]
        self.assertEqual([item["code"] for item in candidates], ["000001"])
        self.assertNotIn("chart", candidates[0])
        self.assertEqual(analysis, original)
        analysis["market"]["state"] = "red"
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}), \
             patch("llm.daily_market_summary", return_value="risk"), patch("llm.pick_top_picks") as picks:
            llm.report_sections(analysis)
        picks.assert_not_called()

    def run_print_report(self, analysis, state):
        with patch("sys.argv", ["server.py", "--print-report", "close-summary"]), \
             patch("server.datetime") as clock, patch("server.run_analysis", return_value=analysis), \
             patch("server.load_trading_state", return_value=state), \
             patch("server.save_trading_state") as save, \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            clock.now.return_value = NOW.replace(hour=16)
            code = server.main()
        save.assert_not_called()
        return code, json.loads(output.getvalue())

    def test_print_report_includes_holdings_and_survives_ai_failure(self):
        analysis = self.analysis()
        analysis["market_data_time"] = "1500"
        analysis["universe"][0]["price"] = 11
        state = server.empty_trading_state()
        state["actual_holdings"] = [{
            "code": "000001", "buy_price": 10, "shares": 100, "buy_date": "20260824", "source": "user-confirmed",
        }]
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}), \
             patch("llm.daily_market_summary", side_effect=RuntimeError("offline")), \
             patch("llm.pick_top_picks", side_effect=RuntimeError("offline")), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            code, card = self.run_print_report(analysis, state)
        self.assertEqual(code, 0)
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("浮动盈亏：+100.00 元", text)
        self.assertIn("暂不可用", text)
        self.assertIn("失败", stderr.getvalue())
        self.assertEqual(card["schema"], "2.0")

    def test_print_report_skip_is_one_json_object(self):
        analysis = self.analysis()
        code, result = self.run_print_report(analysis, server.empty_trading_state())
        self.assertEqual((code, result["status"]), (0, "no_report"))
        analysis["trade_date"] = "20260824"
        code, result = self.run_print_report(analysis, server.empty_trading_state())
        self.assertEqual((code, result["status"]), (0, "no_report"))

    def test_scheduled_close_uses_same_ai_sections_and_actual_pnl(self):
        analysis = self.analysis()
        analysis["market_data_time"] = "1500"
        analysis["universe"][0]["price"] = 11
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = server.empty_trading_state()
            state["actual_holdings"] = [{
                "code": "000001", "shares": 100, "buy_price": 10, "buy_date": "20260824",
            }]
            server.save_trading_state(state, path)
            with patch("server.run_analysis", return_value=analysis), patch("server.send_feishu_push") as send, \
                 patch("llm.report_sections", return_value={"AI盘面解读": "shared-summary"}):
                result = server.scheduled_push("close-summary", now=NOW.replace(hour=16), state_path=path)
            self.assertTrue(result["sent"])
            message = server.format_scheduled_message(send.call_args.args[0])
            self.assertIn("shared-summary", message)
            self.assertIn("浮动盈亏：+100.00 元", message)
            self.assertEqual(server.load_trading_state(path)["actual_holdings"], state["actual_holdings"])

    def test_report_limit_keeps_disclaimer(self):
        report = server.build_scheduled_report("close-summary", self.analysis(), server.empty_trading_state())
        report["ai_sections"] = {"AI": "x" * 8000}
        message = server.format_scheduled_message(report)
        self.assertEqual(len(message), 3800)
        self.assertTrue(message.endswith("仅供研究参考，不构成个性化投资建议。"))
