import unittest
from unittest.mock import patch

import server


class WatchlistTests(unittest.TestCase):
    def test_open_actual_positions_join_pool_without_duplicates(self):
        state = server.empty_trading_state()
        state["actual_holdings"] = [
            {"code": "000001", "status": "open"},
            {"code": "000001", "status": "open"},
            {"code": "000002", "status": "closed"},
            {"code": server.WATCHLIST[0], "status": "open"},
        ]
        with patch("server.load_trading_state", return_value=state):
            pool = server.effective_watchlist()
        self.assertEqual(pool, list(dict.fromkeys(server.WATCHLIST + ["000001"])))

    def test_invalid_actual_code_fails_explicitly(self):
        state = server.empty_trading_state()
        state["actual_holdings"] = [{"code": "invalid"}]
        with patch("server.load_trading_state", return_value=state), self.assertRaises(server.CorruptLocalData):
            server.effective_watchlist()

    def test_closed_position_leaves_pool_and_changes_cache_key(self):
        state = server.empty_trading_state()
        original_cache = dict(server._cache)
        self.addCleanup(server._cache.update, original_cache)
        state["actual_holdings"] = [{"code": "000001", "status": "open"}]
        with patch("server.load_trading_state", return_value=state), patch("server.run_analysis") as analyze:
            first = {"stock_pool": server.effective_watchlist()}
            second = {"stock_pool": list(server.WATCHLIST)}
            analyze.side_effect = [first, second]
            self.assertEqual(server.get_analysis(force=True), first)
            self.assertEqual(server.get_analysis(), first)
            state["actual_holdings"] = []
            self.assertEqual(server.get_analysis(), second)
            self.assertEqual(analyze.call_count, 2)

    def test_dynamic_pool_is_used_for_fetching(self):
        state = server.empty_trading_state()
        state["actual_holdings"] = [{"code": "000001", "status": "open"}]
        with patch("server.load_trading_state", return_value=state), \
             patch("server.fetch_minute", return_value={}) as minute, \
             patch("server.fetch_history", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "全部获取失败"):
                server.run_analysis(persist=False)
        self.assertIn(server.symbol_for("000001"), [call.args[0] for call in minute.call_args_list])
