import copy
import io
import json
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import scan_and_push
import send_bot
import server
import tests.test_server as fixtures


NOW = datetime(2026, 8, 25, 10, 0, tzinfo=server.SHANGHAI)


def prepare(path, codes=("000001",)):
    fixture = fixtures.DashboardLogicTests()
    candidates = [{**fixture.candidate(code), "last_time": "1000"} for code in codes]
    analysis = fixture.scan_analysis(candidates=candidates, universe=candidates)
    with patch("server.run_analysis", return_value=analysis):
        return server.lightclaw_scan_prepare(NOW, path)


def completed(payload, code=0):
    return subprocess.CompletedProcess([], code, json.dumps(payload), "")


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_sell_code_comes_from_action_not_date_or_buy_disclaimer(self):
        action = {"side": "sell", "code": "300757", "name": "测试", "notification_fingerprint": "a" * 64}
        card = scan_and_push.build_signal_card({
            "message": "20261028 卖出；当日买入不可卖", "actions": [action], "decision_at": "10:00",
        })
        button = next(item for item in card["body"]["elements"] if item["tag"] == "button")
        self.assertEqual(button["behaviors"][0]["value"], {
            "action": "confirm_sell", "code": "300757", "signal_id": "a" * 64,
        })
        self.assertEqual(card["header"]["template"], "red")

    def test_multiple_and_mixed_actions_get_individual_buttons(self):
        actions = [
            {"side": side, "code": code, "name": code, "notification_fingerprint": code}
            for side, code in (("buy", "000001"), ("sell", "000002"))
        ]
        card = scan_and_push.build_signal_card({"message": "test", "actions": actions, "decision_at": "10:00"})
        self.assertEqual(card["header"]["template"], "blue")
        buttons = [item for item in card["body"]["elements"] if item["tag"] == "button"]
        self.assertEqual(len(buttons), 2)

    def test_ai_failure_is_visible_without_changing_action(self):
        action = {"side": "sell", "code": "300757", "name": "测试", "notification_fingerprint": "a" * 64}
        original = copy.deepcopy(action)
        with patch("llm.analyze_sell_signal", side_effect=RuntimeError("offline")), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            card = scan_and_push.build_signal_card({"message": "test", "actions": [action], "decision_at": "10:00"})
        self.assertIn("暂不可用", json.dumps(card, ensure_ascii=False))
        self.assertIn("offline", stderr.getvalue())
        self.assertEqual(action, original)
        self.assertEqual(card["header"]["template"], "red")

    def test_resume_across_windows_preserves_frozen_actions(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            first = prepare(path)
            original = path.read_bytes()
            with patch("server.run_analysis") as analyzer:
                for later in (NOW + timedelta(minutes=5), NOW.replace(hour=16), NOW + timedelta(days=1)):
                    resumed = server.lightclaw_scan_prepare(later, path, resume_pending=True)
                    self.assertEqual(resumed["status"], "already_pending")
                    self.assertEqual(resumed["actions"], first["actions"])
                    self.assertEqual(resumed["commit_token"], first["commit_token"])
            analyzer.assert_not_called()
            self.assertEqual(path.read_bytes(), original)

    def test_delivery_marker_validates_and_does_not_change_pending(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            result = prepare(path)
            pending = copy.deepcopy(server.load_trading_state(path)["pending_report"])
            for event, message in (("other", "om_1"), (result["event_id"], "")):
                with self.assertRaises(ValueError):
                    server.record_scan_delivery(event, message, path)
            server.record_scan_delivery(result["event_id"], "om_1", path)
            server.record_scan_delivery(result["event_id"], "om_1", path)
            with self.assertRaises(ValueError):
                server.record_scan_delivery(result["event_id"], "om_2", path)
            state = server.load_trading_state(path)
            self.assertEqual(state["pending_report"], pending)
            server._validate_pending(pending, state)

    def test_commit_failure_retries_without_resending(self):
        with TemporaryDirectory() as directory, patch("server.TRADING_STATE_PATH", Path(directory) / "state.json"):
            result = prepare(server.TRADING_STATE_PATH)
            success = {"code": 0, "data": {"message_id": "om_1"}}
            with patch("scan_and_push.run", side_effect=[
                completed(result), completed(success), completed({"status": "error"}, 1),
            ]) as runner, self.assertRaises(RuntimeError):
                scan_and_push.scan_once()
            uuid = runner.call_args_list[1].args[0][-1]
            self.assertEqual(len(uuid), 32)
            resumed = server.lightclaw_scan_prepare(NOW + timedelta(minutes=5), resume_pending=True)
            with patch("scan_and_push.run", side_effect=[
                completed(resumed), completed({"status": "committed"}),
            ]) as retry, patch("builtins.print"):
                self.assertTrue(scan_and_push.scan_once())
            self.assertEqual(retry.call_count, 2)
            self.assertIn("--lightclaw-commit", retry.call_args_list[-1].args[0])

    def test_send_failures_do_not_commit_and_uuid_is_stable(self):
        with TemporaryDirectory() as directory:
            result = prepare(Path(directory) / "state.json")
            uuids = []
            for failure in (completed({"code": 123}), completed({"error": "failed"}, 1)):
                with patch("scan_and_push.run", side_effect=[completed(result), failure]) as runner, \
                     patch("server.record_scan_delivery") as marker, self.assertRaises(RuntimeError):
                    scan_and_push.scan_once()
                uuids.append(runner.call_args_list[1].args[0][-1])
                self.assertEqual(runner.call_count, 2)
                marker.assert_not_called()
            self.assertEqual(uuids[0], uuids[1])

    def test_no_signal_is_successful_cli_exit_and_errors_are_not(self):
        with patch("sys.argv", ["scan_and_push.py"]), patch("scan_and_push.run", return_value=completed(
            {"status": "no_signal"},
        )), patch("builtins.print"):
            self.assertEqual(scan_and_push.main(), 0)
        with patch("sys.argv", ["scan_and_push.py"]), patch("scan_and_push.run", return_value=completed(
            {"status": "error"}, 1,
        )), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(scan_and_push.main(), 1)

    def test_sender_uuid_and_business_errors(self):
        with patch("send_bot.get_tenant_token", return_value="token"), patch(
            "send_bot.http_post", return_value={"code": 0, "data": {"message_id": "om_1"}},
        ) as post:
            send_bot.send_card("oc_test", "{}", "unique")
            self.assertEqual(post.call_args.args[1]["uuid"], "unique")
        for result in ({"code": 123}, {"code": 0, "data": {}}):
            with patch("send_bot.get_tenant_token", return_value="token"), \
                 patch("send_bot.http_post", return_value=result), self.assertRaises(RuntimeError):
                send_bot.send_card("oc_test", "{}")

    def test_sender_cli_outputs_one_json_object(self):
        result = {"code": 0, "data": {"message_id": "om_1"}}
        with patch("sys.argv", ["send_bot.py", "oc_test", "{}", "--uuid", "test"]), \
             patch("send_bot.send_card", return_value=result), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(send_bot.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), result)

    def test_resume_outside_window_without_pending_is_noop(self):
        with TemporaryDirectory() as directory, patch("server.run_analysis") as analyzer:
            result = server.lightclaw_scan_prepare(
                NOW.replace(hour=16), Path(directory) / "state.json", resume_pending=True,
            )
        self.assertEqual(result["status"], "no_signal")
        analyzer.assert_not_called()
