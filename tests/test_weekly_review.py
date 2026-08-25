import io
import json
import os
import stat
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import server
import weekly_review as weekly


class WeeklyReviewTests(unittest.TestCase):
    def analysis(self, trade_date="20260729", candidates=None, universe=None):
        return {
            "generated_at": "2026-07-29T14:30:00+08:00",
            "trade_date": trade_date,
            "data_as_of": "2026-07-29 14:30",
            "market_data_time": "1430",
            "market": {"state": "green", "label": "绿灯", "summary": "稳定"},
            "indices": [{"symbol": "sh000300", "price": 4000, "last_time": "1430"}],
            "breadth": {},
            "trading_dates": [trade_date],
            "capital": 50_000,
            "strategy_version": server.STRATEGY_VERSION,
            "parameters": dict(server.DEFAULT_PARAMETERS),
            "universe_version": server.UNIVERSE_VERSION,
            "stock_pool": list(server.WATCHLIST),
            "stock_pool_hash": weekly.sha256_json(list(server.WATCHLIST)),
            "candidates": candidates or [],
            "universe": universe or [],
        }

    @staticmethod
    def candidate(code="000001", shares=1000):
        return {
            "code": code, "name": "行情响应名称", "price": 10.0, "shares": shares,
            "position_pct": 20, "capital_used": shares * 10, "invalidation": 9.5,
            "resistance": 11.2,
        }

    def formal_event(self, event_id, actions=None, pool=None, drawdown=None):
        event = {
            "schema": weekly.ARCHIVE_SCHEMA,
            "version": 1,
            "event_id": event_id,
            "stage": event_id.split(":", 1)[1],
            "decision_at": "2026-01-01T14:30:00+08:00",
            "strategy_version": weekly.STRATEGY_VERSION,
            "parameters": dict(weekly.DEFAULT_PARAMETERS),
            "universe_version": "historical-v1",
            "stock_pool": pool or ["000001"],
            "stock_pool_hash": weekly.sha256_json(pool or ["000001"]),
            "report": {},
            "actions": actions or [],
            "market_snapshot": {"trade_date": event_id[:8]},
            "benchmark": {"symbol": "sh000300"},
            "test_message": False,
            "formal": True,
            "committed_at": "2026-01-01T14:31:00+08:00",
            "archive_status": "committed",
        }
        if drawdown is not None:
            event["weekly_max_drawdown_pct"] = drawdown
        return event

    def write_event(self, directory, event):
        path = Path(directory) / (event["event_id"].replace(":", "-") + ".json")
        normalized = dict(event)
        normalized["actions"] = [
            {
                "action_id": action.get("action_id"), "side": action.get("side"), "code": action.get("code"),
                "shares": action.get("shares"), "expected_execution_minute": "1431",
                "raw_next_minute": {"time": "1431", "price": action.get("assumed_price"), "volume": max(1, int(action.get("shares", 0))), "suspended": False, "at_limit": False},
            }
            if action.get("status") == "assumed_filled" and action.get("tradability_verified") is True else action
            for action in event.get("actions", [])
        ]
        weekly.atomic_write_json(path, weekly.seal_formal_event(normalized))
        return path

    def test_watchlist_additions_and_symbols(self):
        self.assertIn("300790", server.WATCHLIST)
        self.assertIn("001232", server.WATCHLIST)
        self.assertEqual(server.symbol_for("300790"), "sz300790")
        self.assertEqual(server.symbol_for("001232"), "sz001232")

    def test_formal_archive_preserves_versioned_pool_not_current_watchlist(self):
        analysis = self.analysis(universe=[{"code": "000001", "name": "行情响应名称"}])
        report = server.build_scheduled_report("close-summary", analysis, server.empty_trading_state())
        pending = {"report": report, "prepared_at": analysis["generated_at"], "test_message": False}
        event = weekly.build_formal_event(pending, datetime(2026, 7, 29, 15, 31, tzinfo=server.SHANGHAI))
        self.assertEqual(event["universe_version"], server.UNIVERSE_VERSION)
        self.assertEqual(event["stock_pool"], server.WATCHLIST)
        self.assertEqual(event["stock_pool_hash"], weekly.sha256_json(server.WATCHLIST))
        server.WATCHLIST.append("999999")
        try:
            self.assertNotIn("999999", event["stock_pool"])
        finally:
            server.WATCHLIST.pop()

    def test_execution_evidence_strict_next_minute_or_unfilled(self):
        analysis = self.analysis(candidates=[self.candidate()])
        report = server.build_scheduled_report("tail-decision", analysis, server.empty_trading_state())
        evidence = weekly.execution_evidence(report, {
            "sz000001": {"records": [{"time": "1431", "price": 10.02, "volume": 2000}]},
        })
        self.assertIsNone(evidence[0]["raw_next_minute"].get("suspended"))
        checked = weekly.verify_archived_action(evidence[0])
        self.assertEqual(checked["status"], "reference_only")
        self.assertFalse(checked["tradability_verified"])
        trusted = weekly.execution_evidence(report, {
            "sz000001": {"records": [{"time": "1431", "price": 10.02, "volume": 2000, "suspended": False, "at_limit": False}]},
        })
        self.assertEqual(weekly.verify_archived_action(trusted[0])["status"], "filled")
        missing = weekly.verify_archived_action(weekly.execution_evidence(report, {})[0])
        self.assertEqual(missing["status"], "unfilled")
        self.assertIsNone(missing["filled_price"])

    def test_intraday_execution_evidence_uses_scan_decision_next_minute(self):
        candidate = {**self.candidate(), "last_time": "1000"}
        analysis = self.analysis(
            candidates=[candidate], universe=[{"code": "000001", "price": 10.0, "last_time": "1000"}],
        )
        analysis["market_data_time"] = "1001"
        report, _, _ = server.build_intraday_scan_report(
            analysis, server.empty_trading_state(), datetime(2026, 7, 29, 10, 0, tzinfo=server.SHANGHAI),
        )
        rows = {"sz000001": {"records": [{
            "time": "1001", "price": 10.02, "volume": 2000, "suspended": False, "at_limit": False,
        }]}}
        action = weekly.execution_evidence(report, rows)[0]
        self.assertEqual(action["expected_execution_minute"], "1001")
        self.assertEqual(weekly.verify_archived_action(action)["status"], "filled")
        missing = weekly.execution_evidence(report, {})[0]
        self.assertEqual(missing["expected_execution_minute"], "1001")
        self.assertEqual(weekly.verify_archived_action(missing)["status"], "unfilled")

    def test_production_archive_cannot_invent_thirteen_week_drawdown_history(self):
        with TemporaryDirectory() as directory:
            start = date(2026, 4, 6)
            for index in range(13):
                event = self.formal_event((start + timedelta(weeks=index)).strftime("%Y%m%d") + ":close-summary", drawdown=2.0)
                self.write_event(directory, event)
            buy = {"action_id": "drawdown-buy", "side": "buy", "code": "000001", "shares": 3000,
                   "assumed_price": 10, "status": "assumed_filled", "tradability_verified": True}
            current_day = start + timedelta(weeks=13)
            self.write_event(directory, self.formal_event(current_day.strftime("%Y%m%d") + ":tail-decision", [buy]))
            next_day = current_day + timedelta(days=1)
            self.write_event(directory, self.formal_event(next_day.strftime("%Y%m%d") + ":close-summary"))
            result = weekly.weekly_review(next_day.isoformat(), Path(directory), {
                "trading_calendar": {"dates": [current_day.isoformat(), next_day.isoformat()], "complete": True, "source": "offline fixture"},
                "benchmark_closes": {current_day.isoformat(): 4000, next_day.isoformat(): 4000},
                "mark_prices": {current_day.isoformat(): {"000001": 10}, next_day.isoformat(): {"000001": 8.0}},
            })
        self.assertFalse(result["adjustment"]["drawdown_triggered"])
        self.assertEqual(result["adjustment"]["candidates"], [])

    def test_cost_model_minimum_commission_tax_slippage_and_etf(self):
        stock_buy = weekly.order_cost("buy", "000001", 100, 10)
        stock_sell = weekly.order_cost("sell", "000001", 100, 10)
        etf_sell = weekly.order_cost("sell", "561360", 100, 10)
        self.assertEqual(stock_buy, {"commission": 5.0, "stamp_tax": 0.0, "slippage": 1.0})
        self.assertEqual(stock_sell, {"commission": 5.0, "stamp_tax": 0.5, "slippage": 1.0})
        self.assertEqual(etf_sell["stamp_tax"], 0.0)

    def test_archive_atomic_private_idempotent_and_conflict(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            event = self.formal_event("20260729:close-summary")
            weekly.archive_formal_event(path, event)
            weekly.archive_formal_event(path, event)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(weekly.CorruptLocalData):
                weekly.archive_formal_event(path, {**event, "stage": "tail-decision"})
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_archive_failure_prevents_commit_success_and_state_change(self):
        now = datetime(2026, 7, 29, 15, 30, tzinfo=server.SHANGHAI)
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            analysis = self.analysis()
            analysis["market_data_time"] = "1500"
            with patch("server.run_analysis", return_value=analysis):
                prepared = server.lightclaw_prepare("close-summary", now=now, state_path=state_path)
            before = state_path.read_bytes()
            with patch("server.archive_formal_event", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    server.lightclaw_commit(prepared["commit_token"], now=now, state_path=state_path)
            self.assertEqual(state_path.read_bytes(), before)

    def test_final_archive_marker_failure_recovers_with_same_permanent_token(self):
        now = datetime(2026, 7, 29, 15, 30, tzinfo=server.SHANGHAI)
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            analysis = self.analysis()
            analysis["market_data_time"] = "1500"
            with patch("server.run_analysis", return_value=analysis):
                prepared = server.lightclaw_prepare("close-summary", now=now, state_path=state_path)
            real_archive = server.archive_formal_event
            calls = 0

            def fail_final_marker(path, event):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("final marker failed")
                return real_archive(path, event)

            with patch("server.archive_formal_event", side_effect=fail_final_marker):
                with self.assertRaisesRegex(OSError, "final marker failed"):
                    server.lightclaw_commit(prepared["commit_token"], now=now, state_path=state_path)
            recovered = server.lightclaw_commit(prepared["commit_token"], now=now, state_path=state_path)
            event = json.loads(weekly.archive_path_for(state_path, prepared["event_id"]).read_text(encoding="utf-8"))
        self.assertEqual(recovered["status"], "already_committed")
        self.assertEqual(event["archive_status"], "committed")

    def test_state_write_failure_retries_same_pending_archive_payload(self):
        now = datetime(2026, 7, 29, 15, 30, tzinfo=server.SHANGHAI)
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            analysis = self.analysis()
            analysis["market_data_time"] = "1500"
            with patch("server.run_analysis", return_value=analysis):
                prepared = server.lightclaw_prepare("close-summary", now=now, state_path=state_path)
            real_save = server.save_trading_state
            calls = 0

            def fail_once(state, path=None):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("state failed")
                return real_save(state, path)

            with patch("server.save_trading_state", side_effect=fail_once):
                with self.assertRaisesRegex(OSError, "state failed"):
                    server.lightclaw_commit(prepared["commit_token"], now=now, state_path=state_path)
                archive_path = weekly.archive_path_for(state_path, prepared["event_id"])
                pending_bytes = archive_path.read_bytes()
                recovered = server.lightclaw_commit(
                    prepared["commit_token"], now=now + timedelta(minutes=5), state_path=state_path,
                )
            event = json.loads(archive_path.read_text(encoding="utf-8"))
        self.assertEqual(recovered["status"], "committed")
        self.assertEqual(event["archive_status"], "committed")
        pending = json.loads(pending_bytes)
        self.assertEqual(event["committed_at"], pending["committed_at"])
        self.assertEqual(event["content_hash"], pending["content_hash"])

    def test_test_message_commit_never_enters_formal_archive(self):
        now = datetime(2026, 7, 29, 15, 30, tzinfo=server.SHANGHAI)
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            analysis = self.analysis()
            analysis["market_data_time"] = "1500"
            with patch("server.run_analysis", return_value=analysis):
                prepared = server.lightclaw_prepare("close-summary", now=now, state_path=state_path, test_message=True)
            server.lightclaw_commit(prepared["commit_token"], now=now, state_path=state_path)
            self.assertFalse((Path(directory) / "formal-events").exists())

    def test_successful_commit_archives_formal_event_without_token_or_secret(self):
        now = datetime(2026, 7, 29, 15, 30, tzinfo=server.SHANGHAI)
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            analysis = self.analysis()
            analysis["market_data_time"] = "1500"
            with patch("server.run_analysis", return_value=analysis):
                prepared = server.lightclaw_prepare("close-summary", now=now, state_path=state_path)
            server.lightclaw_commit(prepared["commit_token"], now=now, state_path=state_path)
            path = weekly.archive_path_for(state_path, prepared["event_id"])
            event = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(event["archive_status"], "committed")
        self.assertNotIn("commit_token", path.read_text(encoding="utf-8") if path.exists() else json.dumps(event))
        self.assertNotIn(prepared["commit_token"], json.dumps(event))

    def test_weekly_insufficient_data_contract(self):
        with TemporaryDirectory() as directory:
            result = weekly.weekly_review("2026-08-22", Path(directory), {})
        self.assertEqual(result["status"], "insufficient_data")
        self.assertFalse(result["performance"]["available"])
        self.assertFalse(result["adjustment"]["triggered"])
        self.assertEqual(result["adjustment"]["candidates"], [])

    def test_partial_sell_and_multiple_buys_preserve_quantity(self):
        buy1 = {"action_id": "b1", "side": "buy", "code": "000001", "shares": 100,
                "assumed_price": 10, "status": "assumed_filled", "tradability_verified": True}
        buy2 = {"action_id": "b2", "side": "buy", "code": "000001", "shares": 100,
                "assumed_price": 12, "status": "assumed_filled", "tradability_verified": True}
        sell = {"action_id": "s1", "side": "sell", "code": "000001", "shares": 100,
                "assumed_price": 11, "status": "assumed_filled", "tradability_verified": True}
        with TemporaryDirectory() as directory:
            for day in ("20260817", "20260818"):
                for stage in ("morning-summary", "morning-decision", "tail-decision", "close-summary"):
                    actions = []
                    if day == "20260817" and stage == "morning-decision":
                        actions = [buy1, buy2]
                    if day == "20260818" and stage == "morning-decision":
                        actions = [sell]
                    self.write_event(directory, self.formal_event(f"{day}:{stage}", actions))
            verified = lambda day, price: {"price": price, "as_of": f"{day} 15:00", "verified_at_time": True}
            result = weekly.weekly_review("2026-08-22", Path(directory), {
                "trading_calendar": {"dates": ["2026-08-17", "2026-08-18"], "complete": True, "source": "offline fixture"},
                "benchmark_closes": {"2026-08-17": verified("2026-08-17", 4000), "2026-08-18": verified("2026-08-18", 4000)},
                "mark_prices": {"2026-08-17": {"000001": verified("2026-08-17", 11)}, "2026-08-18": {"000001": verified("2026-08-18", 11)}},
            })
        self.assertEqual(result["status"], "observational")
        self.assertTrue(result["performance"]["available"])
        self.assertEqual(result["adjustment"]["eligibility"]["filled_trades"], 1)

    def test_t_plus_one_same_day_rejected_cross_day_partial_sell_and_fee_conservation(self):
        buy_today = {"action_id": "today-buy", "side": "buy", "code": "000001", "shares": 100,
                     "assumed_price": 10, "status": "assumed_filled", "tradability_verified": True}
        sell_today = {"action_id": "today-sell", "side": "sell", "code": "000001", "shares": 200,
                      "assumed_price": 11, "status": "assumed_filled", "tradability_verified": True}
        sell_next = {**sell_today, "action_id": "next-sell"}
        with TemporaryDirectory() as directory:
            for day in ("20260817", "20260818"):
                for stage in ("morning-summary", "morning-decision", "tail-decision", "close-summary"):
                    actions = []
                    if day == "20260817" and stage == "morning-decision":
                        actions = [buy_today]
                    if stage == "tail-decision":
                        actions = [sell_today if day == "20260817" else sell_next]
                    self.write_event(directory, self.formal_event(f"{day}:{stage}", actions))
            verified = lambda day, price: {"price": price, "as_of": f"{day} 15:00", "verified_at_time": True}
            result = weekly.weekly_review("2026-08-22", Path(directory), {
                "trading_calendar": {"dates": ["2026-08-17", "2026-08-18"], "complete": True, "source": "fixture"},
                "benchmark_closes": {day: verified(day, 4000) for day in ("2026-08-17", "2026-08-18")},
                "mark_prices": {day: {"000001": verified(day, 11)} for day in ("2026-08-17", "2026-08-18")},
            })
        executions = {row["action_id"]: row for row in result["performance"]["executions"]}
        self.assertEqual(executions["today-sell"]["status"], "rejected")
        self.assertEqual(executions["next-sell"]["status"], "partially_filled")
        self.assertEqual(executions["next-sell"]["executed_shares"], 100)
        self.assertEqual(executions["next-sell"]["unfilled_shares"], 100)
        self.assertEqual(executions["next-sell"]["fees"], {"commission": 5.0, "stamp_tax": 0.55, "slippage": 1.1})
        self.assertEqual(result["performance"]["commission"], 10.0)
        self.assertEqual(result["performance"]["stamp_tax"], 0.55)
        self.assertEqual(result["performance"]["slippage"], 2.1)
        self.assertEqual(result["adjustment"]["eligibility"]["filled_trades"], 1)

    def test_archive_content_hash_and_forged_fill_are_rejected(self):
        with TemporaryDirectory() as directory:
            event = self.formal_event("20260817:tail-decision", [{
                "action_id": "forged", "side": "buy", "code": "000001", "shares": 100,
                "assumed_price": 10, "status": "assumed_filled", "tradability_verified": True,
            }])
            path = self.write_event(directory, event)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["actions"][0]["shares"] = 200
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(weekly.CorruptLocalData, "content hash"):
                weekly.weekly_review("2026-08-22", Path(directory), {})

    def test_archive_stock_pool_hash_mismatch_is_corrupt(self):
        with TemporaryDirectory() as directory:
            event = self.formal_event("20260817:close-summary")
            event["stock_pool_hash"] = "wrong"
            self.write_event(directory, event)
            with self.assertRaisesRegex(weekly.CorruptLocalData, "stock pool hash"):
                weekly.weekly_review("2026-08-22", Path(directory), {})

    def test_observational_when_fill_exists_but_ledger_incomplete(self):
        action = {"action_id": "a", "side": "buy", "code": "000001", "shares": 100,
                  "assumed_price": 10, "status": "assumed_filled", "tradability_verified": True}
        with TemporaryDirectory() as directory:
            self.write_event(directory, self.formal_event("20260818:tail-decision", [action]))
            result = weekly.weekly_review("2026-08-22", Path(directory), {
                "trading_calendar": {"dates": ["2026-08-17", "2026-08-18"], "complete": True, "source": "offline fixture"},
                "benchmark_closes": {"2026-08-17": 4000, "2026-08-18": 4010},
            })
        self.assertEqual(result["status"], "observational")
        self.assertFalse(result["performance"]["available"])
        self.assertTrue(result["data_quality"]["not_historical_unbiased_backtest"])

    def test_holiday_week_daily_marks_benchmark_and_drawdown(self):
        buy = {"action_id": "b", "side": "buy", "code": "000001", "shares": 100,
               "assumed_price": 10, "status": "assumed_filled", "tradability_verified": True}
        with TemporaryDirectory() as directory:
            for day in ("20261008", "20261009"):
                scan = self.formal_event(f"{day}:scan:1400:fixture", [buy] if day == "20261008" else [])
                scan["stage"] = "intraday-scan"
                scan["decision_at"] = f"{day[:4]}-{day[4:6]}-{day[6:]}T14:00:00+08:00"
                self.write_event(directory, scan)
                self.write_event(directory, self.formal_event(f"{day}:close-summary"))
            verified = lambda day, price: {"price": price, "as_of": f"{day} 15:00", "verified_at_time": True}
            config = {
                "trading_calendar": {"dates": ["2026-10-08", "2026-10-09"], "complete": True, "source": "offline fixture"},
                "benchmark_closes": {"2026-10-08": verified("2026-10-08", 4000), "2026-10-09": verified("2026-10-09", 4040)},
                "mark_prices": {"2026-10-08": {"000001": verified("2026-10-08", 10)}, "2026-10-09": {"000001": verified("2026-10-09", 9)}},
            }
            result = weekly.weekly_review("2026-10-10", Path(directory), config)
        self.assertEqual(result["status"], "observational")
        self.assertTrue(result["performance"]["available"])
        self.assertEqual(result["period"]["actual_trading_days"], ["2026-10-08", "2026-10-09"])
        self.assertEqual(len(result["performance"]["daily_equity"]), 2)
        self.assertGreater(result["performance"]["max_drawdown_pct"], 0)
        self.assertEqual(result["performance"]["benchmark_return_pct"], 1.0)
        self.assertTrue(result["data_quality"]["not_historical_unbiased_backtest"])

    def test_future_event_excluded_and_historical_pool_unchanged(self):
        with TemporaryDirectory() as directory:
            old = self.formal_event("20260821:close-summary", pool=["000001"])
            future = self.formal_event("20260824:close-summary", pool=list(server.WATCHLIST))
            self.write_event(directory, old)
            self.write_event(directory, future)
            result = weekly.weekly_review("2026-08-22", Path(directory), {})
            archived = json.loads(next(Path(directory).glob("20260821*.json")).read_text(encoding="utf-8"))
        self.assertEqual(result["data_quality"]["event_count"], 1)
        self.assertEqual(archived["stock_pool"], ["000001"])

    def make_threshold_archive(self, directory, trades):
        start = date(2026, 1, 5)
        remaining = trades
        for week in range(26):
            day = start + timedelta(weeks=week)
            count = min(3, remaining)
            remaining -= count
            buys = [{"action_id": f"{week}-{i}:buy:000001", "side": "buy", "code": "000001",
                     "shares": 100, "assumed_price": 10, "status": "assumed_filled",
                     "tradability_verified": True} for i in range(count)]
            sells = [{"action_id": f"{week}-{i}:sell:000001", "side": "sell", "code": "000001",
                      "shares": 100, "assumed_price": 10, "status": "assumed_filled",
                      "tradability_verified": True} for i in range(count)]
            for offset in (0, 1):
                trade_day = day + timedelta(days=offset)
                for stage in ("morning-summary", "morning-decision", "tail-decision", "close-summary"):
                    stage_actions = buys if offset == 0 and stage == "morning-decision" else (sells if offset == 1 and stage == "close-summary" else [])
                    self.write_event(directory, self.formal_event(f"{trade_day:%Y%m%d}:{stage}", stage_actions))

    def test_26_weeks_59_and_60_trade_threshold(self):
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            self.make_threshold_archive(first, 59)
            self.make_threshold_archive(second, 60)
            calendar = [
                (date(2026, 1, 5) + timedelta(weeks=i, days=offset)).isoformat()
                for i in range(26) for offset in (0, 1)
            ]
            base = {"trading_calendar": {"dates": calendar, "complete": True, "source": "offline fixture"}}
            result59 = weekly.weekly_review("2026-07-04", Path(first), base)
            result60 = weekly.weekly_review("2026-07-04", Path(second), base)
        self.assertFalse(result59["adjustment"]["eligible"])
        self.assertTrue(result60["adjustment"]["eligible"])
        self.assertEqual(result59["adjustment"]["candidates"], [])
        self.assertEqual(result60["status"], "observational")
        self.assertTrue(result60["adjustment"]["eligible"])
        self.assertEqual(result60["adjustment"]["candidates"], [])

    def counterfactual_samples(self):
        samples = []
        start = date(2026, 1, 5)
        for index in range(26):
            week = (start + timedelta(weeks=index)).isoformat()
            for security in ("000001", "000002", "000003"):
                samples.append({
                    "week": week, "parameters": {"minimum_reward_risk": 1.8},
                    "candidate_equity": [100, 101], "baseline_equity": [100, 100.5],
                    "trades": [{"security": security, "shares": 1}],
                })
        return samples

    def test_hash_verified_self_reported_counterfactual_results_are_ignored(self):
        with TemporaryDirectory() as directory:
            self.make_threshold_archive(directory, 60)
            path = Path(directory) / "20260105-morning-summary.json"
            event = json.loads(path.read_text(encoding="utf-8"))
            event["counterfactual_samples"] = self.counterfactual_samples()
            event["candidate_metrics"] = {"return": 999, "drawdown": 0, "trades": 999}
            weekly.atomic_write_json(path, weekly.seal_formal_event(event))
            calendar = [
                (date(2026, 1, 5) + timedelta(weeks=i, days=offset)).isoformat()
                for i in range(26) for offset in (0, 1)
            ]
            result = weekly.weekly_review("2026-07-04", Path(directory), {
                "trading_calendar": {"dates": calendar, "complete": True, "source": "fixture"},
            })
        self.assertTrue(result["adjustment"]["eligible"])
        self.assertEqual(result["adjustment"]["candidates"], [])
        self.assertEqual(result["adjustment"]["reason"], "counterfactual_inputs_unavailable")

    def test_legacy_candidate_arrays_holdout_concentration_and_arbitrary_grid_never_create_candidates(self):
        samples = self.counterfactual_samples()
        self.assertEqual(weekly._candidate_evaluations(samples, True), [])
        failed_holdout = [{**row, "candidate_equity": [100, 99]} for row in samples]
        self.assertEqual(weekly._candidate_evaluations(failed_holdout, True), [])
        concentrated = [{**row, "trades": [{"security": "000001", "shares": 10_000}]} for row in samples]
        self.assertEqual(weekly._candidate_evaluations(concentrated, True), [])
        arbitrary = [{**row, "parameters": {"minimum_reward_risk": 0.1}} for row in samples]
        self.assertEqual(weekly._candidate_evaluations(arbitrary, True), [])

    def test_future_archive_records_hashed_raw_counterfactual_inputs_but_marks_them_incomplete(self):
        analysis = self.analysis(universe=[{"code": "000001", "name": "历史名称", "price": 10}])
        report = server.build_scheduled_report("close-summary", analysis, server.empty_trading_state())
        pending = {"report": report, "prepared_at": analysis["generated_at"], "test_message": False}
        event = weekly.build_formal_event(pending, datetime(2026, 7, 29, 15, 31, tzinfo=server.SHANGHAI))
        inputs = event["counterfactual_inputs"]
        payload = {key: value for key, value in inputs.items() if key != "input_hash"}
        self.assertEqual(inputs["input_hash"], weekly.sha256_json(payload))
        self.assertFalse(inputs["complete"])
        self.assertEqual(inputs["reason"], "counterfactual_inputs_unavailable")
        self.assertEqual(inputs["stock_pool"], analysis["stock_pool"])
        server.WATCHLIST.append("999999")
        try:
            self.assertNotIn("999999", inputs["stock_pool"])
        finally:
            server.WATCHLIST.pop()

    def test_counterfactual_input_tamper_is_rejected_even_with_resealed_outer_archive(self):
        with TemporaryDirectory() as directory:
            event = self.formal_event("20260817:close-summary")
            inputs = {
                "schema": "astock.counterfactual-inputs/v1", "complete": False,
                "reason": "counterfactual_inputs_unavailable", "stock_pool": ["000001"],
            }
            event["counterfactual_inputs"] = {**inputs, "input_hash": weekly.sha256_json(inputs)}
            path = self.write_event(directory, event)
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["counterfactual_inputs"]["stock_pool"] = ["999999"]
            weekly.atomic_write_json(path, weekly.seal_formal_event(stored))
            with self.assertRaisesRegex(weekly.CorruptLocalData, "counterfactual input hash"):
                weekly.weekly_review("2026-08-22", Path(directory), {})

    def test_prior_week_replay_produces_opening_equity_for_closed_and_open_positions(self):
        verified = lambda day, price: {"price": price, "as_of": f"{day} 15:00", "verified_at_time": True}
        cases = ((True, {}), (False, {"000001": 100}))
        for close_prior, expected_holdings in cases:
            with self.subTest(close_prior=close_prior), TemporaryDirectory() as directory:
                buy = {"action_id": "prior-buy", "side": "buy", "code": "000001", "shares": 100,
                       "assumed_price": 10, "status": "assumed_filled", "tradability_verified": True}
                sell = {"action_id": "prior-sell", "side": "sell", "code": "000001", "shares": 100,
                        "assumed_price": 11, "status": "assumed_filled", "tradability_verified": True}
                self.write_event(directory, self.formal_event("20260813:morning-decision", [buy]))
                if close_prior:
                    self.write_event(directory, self.formal_event("20260814:tail-decision", [sell]))
                for stage in ("morning-summary", "morning-decision", "tail-decision", "close-summary"):
                    self.write_event(directory, self.formal_event(f"20260817:{stage}"))
                result = weekly.weekly_review("2026-08-22", Path(directory), {
                    "trading_calendar": {"dates": ["2026-08-17"], "complete": True, "source": "fixture"},
                    "benchmark_closes": {"2026-08-17": verified("2026-08-17", 4000)},
                    "mark_prices": {"2026-08-17": {"000001": verified("2026-08-17", 12)}},
                })
                self.assertEqual(result["performance"]["opening_holdings"], expected_holdings)
                self.assertIsNotNone(result["performance"]["week_opening_equity"])

    def test_sparse_or_unverified_calendar_cannot_create_complete_week(self):
        with TemporaryDirectory() as directory:
            for stage in ("morning-summary", "morning-decision", "tail-decision", "close-summary"):
                self.write_event(directory, self.formal_event(f"20260817:{stage}"))
            result = weekly.weekly_review("2026-08-22", Path(directory), {"trading_calendar": ["2026-08-17"]})
        self.assertEqual(result["adjustment"]["eligibility"]["complete_weeks"], 0)
        self.assertFalse(result["data_quality"]["complete"])

    def test_orphan_verified_sell_does_not_count_as_completed_trade(self):
        with TemporaryDirectory() as directory:
            action = {"action_id": "orphan:sell:000001", "side": "sell", "code": "000001", "shares": 100,
                      "assumed_price": 10, "status": "assumed_filled", "tradability_verified": True}
            for stage in ("morning-summary", "morning-decision", "tail-decision", "close-summary"):
                self.write_event(directory, self.formal_event(f"20260817:{stage}", [action] if stage == "close-summary" else []))
            result = weekly.weekly_review("2026-08-22", Path(directory), {
                "trading_calendar": {"dates": ["2026-08-17"], "complete": True, "source": "offline fixture"},
            })
        self.assertEqual(result["adjustment"]["eligibility"]["filled_trades"], 0)

    def test_untrusted_candidate_metrics_are_rejected_by_config_whitelist(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"candidate_evaluations": [{"parameters": {}}]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported keys"):
                weekly.load_config(config)

    def test_intraday_scan_archive_and_same_day_order(self):
        first = self.formal_event("20260826:scan:1000:aaa")
        first.update({"stage": "intraday-scan", "decision_at": "2026-08-26T10:00:00+08:00"})
        second = self.formal_event("20260826:scan:1300:bbb")
        second.update({"stage": "intraday-scan", "decision_at": "2026-08-26T13:00:00+08:00"})
        close = self.formal_event("20260826:close-summary")
        close["decision_at"] = "2026-08-26T15:30:00+08:00"
        with TemporaryDirectory() as directory:
            self.write_event(directory, second)
            self.write_event(directory, close)
            self.write_event(directory, first)
            events = weekly._load_events(Path(directory), date(2026, 8, 26))
        self.assertEqual([event["event_id"] for event in events], [first["event_id"], second["event_id"], close["event_id"]])

    def test_migration_daily_completeness_legacy_four_stage_and_scan_zero_or_many(self):
        self.assertTrue(weekly._day_is_complete("2026-08-25", set(weekly.LEGACY_REQUIRED_STAGES)))
        self.assertFalse(weekly._day_is_complete("2026-08-25", {"close-summary"}))
        self.assertTrue(weekly._day_is_complete("2026-08-26", {"close-summary"}))
        self.assertTrue(weekly._day_is_complete("2026-08-26", {"intraday-scan", "close-summary"}))
        self.assertFalse(weekly._day_is_complete("2026-08-26", {"intraday-scan"}))
        self.assertFalse(weekly._day_is_complete("2026-08-26", {"morning-summary", "close-summary"}))

    def test_post_migration_close_only_day_is_complete_without_no_signal_event(self):
        verified = lambda day, price: {"price": price, "as_of": f"{day} 15:00", "verified_at_time": True}
        with TemporaryDirectory() as directory:
            for day in ("20260826", "20260827"):
                self.write_event(directory, self.formal_event(day.replace("-", "") + ":close-summary"))
            result = weekly.weekly_review("2026-08-29", Path(directory), {
                "trading_calendar": {"dates": ["2026-08-26", "2026-08-27"], "complete": True, "source": "fixture"},
                "benchmark_closes": {day: verified(day, 4000) for day in ("2026-08-26", "2026-08-27")},
                "mark_prices": {"2026-08-26": {}, "2026-08-27": {}},
            })
        self.assertTrue(result["data_quality"]["complete"])
        self.assertEqual(result["data_quality"]["event_count"], 2)

    def test_same_day_multiple_scan_t_plus_one_quantity_and_fees(self):
        buy = {"action_id": "scan-buy", "side": "buy", "code": "000001", "shares": 100,
               "assumed_price": 10, "status": "assumed_filled", "tradability_verified": True}
        sell_same = {"action_id": "scan-sell-same", "side": "sell", "code": "000001", "shares": 100,
                     "assumed_price": 11, "status": "assumed_filled", "tradability_verified": True}
        sell_next = {**sell_same, "action_id": "scan-sell-next"}
        verified = lambda day, price: {"price": price, "as_of": f"{day} 15:00", "verified_at_time": True}
        with TemporaryDirectory() as directory:
            for event_id, decision_at, actions in (
                ("20260826:scan:1000:a", "2026-08-26T10:00:00+08:00", [buy]),
                ("20260826:scan:1400:b", "2026-08-26T14:00:00+08:00", [sell_same]),
                ("20260826:close-summary", "2026-08-26T15:30:00+08:00", []),
                ("20260827:scan:1000:c", "2026-08-27T10:00:00+08:00", [sell_next]),
                ("20260827:close-summary", "2026-08-27T15:30:00+08:00", []),
            ):
                event = self.formal_event(event_id, actions)
                event["stage"] = "intraday-scan" if ":scan:" in event_id else "close-summary"
                event["decision_at"] = decision_at
                self.write_event(directory, event)
            result = weekly.weekly_review("2026-08-29", Path(directory), {
                "trading_calendar": {"dates": ["2026-08-26", "2026-08-27"], "complete": True, "source": "fixture"},
                "benchmark_closes": {day: verified(day, 4000) for day in ("2026-08-26", "2026-08-27")},
                "mark_prices": {day: {"000001": verified(day, 11)} for day in ("2026-08-26", "2026-08-27")},
            })
        executions = {row["action_id"]: row for row in result["performance"]["executions"]}
        self.assertEqual(executions["scan-sell-same"]["status"], "rejected")
        self.assertEqual(executions["scan-sell-next"]["executed_shares"], 100)
        self.assertEqual(result["performance"]["commission"], 10.0)
        self.assertEqual(result["performance"]["stamp_tax"], 0.55)
        self.assertEqual(result["performance"]["slippage"], 2.1)

    def test_cli_json_exit_codes_read_only_offline_and_no_sensitive_env(self):
        with TemporaryDirectory() as directory:
            archive = Path(directory) / "archive"
            archive.mkdir()
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"archive_dir": str(archive)}), encoding="utf-8")
            state_path = Path(directory) / "state.json"
            state_path.write_text("unchanged", encoding="utf-8")
            output = io.StringIO()
            environ = {"WECOM_WEBHOOK": "sentinel", "RESOURCE_STATS_FEISHU_WEBHOOK": "sentinel"}
            with patch.object(sys, "argv", ["server.py", "--weekly-review", "--as-of", "2026-08-22",
                                                    "--weekly-config", str(config_path)]), \
                 patch.dict(os.environ, environ, clear=True), patch("sys.stdout", output), \
                 patch("urllib.request.urlopen") as network, patch("server.send_wecom_push") as sender:
                code = server.main()
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "insufficient_data")
            self.assertEqual(state_path.read_text(encoding="utf-8"), "unchanged")
            network.assert_not_called()
            sender.assert_not_called()

            bad = io.StringIO()
            with patch.object(sys, "argv", ["server.py", "--weekly-review", "--as-of", "bad"]), patch("sys.stdout", bad):
                self.assertEqual(server.main(), 1)
            alias = io.StringIO()
            with patch.object(sys, "argv", ["server.py", "--weekly-review", "--as-of", "2026-08-22", "--config", str(config_path)]), patch("sys.stdout", alias):
                self.assertEqual(server.main(), 0)
            conflict = io.StringIO()
            with patch.object(sys, "argv", ["server.py", "--weekly-review", "--weekly-config", str(config_path), "--config", str(config_path)]), patch("sys.stdout", conflict):
                self.assertEqual(server.main(), 1)
            self.assertIn("不得同时使用", json.loads(conflict.getvalue())["errors"][0])
            corrupt = archive / "bad.json"
            corrupt.write_text("{", encoding="utf-8")
            broken = io.StringIO()
            with patch.object(sys, "argv", ["server.py", "--weekly-review", "--as-of", "2026-08-22",
                                                    "--weekly-config", str(config_path)]), patch("sys.stdout", broken):
                self.assertEqual(server.main(), 2)


if __name__ == "__main__":
    unittest.main()
