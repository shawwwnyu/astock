import json
import unittest
from unittest.mock import patch

import feishu_client
import server


class BotConfigTests(unittest.TestCase):
    def test_missing_chat_and_credentials_fail_before_network(self):
        with patch.dict("os.environ", {}, clear=True), patch("feishu_client.http_post") as network:
            with self.assertRaisesRegex(ValueError, "FEISHU_CHAT_ID"):
                feishu_client.target_chat_id()
            with self.assertRaisesRegex(ValueError, "FEISHU_APP_ID"):
                feishu_client.get_tenant_token()
            network.assert_not_called()

    def test_credentials_read_at_call_time_and_token_not_in_errors(self):
        with patch.dict("os.environ", {"FEISHU_APP_ID": "app", "FEISHU_APP_SECRET": "secret"}), \
             patch("feishu_client.http_post", return_value={"code": 0, "tenant_access_token": "token"}) as post:
            self.assertEqual(feishu_client.get_tenant_token(), "token")
            self.assertEqual(post.call_args.args[1], {"app_id": "app", "app_secret": "secret"})

    def test_scheduled_push_uses_configured_bot_chat(self):
        with patch.dict("os.environ", {
            "FEISHU_CHAT_ID": "oc_destination", "FEISHU_APP_ID": "app", "FEISHU_APP_SECRET": "secret",
        }), patch("server.load_push_config", return_value={}), \
             patch("server.send_card", return_value={"code": 0}) as send, \
             patch("server.send_feishu_direct") as legacy:
            server.send_feishu_push({"market": {}, "candidates": []})
        self.assertEqual(send.call_args.args[0], "oc_destination")
        self.assertEqual(json.loads(send.call_args.args[1])["schema"], "2.0")
        legacy.assert_not_called()

    def test_legacy_email_and_chat_share_one_transport(self):
        with patch("feishu_client.get_tenant_token", return_value="token"), patch(
            "feishu_client.http_post", return_value={"code": 0, "data": {"message_id": "om_1"}},
        ) as post:
            server.send_feishu_direct("message", "app", "secret", "user@example.com")
            self.assertTrue(post.call_args.args[0].endswith("receive_id_type=email"))
            self.assertEqual(post.call_args.args[1]["msg_type"], "text")
            feishu_client.send_card("oc_test", "{}")
            self.assertTrue(post.call_args.args[0].endswith("receive_id_type=chat_id"))

    def test_oversized_card_rejected_before_token_request(self):
        with patch("feishu_client.get_tenant_token") as token, self.assertRaises(ValueError):
            feishu_client.send_card("oc_test", "x" * 31_000)
        token.assert_not_called()

    def test_invalid_token_response_does_not_leak_response_body(self):
        with patch.dict("os.environ", {"FEISHU_APP_ID": "app", "FEISHU_APP_SECRET": "secret"}), \
             patch("feishu_client.http_post", return_value={"code": 123, "secret": "sensitive"}):
            with self.assertRaises(RuntimeError) as raised:
                feishu_client.get_tenant_token()
        self.assertNotIn("sensitive", str(raised.exception))
