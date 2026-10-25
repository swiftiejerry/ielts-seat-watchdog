# -*- coding: utf-8 -*-
"""离线单元测试：不依赖网络与账号，验证监控核心逻辑。

模拟响应与 ielts.neea.cn queryTestSeats 真实结构同构
（字段来源：ielts.cn_hkj ielts.js 的 fetchSeats 响应映射）。
"""
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "monitor"))
import ielts_monitor
from ielts_monitor import (
    expand_dates, in_night_pause, parse_available_seats, build_push,
    load_config, deep_merge, in_surge_window, effective_interval, DEFAULTS,
)

PASS = 0
FAIL = 0

def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")

# 与真实响应同构：testDay + testSeats[].testTime + testSeat[].{seatId,centerName,seatStatus,optStatus}
MOCK = {
    "cityCode": "440300", "cityName": "深圳", "testDay": "2026-11-25",
    "rescheduleAndCancelDeadline": "2026-11-11",
    "testSeats": [
        {"testTime": "上午 09:00", "testSeat": [
            {"seatId": 111, "centerCode": "C1", "centerName": "深圳赛格人才培训中心",
             "seatStatus": 1, "optStatus": 1},
            {"seatId": 112, "centerCode": "C1", "centerName": "深圳赛格人才培训中心",
             "seatStatus": 1, "optStatus": 0},   # 不可报
        ]},
        {"testTime": "下午 14:00", "testSeat": [
            {"seatId": 221, "centerCode": "C2", "centerName": "深圳大学师范学院",
             "seatStatus": 1, "optStatus": 1},
        ]},
        {"testTime": "上午 09:00", "testSeat": [
            {"seatId": 113, "centerCode": "C2", "centerName": "深圳大学师范学院",
             "seatStatus": 0, "optStatus": 1},   # 无位
        ]},
    ],
}

print("== 1. 考位解析与过滤（真实结构） ==")
all_seats = parse_available_seats(MOCK, [], [])
check("无过滤时只取到可报考位（3个中2个）", len(all_seats) == 2)
check("seatStatus/optStatus 双条件生效", {s["seatId"] for s in all_seats} == {111, 221})

am = parse_available_seats(MOCK, [], ["上午"])
check("场次过滤[上午]: 只剩赛格111", [s["seatId"] for s in am] == [111])

pm = parse_available_seats(MOCK, [], ["下午", "14:00"])
check("场次过滤[下午,14:00]: 深大221", [s["seatId"] for s in pm] == [221])

seg = parse_available_seats(MOCK, ["赛格"], [])
check("考点过滤[赛格]: 111", [s["seatId"] for s in seg] == [111])

seg_am = parse_available_seats(MOCK, ["赛格"], ["上午"])
check("考点+场次组合: 111", [s["seatId"] for s in seg_am] == [111])

both = parse_available_seats(MOCK, ["不存在考点"], [])
check("考点不命中 → 空", both == [])
check("响应为 true/None 等非dict → 空", parse_available_seats(True, [], []) == [])

print("== 2. 日期展开 ==")
cfg = {"monitor": {
    "dates": ["2026-11-25", "2026-11-26", "2026-11-25"],
    "date_range": {"start": "2026-12-01", "end": "2026-12-03"},
}}
today = datetime(2026, 11, 24).date()
got = expand_dates(cfg, today)
check("dates去重 + range展开并集(3+3=6天)",
      got == ["2026-11-25", "2026-11-26", "2026-12-01", "2026-12-02", "2026-12-03"])
got2 = expand_dates(cfg, datetime(2026, 12, 2).date())
check("过期日期自动剔除（12-02起只见3天）",
      got2 == ["2026-12-02", "2026-12-03"])
check("全空配置 → 空", expand_dates({"monitor": {}}, today) == [])

print("== 3. 夜间降频窗口 ==")
np_cfg = {"monitor": {"night_pause": {"enabled": True, "from": "01:00", "to": "07:00"}}}
check("03:00 在窗口内", in_night_pause(np_cfg, datetime(2026, 11, 25, 3, 0)))
check("08:00 不在窗口", not in_night_pause(np_cfg, datetime(2026, 11, 25, 8, 0)))
check("00:30 不在窗口", not in_night_pause(np_cfg, datetime(2026, 11, 25, 0, 30)))
cross = {"monitor": {"night_pause": {"enabled": True, "from": "23:30", "to": "06:00"}}}
check("跨午夜窗口 23:45 命中", in_night_pause(cross, datetime(2026, 11, 25, 23, 45)))
check("跨午夜窗口 02:00 命中", in_night_pause(cross, datetime(2026, 11, 25, 2, 0)))
check("跨午夜窗口 12:00 不命中", not in_night_pause(cross, datetime(2026, 11, 25, 12, 0)))
off = {"monitor": {"night_pause": {"enabled": False}}}
check("开关关闭恒不命中", not in_night_pause(off, datetime(2026, 11, 25, 3, 0)))

print("== 4. 推送构造（与油猴 v2.2.5 同协议） ==")
url, headers, payload, ok = build_push("pushplus", "TOK", "标题", "内容")
check("pushplus 端点", url == "https://www.pushplus.plus/send")
check("pushplus 成功判定", ok({"code": 200}, 200) and not ok({"code": 400}, 200))
check("pushplus token 传递", payload["token"] == "TOK" and payload["title"] == "标题")

