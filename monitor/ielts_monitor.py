"""NEEA 雅思考位监控 · 控制面容器 + Web 配置面板（混合架构）。

ielts.neea.cn 对非浏览器客户端一律返回 412，自动化浏览器内的接口调用
同样无法稳定工作；唯一可靠的数据通道是用户真实 Chrome 里的油猴脚本。
因此本容器只做控制面：配置存储 / Web 面板 / 筛选 / 去重 / 微信推送 /
心跳 / 状态聚合；数据面由油猴脚本（工作模式）轮询本容器领取任务并回传。

风控对策：轮询节奏由容器统一下发（默认 60s±30s 抖动，下限 30s）；
登录只发生在用户真实浏览器的脚本里；容器不持有 NEEA 凭据亦可运行。
"""
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta

import yaml
from flask import Flask, jsonify, request

CONFIG_PATH = os.environ.get("MONITOR_CONFIG", "/app/config.yml")
DATA_DIR = os.environ.get("MONITOR_DATA", "/data")
UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
FOUND_FILE = os.path.join(DATA_DIR, "found.json")
SCAN_CACHE_FILE = os.path.join(DATA_DIR, "scan_cache.json")

CITY_MAP = {
    "110100": "北京", "120100": "天津", "130100": "石家庄", "140100": "太原",
    "210100": "沈阳", "210200": "大连", "220100": "长春", "230100": "哈尔滨",
    "310100": "上海", "320100": "南京", "320200": "无锡", "320500": "苏州",
    "330100": "杭州", "330200": "宁波", "340100": "合肥", "350100": "福州",
    "350200": "厦门", "360100": "南昌", "370100": "济南", "370200": "青岛",
    "410100": "郑州", "420100": "武汉", "430100": "长沙", "440100": "广州",
    "440300": "深圳", "440600": "佛山", "450100": "南宁", "460100": "海口",
    "500100": "重庆", "510100": "成都", "520100": "贵阳", "530100": "昆明",
    "610100": "西安", "620100": "兰州", "650100": "乌鲁木齐",
}
PUSH_PROVIDERS = {"pushplus", "serverchan", "bark", "wecom-hook", "dingtalk", "feishu", "custom"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("ielts-monitor")

try:
    from zoneinfo import ZoneInfo
    TZ_CN = ZoneInfo("Asia/Shanghai")   # 夜间窗口按北京时间判断，与容器时区无关
except Exception:                       # 找不到 IANA 时区库时退回系统本地时钟
    TZ_CN = None


class RingHandler(logging.Handler):
    """内存日志环，供 UI 展示最近日志。"""
    def __init__(self, capacity=300):
        super().__init__()
        self.buf = []
        self.cap = capacity

    def emit(self, record):
        self.buf.append(f"{record.asctime} [{record.levelname}] {record.getMessage()}")
        if len(self.buf) > self.cap:
            self.buf = self.buf[-self.cap:]


ring = RingHandler()
ring.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                    datefmt="%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(ring)

# ---------------- 纯函数区（离线可测） ----------------

DEFAULTS = {
    "neea": {"user_id": "", "password": ""},
    "monitor": {
        "enabled": True,
        "product_code": "IELTSCDI",
        "cities": ["440300"],
        "dates": [],
        "date_range": {"start": "", "end": ""},
        "center_keywords": [],
        "session_keywords": [],
        "interval_seconds": 60,
        "jitter_seconds": 30,
        "night_pause": {"enabled": True, "from": "01:00", "to": "07:00", "interval_seconds": 600},
    },
    "push": {"provider": "pushplus", "token": "", "heartbeat_time": "09:00"},
    "ocr": {"url": "http://ielts-ocr:9898"},
    "scan": {"request_days": 0},  # >0 表示面板请求扫描 N 天，worker 领取后清零
}


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return deep_merge(DEFAULTS, raw)


def expand_dates(cfg, today=None):
    from datetime import date as date_cls
    today = today or datetime.now().date()
    days = set()
    for d in cfg["monitor"].get("dates") or []:
        days.add(str(d).strip())
    rng = cfg["monitor"].get("date_range") or {}
    start, end = str(rng.get("start") or ""), str(rng.get("end") or "")
    if start and end:
        try:
            cur = datetime.strptime(start, "%Y-%m-%d").date()
            last = datetime.strptime(end, "%Y-%m-%d").date()
            while cur <= last:
                days.add(cur.isoformat())
                cur += timedelta(days=1)
        except ValueError:
            log.warning("date_range 格式错误，期望 YYYY-MM-DD: %s~%s", start, end)
    return sorted(d for d in days if d >= today.isoformat())


def in_night_pause(cfg, now=None):
    np = cfg["monitor"].get("night_pause") or {}
    if not np.get("enabled"):
        return False
    now = now or datetime.now()
    t = now.strftime("%H:%M")
    frm, to = np.get("from", "01:00"), np.get("to", "07:00")
    if frm <= to:
        return frm <= t < to
    return t >= frm or t < to


def seat_available(seat):
    return seat.get("seatStatus") == 1 and seat.get("optStatus") == 1


def center_matches(center_name, keywords):
    if not keywords:
        return True
    return any(kw in (center_name or "") for kw in keywords)


def session_matches(test_time, keywords):
    if not keywords:
        return True
    return any(kw in (test_time or "") for kw in keywords)


def parse_available_seats(response, center_keywords, session_keywords):
    """worker 上报的原始 queryTestSeats 响应 → 过滤后的考位列表。"""
    if not isinstance(response, dict):
        return []
    test_day = response.get("testDay") or ""
    found = []
    for slot in response.get("testSeats") or []:
        test_time = slot.get("testTime") or ""
        if not session_matches(test_time, session_keywords):
            continue
        for seat in slot.get("testSeat") or []:
            if not seat_available(seat):
                continue
            if not center_matches(seat.get("centerName"), center_keywords):
                continue
            found.append({
                "testDay": test_day, "testTime": test_time,
                "seatId": seat.get("seatId"), "centerName": seat.get("centerName"),
                "centerCode": seat.get("centerCode"),
            })
    return found


def build_push(provider, token, title, message):
    """返回 (url, headers, payload, ok_pred)；与油猴脚本同协议。"""
    provider = provider or "pushplus"
    token = (token or "").strip()
    if provider == "pushplus":
        return ("https://www.pushplus.plus/send", {"Content-Type": "application/json"},
                {"token": token, "title": title, "content": message, "template": "txt"},
                lambda j, s: j.get("code") == 200)
    if provider == "serverchan":
        return (f"https://sctapi.ftqq.com/{token}.send",
                {"Content-Type": "application/x-www-form-urlencoded"},
                {"title": title, "desp": message}, lambda j, s: j.get("code") == 0)
    if provider == "bark":
        base = token if token.startswith("http") else f"https://api.day.app/{token}"
        return (f"{base}/{title}/{message}?sound=alarm".replace(" ", "%20"), {}, None,
                lambda j, s: s == 200)
    if provider == "wecom-hook":
        return (f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={token}",
                {"Content-Type": "application/json"},
                {"msgtype": "text", "text": {"content": f"{title}\n{message}"}},
                lambda j, s: j.get("errcode") == 0)
    if provider == "dingtalk":
        return (f"https://oapi.dingtalk.com/robot/send?access_token={token}",
                {"Content-Type": "application/json"},
                {"msgtype": "text", "text": {"content": f"{title}\n{message}"}},
                lambda j, s: j.get("errcode") == 0)
    if provider == "feishu":
        return (token.rstrip("/"), {"Content-Type": "application/json"},
                {"msg_type": "text", "content": {"text": f"{title}\n{message}"}},
                lambda j, s: j.get("code") == 0 or j.get("StatusCode") == 0)
    return (token, {"Content-Type": "application/json"}, {"title": title, "content": message},
            lambda j, s: s == 200)


# ---------------- 控制面主体 ----------------

class ControlPlane:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.seen = self._load_json(SEEN_FILE, {})
        self.found_recent = self._load_json(FOUND_FILE, [])
        scan_cache = self._load_json(SCAN_CACHE_FILE, {})
        self.known_centers = set(scan_cache.get("centers") or [])
        self.known_times = set(scan_cache.get("test_times") or [])
        self.scan_cache = scan_cache
        self.status = {
            "mode": "idle",  # idle=无worker stopped=开关关 monitoring=worker在线
            "worker_last_seen": None, "worker_neea_ok": None,
            "last_poll_at": None, "next_poll_at": None,
            # 去重存储里是历史推送过的考位键，重启后据此恢复计数
            "seats_pushed_total": len(self.seen), "last_heartbeat": None,
            "scan": {"running": False, "results": {}},
            "scan_deadline": None,  # worker 领取扫描后设置的完成期限
        }
        self._cfg_mtime = None
        self._last_heartbeat_day = None

    @staticmethod
    def _load_json(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _save_json(path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    # ---- 推送 ----
    def push(self, title, message):
        p = self.cfg["push"]
        if not p.get("token"):
            return False
        import urllib.request
        import urllib.parse
        try:
            url, headers, payload, ok_pred = build_push(
                p.get("provider"), p.get("token"), title, message)
            if payload is None:
                req = urllib.request.Request(url, method="GET")
            else:
                is_json = "json" in (headers or {}).get("Content-Type", "")
                data = json.dumps(payload).encode() if is_json \
                    else urllib.parse.urlencode(payload).encode()
                req = urllib.request.Request(url, data=data, method="POST")
                if headers:
                    for k, v in headers.items():
                        req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode() or "{}")
                status = resp.status
            if ok_pred(body, status):
                log.info("推送成功: %s", title)
                return True
            log.error("推送失败: HTTP %s %s", status, str(body)[:150])
        except Exception as exc:
            log.error("推送异常: %s", exc)
        return False

    def heartbeat(self):
        p = self.cfg["push"]
        hb = p.get("heartbeat_time") or ""
        if not hb or not p.get("token"):
            return
        now = datetime.now()
        if now.strftime("%H:%M") < hb:
            return
        if self._last_heartbeat_day == now.strftime("%Y-%m-%d"):
            return
        worker_alive = self.status.get("worker_last_seen") and \
            (datetime.now() - datetime.fromisoformat(self.status["worker_last_seen"])).total_seconds() < 300
        if not worker_alive:
            return  # worker 不在线时不发心跳（避免误导"监控活着"）
        self._last_heartbeat_day = now.strftime("%Y-%m-%d")
        self.push("IELTS监控心跳",
                  f"监控运行中: 日期{len(expand_dates(self.cfg))}个 "
                  f"累计推送考位 {self.status['seats_pushed_total']} 个")

    # ---- worker 上报处理 ----
    def handle_report(self, body):
        with self.lock:
            self.status["worker_last_seen"] = datetime.now().isoformat(timespec="seconds")
            self.status["worker_neea_ok"] = bool(body.get("neea_ok", True))
            self.status["mode"] = "monitoring" if self.cfg["monitor"].get("enabled", True) else "stopped"
            self.status["last_poll_at"] = self.status["worker_last_seen"]

            for c in body.get("known_centers") or []:
                self.known_centers.add(c)
            for t in body.get("known_test_times") or []:
                self.known_times.add(t)

            # 扫描结果回传
            if body.get("scan_results"):
                results = body["scan_results"]  # {cityCode: [dates]}
                named = {CITY_MAP.get(c, c): d for c, d in results.items()}
                self.scan_cache["cities"] = results
                self.status["scan"] = {"running": False, "results": named}
                self._persist_knowledge()
                self.push("IELTS日期扫描完成",
                          "；".join(f"{k} {len(v)}天" for k, v in named.items()))
                log.info("扫描完成: %s",
                         {CITY_MAP.get(c, c): len(d) for c, d in results.items()})

            # 考位处理：过滤 → 去重 → 推送
            cfg_m = self.cfg["monitor"]
            new_seats = []
            for seat in body.get("seats") or []:
                if not session_matches(seat.get("testTime"), cfg_m.get("session_keywords")):
                    continue
                if not center_matches(seat.get("centerName"), cfg_m.get("center_keywords")):
                    continue
                key = f"{seat.get('testDay')}:{seat.get('seatId')}"
                if key not in self.seen:
                    self.seen[key] = datetime.now().isoformat(timespec="seconds")
                    seat["found_at"] = datetime.now().isoformat(timespec="seconds")
                    new_seats.append(seat)
            today = datetime.now().strftime("%Y-%m-%d")
            self.seen = {k: v for k, v in self.seen.items() if k.split(":")[0] >= today}
            if new_seats:
                lines = [f"{s['testDay']} {s.get('testTime','')} {s.get('centerName','')}"
                         for s in new_seats]
                self.push("IELTS发现考位!", "\n".join(lines))
                self.status["seats_pushed_total"] += len(new_seats)
                self.found_recent = (new_seats + self.found_recent)[:50]
                self._save_json(FOUND_FILE, self.found_recent)
                log.info("发现 %d 个新考位: %s", len(new_seats), "; ".join(lines))
            self._save_json(SEEN_FILE, self.seen)
            self._persist_knowledge()
        return {"ok": True, "accepted": len(new_seats) if not body.get("scan_results") else 0}

    def _persist_knowledge(self):
        self._save_json(SCAN_CACHE_FILE, {
            "at": datetime.now().isoformat(timespec="seconds"),
            "cities": self.scan_cache.get("cities") or {},
            "centers": sorted(self.known_centers),
            "test_times": sorted(self.known_times),
        })

    # ---- worker 领取任务 ----
    def worker_config(self):
        cfg_m = self.cfg["monitor"]
        interval = cfg_m["interval_seconds"]
        now_cn = datetime.now(TZ_CN) if TZ_CN else datetime.now()
        if in_night_pause(self.cfg, now_cn):
            interval = cfg_m["night_pause"].get("interval_seconds", 600)
        jitter = cfg_m.get("jitter_seconds", 30)
        # worker 端做抖动，这里下发 interval+jitter 范围
        task = {
            "enabled": bool(cfg_m.get("enabled", True)),
            "product_code": cfg_m["product_code"],
            "cities": cfg_m["cities"],
            "dates": expand_dates(self.cfg),
            "interval_ms": max(30, interval) * 1000,
            "jitter_ms": jitter * 1000,
            "scan_days": 0,
            # 凭据仅经 localhost 下发给同机浏览器脚本, 用于会话过期自动重登
            "neea": {"user_id": self.cfg["neea"].get("user_id", ""),
                     "password": self.cfg["neea"].get("password", "")},
        }
        # 扫描任务下发（一次性）
        if self.cfg.get("scan", {}).get("request_days", 0) > 0:
            task["scan_days"] = self.cfg["scan"]["request_days"]
            self.cfg["scan"]["request_days"] = 0
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(self.cfg, f, allow_unicode=True, sort_keys=False)
            self._cfg_mtime = os.path.getmtime(CONFIG_PATH)
            self.status["scan"] = {"running": True, "results": {}}
            self.status["scan_deadline"] = (datetime.now() + timedelta(minutes=8)).isoformat(timespec="seconds")
            log.info("已向 worker 下发扫描任务: %d 天", task["scan_days"])
        return task

    # ---- 主循环（配置热加载 + 心跳 + worker 掉线检测） ----
    def run_forever(self):
        log.info("控制面启动, 配置: %s", CONFIG_PATH)
        while True:
            try:
                mtime = os.path.getmtime(CONFIG_PATH)
                if self._cfg_mtime is not None and mtime != self._cfg_mtime:
                    self.cfg = load_config(CONFIG_PATH)
                    log.info("配置已热加载: 日期%d个 城市%s", len(expand_dates(self.cfg)),
                             self.cfg["monitor"]["cities"])
                self._cfg_mtime = mtime
            except OSError as exc:
                log.error("读取配置失败: %s", exc)
                time.sleep(10)
                continue

            # worker 掉线检测
            ls = self.status.get("worker_last_seen")
            if ls:
                age = (datetime.now() - datetime.fromisoformat(ls)).total_seconds()
                if age > 300 and self.status["mode"] == "monitoring":
                    self.status["mode"] = "worker_offline"
                    log.warning("worker 超过 5 分钟未上报（NEEA 标签页关了或 Chrome 退了）")

            # 扫描超时回收
            if self.status["scan"].get("running") and self.status.get("scan_deadline"):
                if datetime.now().isoformat(timespec="seconds") > self.status["scan_deadline"]:
                    self.status["scan"] = {"running": False, "results": {}}
                    log.warning("扫描超时未回传，已回收")

            self.heartbeat()
            time.sleep(5)


# ---------------- 校验与序列化 ----------------

def validate_config_payload(current, body):
    cfg = json.loads(json.dumps(current))
    neea = body.get("neea") or {}
    if "user_id" in neea:
        cfg["neea"]["user_id"] = str(neea["user_id"]).strip()
    if neea.get("password"):
        cfg["neea"]["password"] = str(neea["password"])
    m = body.get("monitor") or {}
    if "enabled" in m:
        cfg["monitor"]["enabled"] = bool(m["enabled"])
    if m.get("product_code") in ("IELTSCDI", "IELTSPBT"):
        cfg["monitor"]["product_code"] = m["product_code"]
    if "cities" in m:
        cities = [c for c in (m["cities"] or []) if c in CITY_MAP]
        if not cities:
            raise ValueError("至少选择一个城市")
        cfg["monitor"]["cities"] = cities
    if "dates" in m:
        dates = []
        for d in (m["dates"] or []):
            d = str(d).strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                raise ValueError(f"日期格式错误: {d}")
            dates.append(d)
        cfg["monitor"]["dates"] = sorted(set(dates))
        cfg["monitor"]["date_range"] = {"start": "", "end": ""}
    if "date_range" in m:
        rng = m["date_range"] or {}
        start, end = str(rng.get("start") or ""), str(rng.get("end") or "")
        for v in (start, end):
            if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise ValueError(f"日期范围格式错误: {v}")
        if start and end and start > end:
            raise ValueError("日期范围 start > end")
        cfg["monitor"]["date_range"] = {"start": start, "end": end}
    if "center_keywords" in m:
        cfg["monitor"]["center_keywords"] = [str(k).strip() for k in (m["center_keywords"] or []) if str(k).strip()]
    if "session_keywords" in m:
        cfg["monitor"]["session_keywords"] = [str(k).strip() for k in (m["session_keywords"] or []) if str(k).strip()]
    if "interval_seconds" in m:
        cfg["monitor"]["interval_seconds"] = min(600, max(30, int(m["interval_seconds"])))
    if "jitter_seconds" in m:
        cfg["monitor"]["jitter_seconds"] = min(120, max(0, int(m["jitter_seconds"])))
    p = body.get("push") or {}
    if p.get("provider") in PUSH_PROVIDERS:
        cfg["push"]["provider"] = p["provider"]
    if "token" in p:
        cfg["push"]["token"] = str(p["token"]).strip()
    if "heartbeat_time" in p:
        hb = str(p["heartbeat_time"]).strip()
        if hb and not re.fullmatch(r"\d{2}:\d{2}", hb):
            raise ValueError("心跳时间格式错误")
        cfg["push"]["heartbeat_time"] = hb
    return cfg


def sanitize(cfg):
    out = json.loads(json.dumps(cfg))
    out["neea"].pop("password", None)
    out["neea"]["has_password"] = bool(cfg["neea"].get("password"))
    return out


def _tz_aware(value):
    """naive ISO 时间串补上本地时区偏移后再下发，浏览器才能算对相对时间。"""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value):
        return datetime.fromisoformat(value).astimezone().isoformat(timespec="seconds")
    if isinstance(value, list):
        return [_tz_aware(v) for v in value]
    if isinstance(value, dict):
        return {k: _tz_aware(v) for k, v in value.items()}
    return value


def create_app(cp):
    app = Flask(__name__)

    @app.get("/")
    def index():
        with open(UI_PATH, encoding="utf-8") as f:
            html = f.read()
        return html.replace("__CITY_MAP_JSON__", json.dumps(CITY_MAP, ensure_ascii=False))

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/api/config")
    def get_config():
        return jsonify(sanitize(cp.cfg))

    @app.post("/api/config")
    def post_config():
        try:
            body = request.get_json(force=True)
            new_cfg = validate_config_payload(cp.cfg, body)
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(new_cfg, f, allow_unicode=True, sort_keys=False)
        cp.cfg = new_cfg
        cp._cfg_mtime = os.path.getmtime(CONFIG_PATH)
        log.info("面板保存配置: 日期%d个 城市%s", len(expand_dates(new_cfg)),
                 new_cfg["monitor"]["cities"])
        return jsonify({"ok": True})

    @app.get("/api/status")
    def get_status():
        return jsonify({
            "status": _tz_aware(cp.status),
            "config": sanitize(cp.cfg),
            "effective_dates": expand_dates(cp.cfg),
            "recent_found": _tz_aware(cp.found_recent[:20]),
            "known_centers": sorted(cp.known_centers),
            "known_test_times": sorted(cp.known_times),
            "scan_cache": _tz_aware(cp.scan_cache.get("cities") or {}),
            "logs": ring.buf[-100:],
        })

    app.add_url_rule("/status", view_func=get_status)

    @app.post("/api/test-push")
    def test_push():
        if not cp.cfg["push"].get("token"):
            return jsonify({"ok": False, "error": "请先填写推送 Token"}), 400
        ok = cp.push("IELTS考位监控", "测试推送成功! 通道已打通")
        return jsonify({"ok": ok}), (200 if ok else 502)

    # ---- worker 专用端点（油猴脚本） ----
    @app.get("/api/worker-config")
    def worker_config():
        return jsonify(cp.worker_config())

    @app.post("/api/worker-report")
    def worker_report():
        body = request.get_json(silent=True) or {}
        result = cp.handle_report(body)
        return jsonify(result)

    # 面板触发扫描 → 写入 scan.request_days, worker 领取
    @app.post("/api/scan-dates")
    def scan_dates_ep():
        days = min(90, max(7, int((request.get_json(silent=True) or {}).get("days", 90))))
        if cp.status["scan"].get("running"):
            return jsonify({"ok": False, "error": "扫描进行中，请稍候"}), 409
        cp.cfg["scan"] = {"request_days": days}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(cp.cfg, f, allow_unicode=True, sort_keys=False)
        cp._cfg_mtime = os.path.getmtime(CONFIG_PATH)
        # 若 worker 在线会立即领取（其轮询周期内）；否则等它上线领取
        log.info("面板请求扫描 %d 天, 等待 worker 领取", days)
        return jsonify({"ok": True, "days": days, "queued": True})

    @app.post("/api/poll-now")
    def poll_now():
        # 混合架构下查询由 worker 发起；立即触发 = 缩短其下次轮询
        return jsonify({"ok": True, "note": "由浏览器数据源自动执行，请稍候刷新状态"})

    return app


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    cp = ControlPlane(load_config(CONFIG_PATH))
    app = create_app(cp)
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=9899, threaded=True),
                     daemon=True).start()
    cp.run_forever()


if __name__ == "__main__":
    main()
