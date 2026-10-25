"""IDP 监控的离线测试：不碰网络、不依赖容器。

运行： ./venv/Scripts/python -I idp/test_idp_offline.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import idp_monitor  # noqa: E402
from idp_monitor import IdpMonitor, build_push, is_available, seat_label  # noqa: E402

# 离线测试不读 /app/config.yml，也不往真实 data 目录写状态
idp_monitor.CONFIG_PATH = str(Path(__file__).resolve().parent / "nonexistent.yml")
idp_monitor.STATE_FILE = str(Path(__file__).resolve().parent / ".test_state.json")
idp_monitor.FOUND_FILE = str(Path(__file__).resolve().parent / ".test_found.json")
idp_monitor.log.setLevel("ERROR")


def row(**kw):
    base = {
        "ID": "x", "centerCnName": "测试考点", "examTimeOrigin": "2026-11-25",
        "time": "14:00 PM", "participantsCount": "35", "signUpCount": 10,
        "fullyBooked": "0", "seatAvailable": 1,
    }
    base.update(kw)
    return base


class TestAvailability(unittest.TestCase):
    """判定 = 官方未标记满 且 数值上还有余量。两个条件都有实测依据。"""

    def test_free(self):
        self.assertTrue(is_available(row(fullyBooked="0")))

    def test_full_string(self):
        self.assertFalse(is_available(row(fullyBooked="1")))

    def test_full_bool_and_int(self):
        for v in (True, 1, "true", "True"):
            self.assertFalse(is_available(row(fullyBooked=v)), v)

    def test_oversold_is_still_full(self):
        # 超售：signUpCount > participantsCount，但 fullyBooked=1 就是满
        self.assertFalse(is_available(row(signUpCount=40, participantsCount="35",
                                          fullyBooked="1")))

    def test_underfilled_but_marked_full(self):
        # 实测存在的组合：还有空位却已标记满（候补/锁位），必须判满
        self.assertFalse(is_available(row(signUpCount=20, participantsCount="35",
                                          fullyBooked="1")))

    def test_lagging_flag_with_no_room_is_not_available(self):
        # 实测 2026-11-29：涨到 35/35 但 fullyBooked 仍为 '0'，官方标记滞后
        self.assertFalse(is_available(row(signUpCount=35, participantsCount="35",
                                          fullyBooked="0")))

    def test_bad_capacity_falls_back_to_flag(self):
        self.assertTrue(is_available(row(signUpCount=1, participantsCount="n/a",
                                         fullyBooked="0")))


class TestLabel(unittest.TestCase):
    def test_normal(self):
        self.assertIn("余 25", seat_label(row(signUpCount=10, participantsCount="35")))

    def test_garbage_capacity(self):
        self.assertEqual(seat_label(row(signUpCount=10, participantsCount="n/a")), "10/n/a")


class TestPush(unittest.TestCase):
    def test_pushplus_ok_predicate(self):
        url, headers, payload, ok = build_push("pushplus", "tok", "t", "m")
        self.assertIn("pushplus.plus", url)
        self.assertEqual(payload["token"], "tok")
        self.assertTrue(ok({"code": 200}, 200))
        self.assertFalse(ok({"code": 905}, 200))

    def test_bark_is_get(self):
        _, _, payload, _ = build_push("bark", "mykey", "标题", "内容")
        self.assertIsNone(payload)


class TestBaselineAndDedupe(unittest.TestCase):
    """首轮只建基线不推送；只有 满->有位 的翻转才算新放位。"""

    def setUp(self):
        self.m = IdpMonitor()
        self.m.seen = {}
        self.pushed = []
        self.m.push = lambda t, b: self.pushed.append((t, b)) or True
        self.m._save_json = lambda *a, **k: None
        # 这里只关心去重，日期过滤用 all 模式排除干扰
        self.m.cfg["dates_mode"] = "all"
        self.m.cfg["dates"] = []

    def _poll(self, rows):
        import idp_monitor
        orig = idp_monitor.fetch_seats
        idp_monitor.fetch_seats = lambda cid: rows
        try:
            return self.m.poll_once()
        finally:
            idp_monitor.fetch_seats = orig

    def test_first_round_is_baseline_only(self):
        self.assertEqual(self._poll([row(fullyBooked="0")]), 0)
        self.assertEqual(self.pushed, [])

    def test_full_to_free_triggers(self):
        self._poll([row(fullyBooked="1")])
        self.assertEqual(self.pushed, [])
        self.assertEqual(self._poll([row(fullyBooked="0")]), 1)
        self.assertEqual(len(self.pushed), 1)
        self.assertIn("2026-11-25", self.pushed[0][1])

    def test_staying_free_does_not_repeat(self):
        self._poll([row(fullyBooked="1")])
        self._poll([row(fullyBooked="0")])
        self.assertEqual(self._poll([row(fullyBooked="0")]), 0)
        self.assertEqual(len(self.pushed), 1)

    def test_free_to_full_then_free_again_repushes(self):
        self._poll([row(fullyBooked="1")])
        self._poll([row(fullyBooked="0")])
        self._poll([row(fullyBooked="1")])
        self.assertEqual(self._poll([row(fullyBooked="0")]), 1)
        self.assertEqual(len(self.pushed), 2)


class TestFilters(unittest.TestCase):
    def setUp(self):
        self.m = IdpMonitor()
        self.m.seen = {}
        self.pushed = []
        self.m.push = lambda t, b: self.pushed.append((t, b)) or True
        self.m._save_json = lambda *a, **k: None
        # 只测 times 过滤时，日期维度用 all 排除干扰
        self.m.cfg["dates_mode"] = "all"
        self.m.cfg["dates"] = []

    def test_date_filter(self):
        self.m.cfg["dates_mode"] = "list"
        self.m.cfg["dates"] = ["2026-12-01"]
        import idp_monitor
        orig = idp_monitor.fetch_seats
        idp_monitor.fetch_seats = lambda cid: [row(examTimeOrigin="2026-11-25"),
                                               row(ID="y", examTimeOrigin="2026-12-01")]
        try:
            self.m.poll_once()
        finally:
            idp_monitor.fetch_seats = orig
        self.assertEqual(self.m.status["sessions_total"], 1)

    def test_time_filter(self):
        self.m.cfg["times"] = ["09:00 AM"]
        import idp_monitor
        orig = idp_monitor.fetch_seats
        idp_monitor.fetch_seats = lambda cid: [row(time="02:00 PM"),
                                               row(ID="y", time="09:00 AM")]
        try:
            self.m.poll_once()
        finally:
            idp_monitor.fetch_seats = orig
        self.assertEqual(self.m.status["sessions_total"], 1)


class TestNightWindow(unittest.TestCase):
    def setUp(self):
        self.m = IdpMonitor()

    def _at(self, hhmm):
        from datetime import datetime
        return datetime.strptime(hhmm, "%H:%M")

    def test_inside_night(self):
        self.assertTrue(self.m._in_night(self._at("03:00")))

    def test_boundary_inclusive_start(self):
        self.assertTrue(self.m._in_night(self._at("01:00")))

    def test_boundary_exclusive_end(self):
        self.assertFalse(self.m._in_night(self._at("07:00")))

    def test_daytime(self):
        self.assertFalse(self.m._in_night(self._at("14:00")))

    def test_interval_switches(self):
        _, mode = self.m.current_interval(self._at("03:00"))
        self.assertEqual(mode, "night")
        _, mode = self.m.current_interval(self._at("14:00"))
        self.assertEqual(mode, "normal")

    def test_night_disabled(self):
        self.m.cfg["night_pause"]["enabled"] = False
        self.assertFalse(self.m._in_night(self._at("03:00")))


class TestPrune(unittest.TestCase):
    def test_drops_past_dates(self):
        from datetime import datetime
        m = IdpMonitor()
        m.seen = {"a": {"date": "2020-01-01"}, "b": {"date": "2099-01-01"}}
        m._prune_seen(datetime(2026, 10, 25))
        self.assertEqual(list(m.seen), ["b"])


class TestConfigValidation(unittest.TestCase):
    """面板保存走的就是这条路径，非法值必须拒绝而不是静默纠正。"""

    def setUp(self):
        self.m = IdpMonitor()
        self.base = self.m.cfg

    def _v(self, body):
        import idp_monitor
        return idp_monitor.validate_config_payload(self.base, body)

    def test_interval_clamped(self):
        self.assertEqual(self._v({"interval_seconds": 1})["interval_seconds"], 5)
        self.assertEqual(self._v({"interval_seconds": 9999})["interval_seconds"], 600)

    def test_bad_date_rejected(self):
        with self.assertRaises(ValueError):
            self._v({"dates": ["2026/11/25"]})

    def test_empty_dates_means_all(self):
        self.assertEqual(self._v({"dates": []})["dates"], [])

    def test_bad_time_rejected(self):
        with self.assertRaises(ValueError):
            self._v({"times": ["下午两点"]})

    def test_empty_times_rejected(self):
        with self.assertRaises(ValueError):
            self._v({"times": []})

    def test_valid_time_accepted(self):
        self.assertEqual(self._v({"times": ["14:00 PM"]})["times"], ["14:00 PM"])

    def test_unknown_push_provider_rejected(self):
        with self.assertRaises(ValueError):
            self._v({"push": {"provider": "telegram"}})

    def test_bad_heartbeat_rejected(self):
        with self.assertRaises(ValueError):
            self._v({"push": {"heartbeat_time": "9点"}})

    def test_token_blank_keeps_existing(self):
        # 面板留空 = 不修改，不能被空串覆盖掉已配置的 token
        self.base["push"]["token"] = "keepme"
        cfg = self._v({"push": {"token": ""}})
        self.assertEqual(cfg["push"]["token"], "keepme")

    def test_sanitize_hides_token(self):
        import idp_monitor
        self.base["push"]["token"] = "secret"
        out = idp_monitor.sanitize(self.base)
        self.assertEqual(out["push"]["token"], "")
        self.assertTrue(out["push"]["token_set"])


class TestPruneExpired(unittest.TestCase):
    """时间只会向前走：过期考期必须自动作废。"""

    def setUp(self):
        self.m = IdpMonitor()
        self.pushed = []
        self.m.push = lambda t, b: self.pushed.append((t, b)) or True
        self.m._write_cfg = lambda cfg: None

    def test_past_dates_dropped(self):
        self.m.cfg["dates_mode"] = "list"
        self.m.cfg["dates"] = ["2026-10-20", "2026-10-24", "2026-11-05"]
        dropped = self.m.prune_expired("测试")
        self.assertEqual(dropped, ["2026-10-20", "2026-10-24"])
        self.assertEqual(self.m.cfg["dates"], ["2026-11-05"])

    def test_today_also_dropped(self):
        # 考试日过了当天就没意义了
        self.m.cfg["dates_mode"] = "list"
        today = idp_monitor.today_cn()
        self.m.cfg["dates"] = [today, "2099-01-01"]
        dropped = self.m.prune_expired("测试")
        self.assertEqual(dropped, [today])
        self.assertEqual(self.m.cfg["dates"], ["2099-01-01"])

    def test_all_mode_unaffected(self):
        # 盯全部时配置里的日期列表本就不参与过滤，不能被作废改动
        self.m.cfg["dates_mode"] = "all"
        self.m.cfg["dates"] = ["2026-10-20"]
        self.assertEqual(self.m.prune_expired("测试"), [])
        self.assertEqual(self.m.cfg["dates"], ["2026-10-20"])

    def test_all_dates_expired_notifies_user(self):
        # 范围归零必须通知，否则监控看起来还活着实际已经瞎了
        self.m.cfg["dates_mode"] = "list"
        self.m.cfg["dates"] = ["2020-01-01"]
        self.m.prune_expired("测试")
        self.assertEqual(len(self.pushed), 1)
        self.assertIn("清空", self.pushed[0][0])

    def test_not_empty_no_notification(self):
        self.m.cfg["dates_mode"] = "list"
        self.m.cfg["dates"] = ["2020-01-01", "2099-01-01"]
        self.m.prune_expired("测试")
        self.assertEqual(self.pushed, [])


class TestPruneSchedule(unittest.TestCase):
    """触发时机：启动即触发 + 每天 00:00/09:00。"""

    DAY = "2026-10-25"

    def setUp(self):
        self.m = IdpMonitor()
        self.m._pruned_at_9 = False

    def _at(self, hhmm):
        from datetime import datetime
        return datetime.strptime(f"{self.DAY} {hhmm}", "%Y-%m-%d %H:%M")

    def test_first_call_always_prunes(self):
        # 启动时 last_prune_day=None → 必触发
        self.assertTrue(self.m._should_prune_now(self._at("00:05"), None))

    def test_same_day_early_does_not_repeat(self):
        self.assertFalse(self.m._should_prune_now(self._at("00:05"), self.DAY))

    def test_same_day_before_nine_does_not_repeat(self):
        self.assertFalse(self.m._should_prune_now(self._at("08:30"), self.DAY))

    def test_nine_am_triggers_once(self):
        self.assertTrue(self.m._should_prune_now(self._at("09:00"), self.DAY))
        self.m._pruned_at_9 = True
        self.assertFalse(self.m._should_prune_now(self._at("09:30"), self.DAY))

    def test_new_day_triggers(self):
        self.assertTrue(self.m._should_prune_now(self._at("00:10"), "2026-10-24"))


class TestDatesMode(unittest.TestCase):
    """dates_mode 必须显式，不能由 dates 是否为空推断——
    否则"清空指定日期"会被误读成切换模式。"""

    def setUp(self):
        self.m = IdpMonitor()

    def _v(self, body):
        return idp_monitor.validate_config_payload(self.m.cfg, body)

    def test_picking_dates_sets_list_mode(self):
        cfg = self._v({"dates_mode": "list", "dates": ["2026-11-25"]})
        self.assertEqual(cfg["dates_mode"], "list")

    def test_clearing_dates_keeps_list_mode(self):
        # 清空选择不等于退出 list 模式；要切换得显式传 dates_mode
        self.m.cfg["dates_mode"] = "list"
        cfg = self._v({"dates": []})
        self.assertEqual(cfg["dates_mode"], "list")

    def test_expired_date_rejected(self):
        with self.assertRaises(ValueError):
            self._v({"dates": ["2020-01-01"]})

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            self._v({"dates_mode": "nope"})

    def test_filter_respects_mode(self):
        import idp_monitor
        rows = [row(ID="a", examTimeOrigin="2026-11-25"),
                row(ID="b", examTimeOrigin="2026-12-01")]
        orig = idp_monitor.fetch_seats
        idp_monitor.fetch_seats = lambda cid: rows
        try:
            self.m.seen = {}
            self.m.cfg["dates_mode"] = "list"
            self.m.cfg["dates"] = ["2026-11-25"]
            self.m.poll_once()
            self.assertEqual(self.m.status["sessions_total"], 1)

            self.m.seen = {}
            self.m.cfg["dates_mode"] = "all"
            self.m.cfg["dates"] = []
            self.m.poll_once()
            self.assertEqual(self.m.status["sessions_total"], 2)
        finally:
            idp_monitor.fetch_seats = orig


class TestRollingWindow(unittest.TestCase):
    """窗口跟着今天往前滚，结构上不可能过期——这就是它优于固定日期列表的原因。"""

    def test_window_includes_today(self):
        lo, hi = idp_monitor.rolling_window("2026-10-25", 21)
        self.assertEqual(lo, "2026-10-25")
        self.assertEqual(hi, "2026-11-15")

    def test_window_advances_with_today(self):
        # 同一天往后挪一天，整个窗口跟着挪
        _, hi1 = idp_monitor.rolling_window("2026-10-25", 21)
        lo2, hi2 = idp_monitor.rolling_window("2026-10-26", 21)
        self.assertEqual(lo2, "2026-10-26")
        self.assertEqual(hi2, "2026-11-16")
        self.assertGreater(lo2, "2026-10-25")
        self.assertGreater(hi2, hi1)

    def test_crosses_month_boundary(self):
        _, hi = idp_monitor.rolling_window("2026-11-25", 21)
        self.assertEqual(hi, "2026-12-16")

    def test_filter_uses_window_not_stored_dates(self):
        m = IdpMonitor()
        m.seen = {}
        m.cfg["dates_mode"] = "rolling"
        m.cfg["rolling_days"] = 21
        m.cfg["dates"] = []          # 配置里没有写死任何日期
        rows = [row(ID="a", examTimeOrigin="2026-01-01"),   # 窗口外
                row(ID="b", examTimeOrigin="2099-01-01")]   # 窗口外
        orig = idp_monitor.fetch_seats
        idp_monitor.fetch_seats = lambda cid: rows
        try:
            m.poll_once()
            self.assertEqual(m.status["sessions_total"], 0)
        finally:
            idp_monitor.fetch_seats = orig

    def test_rolling_mode_never_prunes(self):
        # 滚动模式没有"过期"概念，prune 必须是空操作
        m = IdpMonitor()
        m.cfg["dates_mode"] = "rolling"
        m.cfg["dates"] = ["2026-10-20"]
        self.assertEqual(m.prune_expired("测试"), [])
        self.assertEqual(m.cfg["dates"], ["2026-10-20"])

    def test_rolling_days_clamped(self):
        m = IdpMonitor()
        cfg = idp_monitor.validate_config_payload(m.cfg, {"rolling_days": 9999})
        self.assertEqual(cfg["rolling_days"], 90)
        cfg = idp_monitor.validate_config_payload(m.cfg, {"rolling_days": 1})
        self.assertEqual(cfg["rolling_days"], 7)

    def test_invalid_mode_rejected(self):
        m = IdpMonitor()
        with self.assertRaises(ValueError):
            idp_monitor.validate_config_payload(m.cfg, {"dates_mode": "weekly"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