url2, _, pay2, ok2 = build_push("serverchan", "SKEY", "t", "m")
check("serverchan URL 拼接", url2 == "https://sctapi.ftqq.com/SKEY.send")
check("serverchan 成功判定", ok2({"code": 0}, 200) and not ok2({"code": 40001}, 200))

url3, _, _, ok3 = build_push("bark", "bkey", "t", "m")
check("bark 纯key自动拼 api.day.app", url3.startswith("https://api.day.app/bkey/"))
check("bark 成功判定", ok3(None, 200) and not ok3(None, 500))

url4, _, _, ok4 = build_push("wecom-hook", "K", "t", "m")
check("企微 webhook 成功判定", ok4({"errcode": 0}, 200) and not ok4({"errcode": 93000}, 200))

print("== 5. 配置默认值合并 ==")
merged = deep_merge({"neea": {"user_id": "", "password": ""}}, {"neea": {"user_id": "55532186"}})
check("覆盖保留共存", merged["neea"] == {"user_id": "55532186", "password": ""})
merged2 = deep_merge({"monitor": {"interval_seconds": 60}}, {"monitor": {"interval_seconds": 30}})
check("标量覆盖", merged2["monitor"]["interval_seconds"] == 30)

print("== 6. 高峰窗口策略（默认窗口 09:55-10:40 / 19:55-20:40） ==")
D = lambda h, m: datetime(2026, 10, 25, h, m)
CFG = deep_merge(DEFAULTS, {"monitor": {}})   # 生产配置必经 load_config 合并默认值
check("10:10 在高峰窗口", in_surge_window(CFG, D(10, 10)))
check("20:30 在晚高峰窗口", in_surge_window(CFG, D(20, 30)))
check("09:55 边界含起点", in_surge_window(CFG, D(9, 55)))
check("10:40 边界不含终点", not in_surge_window(CFG, D(10, 40)))
check("19:54 不在窗口", not in_surge_window(CFG, D(19, 54)))
check("surge 关闭恒不在", not in_surge_window({"monitor": {"surge": {"enabled": False}}}, D(10, 10)))
cross = {"monitor": {"surge": {"enabled": True, "windows": [["23:50", "00:10"]]}}}
check("跨午夜 23:55 命中", in_surge_window(cross, D(23, 55)))
check("跨午夜 00:05 命中", in_surge_window(cross, D(0, 5)))
check("跨午夜 12:00 不命中", not in_surge_window(cross, D(12, 0)))
bad = {"monitor": {"surge": {"enabled": True, "windows": [["09:00"], "bad", ["08:00", "11:00"]]}}}
check("畸形窗口跳过后仍可用", in_surge_window(bad, D(8, 30)))

iv, mode = effective_interval(CFG, D(10, 10))
check("高峰时段 8s/surge", (iv, mode) == (8, "surge"))
iv, mode = effective_interval(CFG, D(14, 0))
check("平峰时段 300s/offpeak", (iv, mode) == (300, "offpeak"))
iv, mode = effective_interval(CFG, D(2, 0))
check("夜间 600s/night", (iv, mode) == (600, "night"))
iv, mode = effective_interval(deep_merge(CFG, {"monitor": {"surge": {"enabled": False}}}), D(14, 0))
check("surge 关闭回落 60s/normal", (iv, mode) == (60, "normal"))
iv, mode = effective_interval(deep_merge(CFG, {"monitor": {"surge": {"interval_seconds": 45, "offpeak_interval_seconds": 240}}}), D(19, 56))
check("自定义间隔生效", (iv, mode) == (45, "surge"))
iv, mode = effective_interval(deep_merge(CFG, {"monitor": {"surge": {"windows": [["01:30", "03:00"]]}}}), D(2, 0))
check("高峰优先于夜间", mode == "surge")

# ---- 抖动必须小于间隔，否则间隔会被整个吞掉 ----
# 注意 ControlPlane 构造时会读状态文件，这里指向不存在的路径走默认值
_orig_state = ielts_monitor.SEEN_FILE
ielts_monitor.SEEN_FILE = os.path.join(os.path.dirname(__file__), ".nonexistent.json")

cp = ielts_monitor.ControlPlane(deep_merge(CFG, {"monitor": {"jitter_seconds": 30}}))
_orig_now = ielts_monitor.datetime


def _fake_at(hour, minute):
    class _FakeDT:
        @staticmethod
        def now(tz=None):
            return _orig_now(2026, 10, 25, hour, minute, 0, tzinfo=tz)
    return _FakeDT


# 高峰时刻（10:10 落在 09:55-10:40 窗口内）
ielts_monitor.datetime = _fake_at(10, 10)
try:
    task = cp.worker_config()
finally:
    ielts_monitor.datetime = _orig_now
check("surge 间隔下发 8s", task["interval_ms"] == 8000)
check("surge 抖动被钳到间隔 1/4", task["jitter_ms"] == 2000)

# 平峰时刻（14:00 不在任何窗口内）抖动保持原样
ielts_monitor.datetime = _fake_at(14, 0)
try:
    task2 = cp.worker_config()
finally:
    ielts_monitor.datetime = _orig_now
ielts_monitor.SEEN_FILE = _orig_state
check("平峰间隔下发 300s", task2["interval_ms"] == 300000)
check("平峰抖动保持 30s", task2["jitter_ms"] == 30000)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
