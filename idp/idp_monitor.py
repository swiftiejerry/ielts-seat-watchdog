"""IDP 雅思（sign.idpielts.cn）考位监控 · 常驻服务。

与 NEEA 侧控制面是两套独立数据源，刻意不混进同一个容器：
NEEA 依赖真实 Chrome + 油猴（瑞数 WAF），IDP 是纯 HTTP 公开查询接口。

数据通道结论（实测）：
- 接口免登录、免验证码、无瑞数式请求级 WAF，裸 HTTP 即可 200
- 一次 POST 拿全量；传 centerId 可精确锁定单考点，响应从 2.1MB 降到 70KB
- 响应体 SM4-CBC 加密，密钥/IV 硬编码在前端 bundle（会轮换，见 README）
- 4 req/s 连打 80 次、2s 间隔连打 150 次均零失败；5 分钟内实测到真实数据
  变化，证明返回的是实时值而非缓存快照

判定「有位」只认 fullyBooked（官方报名页 isTimeFull 的唯一依据）。
不用 signUpCount 与 participantsCount 的差值——超售时该差值会失真，
实测 179 条 fullyBooked=1 的记录里有 97 条 signUpCount < participantsCount。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import yaml
from curl_cffi import requests
from flask import Flask, jsonify
from gmssl.sm4 import CryptSM4, SM4_DECRYPT

CONFIG_PATH = os.environ.get("IDP_CONFIG", "/app/config.yml")
DATA_DIR = os.environ.get("IDP_DATA", "/data")
UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
STATE_FILE = os.path.join(DATA_DIR, "idp_state.json")
FOUND_FILE = os.path.join(DATA_DIR, "idp_found.json")

API = "https://sign.idpielts.cn/chinesetestwebapi/common/testCenterSearch"
SM4_KEY = bytes.fromhex("065574e7ef3d92c579ffba093797b4f2")
SM4_IV = bytes.fromhex("7a6b964619a05e5ce5423608b7bf4e95")

TZ_CN = timezone(timedelta(hours=8))

PUSH_PROVIDERS = {"pushplus", "serverchan", "bark", "wecom-hook", "feishu", "custom"}

DEFAULTS = {
    "enabled": True,
    "center_id": "22090ffd-27cd-4945-09c8-71f67fa5bcaf",  # IDP 深圳福田
    "center_name": "IDP雅思深圳福田区考点",
    "dates": [],
    # "rolling" = 自动盯未来 N 天（窗口随日期自己往前滚，无需手动重选）
    # "all"     = 盯该考点全部场次
    # "list"    = 只盯 dates 里列的
    "dates_mode": "rolling",
    "rolling_days": 21,
    "times": ["14:00 PM"],   # 接口 time 字段的写法；examTime 是 02:00 PM，别混用
    "interval_seconds": 30,
    "jitter_seconds": 10,
    "night_pause": {"enabled": True, "from": "01:00", "to": "07:00", "interval_seconds": 300},
    "push": {"provider": "pushplus", "token": "", "heartbeat_time": "09:00"},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("idp-monitor")


# ---------------- 数据面 ----------------

def decrypt(hex_str: str) -> str:
    c = CryptSM4()
    c.set_key(SM4_KEY, SM4_DECRYPT)
    out = c.crypt_cbc(SM4_IV, bytes.fromhex(hex_str))
    pad = out[-1]
    if 0 < pad <= 16:
        out = out[:-pad]
    return out.decode("utf-8")


def fetch_seats(center_id: str) -> list[dict]:
    r = requests.post(API, json={"pageNum": 1, "pageSize": 200, "centerId": center_id},
                      timeout=25, impersonate="chrome")
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != 200:
        raise RuntimeError(f"接口异常: {payload.get('msg')}")
    return json.loads(decrypt(payload["data"]))["rows"]


def is_available(row: dict) -> bool:
    """能不能真的报上：官方标记未满，且数值上还有余量。

    两个条件缺一不可，理由都是实测出来的：
    - fullyBooked 是官方报名页 isTimeFull() 的唯一依据，但它会滞后。
      实测 2026-11-29 从 34/35 涨到 35/35 时 fullyBooked 仍是 '0'，
      只看它会把已满场次报成"有位"，用户点进去才发现满了。
    - 反过来只看余量也不行：179 条 fullyBooked='1' 的记录里有 97 条
      signUpCount < participantsCount（超售/锁位），余量看着是正的，
      但网站不放人。
    """
    if str(row.get("fullyBooked")) in ("1", "true", "True"):
        return False
    try:
        return int(row["participantsCount"]) - int(row["signUpCount"]) > 0
    except (KeyError, TypeError, ValueError):
        return True   # 容量字段异常时退回官方标记，宁可多报不可漏报


def seat_label(row: dict) -> str:
    cap, signed = row.get("participantsCount"), row.get("signUpCount")
    try:
        return f"{signed}/{cap}（余 {int(cap) - int(signed)}）"
    except (TypeError, ValueError):
        return f"{signed}/{cap}"


# ---------------- 推送 ----------------

def build_push(provider, token, title, message):
    """与 NEEA 控制面同协议，便于复用同一份推送 token。"""
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
        return (f"{base}/{urllib.parse.quote(title)}/{urllib.parse.quote(message)}?sound=alarm",
                {}, None, lambda j, s: s == 200)
    if provider == "wecom-hook":
        return (f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={token}",
                {"Content-Type": "application/json"},
                {"msgtype": "text", "text": {"content": f"{title}\n{message}"}},
                lambda j, s: j.get("errcode") == 0)
    if provider == "feishu":
        return (token.rstrip("/"), {"Content-Type": "application/json"},
                {"msg_type": "text", "content": {"text": f"{title}\n{message}"}},
                lambda j, s: j.get("code") == 0 or j.get("StatusCode") == 0)
    return (token, {"Content-Type": "application/json"}, {"title": title, "content": message},
            lambda j, s: s == 200)


# ---------------- 服务主体 ----------------

class IdpMonitor:
    def __init__(self):
        self.cfg = self._load_cfg()
        self.lock = threading.Lock()
        self.seen = self._load_json(STATE_FILE, {})
        self.found = self._load_json(FOUND_FILE, [])
        self.status = {
            "started_at": datetime.now(TZ_CN).isoformat(timespec="seconds"),
            "last_poll_at": None,
            "last_poll_ok": None,
            "last_error": None,
            "polls_total": 0,
            "polls_failed": 0,
            "seats_pushed_total": 0,
            "available_now": 0,
            "sessions_total": 0,
            "poll_mode": "idle",
        }
        self._last_heartbeat_day = None
        self._stop = threading.Event()
        self._thread = None

    # ---- 配置与持久化 ----
    def _load_cfg(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.warning("读取配置失败，用默认值: %s", exc)
            raw = {}
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in raw.items() if v is not None})
        cfg["push"] = {**DEFAULTS["push"], **(raw.get("push") or {})}
        cfg["night_pause"] = {**DEFAULTS["night_pause"], **(raw.get("night_pause") or {})}
        return cfg

    @staticmethod
    def _load_json(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _save_json(path, obj):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)   # 原子替换，避免容器被杀时写出半个文件

    # ---- 推送 ----
    def push(self, title, message):
        p = self.cfg["push"]
        if not p.get("token"):
            log.warning("未配置推送 token，跳过: %s", title)
            return False
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
                for k, v in (headers or {}).items():
                    req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode() or "{}")
                code = resp.status
            if ok_pred(body, code):
                log.info("推送成功: %s", title)
                return True
            log.error("推送失败: HTTP %s %s", code, str(body)[:150])
        except Exception as exc:
            log.error("推送异常: %s", exc)
        return False

    def heartbeat(self):
        p = self.cfg["push"]
        hb = p.get("heartbeat_time") or ""
        if not hb or not p.get("token"):
            return
        now = datetime.now(TZ_CN)
        if now.strftime("%H:%M") < hb:
            return
        if self._last_heartbeat_day == now.strftime("%Y-%m-%d"):
            return
        # 至少成功轮询过一次才发心跳，否则等于宣布一个还没验证过的监控"活着"
        if not self.status["last_poll_ok"]:
            return
        self._last_heartbeat_day = now.strftime("%Y-%m-%d")
        self.push("IDP监控心跳",
                  f"IDP 监控运行中：{self.cfg['center_name']} "
                  f"当前可报 {self.status['available_now']} 场，"
                  f"累计推送 {self.status['seats_pushed_total']} 次")

    # ---- 节奏 ----
    def _in_night(self, now):
        np = self.cfg["night_pause"]
        if not np.get("enabled"):
            return False
        cur, start, end = now.strftime("%H:%M"), np["from"], np["to"]
        return start <= cur < end if start <= end else (cur >= start or cur < end)

    def current_interval(self, now):
        if self._in_night(now):
            return float(self.cfg["night_pause"]["interval_seconds"]), "night"
        return float(self.cfg["interval_seconds"]), "normal"

    # ---- 单轮 ----
    def poll_once(self):
        cfg = self.cfg
        now = datetime.now(TZ_CN)
        rows = fetch_seats(cfg["center_id"])

        mode = cfg.get("dates_mode", "rolling")
        if mode == "rolling":
            # 窗口跟着今天走，不写死日期，所以不存在过期需要清理
            lo, hi = rolling_window(today_cn(), int(cfg.get("rolling_days", 21)))
            rows = [r for r in rows if lo <= r["examTimeOrigin"] <= hi]
        elif mode == "list" and cfg["dates"]:
            rows = [r for r in rows if r["examTimeOrigin"] in cfg["dates"]]
        # mode == "all" 时不做日期过滤
        if cfg["times"]:
            rows = [r for r in rows if r["time"] in cfg["times"]]

        with self.lock:
            fresh = []
            for r in rows:
                key = r["ID"]
                free = is_available(r)
                prev = self.seen.get(key)
                # 首轮只建基线：否则启动瞬间会把所有现存空位刷成"新放位"
                if prev is not None and free and prev.get("free") is False:
                    fresh.append(r)
                self.seen[key] = {
                    "free": free,
                    "sign": r.get("signUpCount"),
                    "cap": r.get("participantsCount"),
                    "date": r["examTimeOrigin"],
                    "time": r["time"],
                    "last_seen": now.isoformat(timespec="seconds"),
                }
            self._prune_seen(now)
            self.status.update({
                "last_poll_at": now.isoformat(timespec="seconds"),
                "last_poll_ok": True,
                "last_error": None,
                "polls_total": self.status["polls_total"] + 1,
                "sessions_total": len(rows),
                "available_now": sum(1 for r in rows if is_available(r)),
            })
            if fresh:
                self._announce(fresh, now)
            self._save_json(STATE_FILE, self.seen)
        return len(fresh)

    def _prune_seen(self, now):
        """只保留今天及以后的场次，避免状态文件无限膨胀。"""
        today = now.strftime("%Y-%m-%d")
        self.seen = {k: v for k, v in self.seen.items() if (v.get("date") or "") >= today}

    def _announce(self, fresh, now):
        lines = [f"{r['examTimeOrigin']} {r['time']} {seat_label(r)}" for r in fresh]
        title = f"IDP放位! {len(fresh)}场"
        body = f"{self.cfg['center_name']}\n" + "\n".join(lines) + \
               f"\n\n发现于 {now.strftime('%H:%M:%S')}"
        self.push(title, body)
        self.status["seats_pushed_total"] += len(fresh)
        self.found = ([{"at": now.isoformat(timespec="seconds"),
                        "date": r["examTimeOrigin"], "time": r["time"],
                        "seat": seat_label(r)} for r in fresh] + self.found)[:100]
        self._save_json(FOUND_FILE, self.found)
        log.info("新放位 %d 场: %s", len(fresh), "; ".join(lines))

    # ---- 过期考期作废 ----
    def prune_expired(self, reason: str) -> list[str]:
        """作废已过去的考期并落盘。返回被作废的日期列表。"""
        today = today_cn()
        with self.lock:
            dropped = prune_expired_dates(self.cfg, today)
            if not dropped:
                return []
            new_cfg = self.cfg
            now_empty = (self.cfg.get("dates_mode") == "list" and not self.cfg["dates"])
        # 落盘在锁外做，避免拖慢轮询
        self._write_cfg(new_cfg)
        log.info("作废过期考期(%s): %s", reason, ", ".join(dropped))
        self.status["expired_dates_dropped"] = \
            self.status.get("expired_dates_dropped", 0) + len(dropped)
        if now_empty:
            # 指定模式的日期全过期 = 监控范围归零。必须告诉用户，
            # 否则监控看起来还在跑，实际已经什么都不会报了。
            stamp = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M")
            self.push("IDP监控范围已清空",
                      f"作废过期考期后，指定日期已全部过期：\n{', '.join(dropped)}\n\n"
                      f"当前监控范围为 0 场。请到面板 http://127.0.0.1:9897 "
                      f"重新选择考期或切换为「盯全部考期」。\n\n通知时间 {stamp}")
            log.warning("作废后监控范围为 0 场，已通知用户")
        return dropped

    def _write_cfg(self, cfg):
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
            # config.yml 是 docker 单文件 bind mount，挂载点不能用 os.replace
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        except OSError as exc:
            log.error("写配置失败: %s", exc)

    def _should_prune_now(self, now, last_prune_day) -> bool:
        """启动时立即触发一次；之后每天 00:00 与 09:00 各触发一次。

        last_prune_day 记录的是"哪一天做过 00 点那次"，9 点那次由
        _pruned_at_9 单独标记——单靠日期区分不了同一天的两次触发。
        """
        day = now.strftime("%Y-%m-%d")
        if last_prune_day != day:
            return True   # 跨天了（也覆盖"服务今天还没跑过"和首次启动）
        hhmm = now.strftime("%H:%M")
        if hhmm >= "09:00" and not self._pruned_at_9:
            return True   # 今天的 9 点那次还没做
        return False

    # ---- 主循环 ----
    def _loop(self):
        log.info("IDP 监控启动: %s 间隔 %ss", self.cfg["center_name"], self.cfg["interval_seconds"])
        self._pruned_at_9 = False
        last_prune_day = None
        while not self._stop.is_set():
            now = datetime.now(TZ_CN)
            # 过期考期作废：启动即触发 + 每天 00:00/09:00
            if self._should_prune_now(now, last_prune_day):
                reason = "启动" if last_prune_day is None else "定时"
                self.prune_expired(reason)
                prev_day = last_prune_day
                last_prune_day = now.strftime("%Y-%m-%d")
                if prev_day != last_prune_day:
                    self._pruned_at_9 = False   # 换天了，重置 9 点标记
                if now.strftime("%H:%M") >= "09:00":
                    self._pruned_at_9 = True

            if not self.cfg.get("enabled", True):
                self.status["poll_mode"] = "stopped"
                self._stop.wait(30)
                continue
            interval, mode = self.current_interval(now)
            self.status["poll_mode"] = mode
            try:
                self.poll_once()
            except Exception as exc:
                with self.lock:
                    self.status["last_poll_ok"] = False
                    self.status["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
                    self.status["polls_failed"] += 1
                log.error("轮询失败: %s", self.status["last_error"])
            try:
                self.heartbeat()
            except Exception as exc:
                log.error("心跳异常: %s", exc)
            self._stop.wait(interval + random.uniform(0, self.cfg["jitter_seconds"]))

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="idp-poller")
        self._thread.start()

    def stop(self):
        self._stop.set()


# ---------------- 配置校验 ----------------

IDP_CENTERS = {
    "22090ffd-27cd-4945-09c8-71f67fa5bcaf": "IDP雅思深圳福田区考点",
    "139ed154-5c0b-d789-6ecf-e19d1883fb7c": "IDP雅思深圳福田区考点(ucId)",
}


def today_cn() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%d")


def rolling_window(today: str, days: int) -> tuple[str, str]:
    """返回 [today, today+days] 这个闭区间。

    窗口跟着真实日期往前滚，所以永远不会有"过期"这回事——不需要手动重选，
    也不需要作废逻辑。今天本身算在内：今天的余位同样是可抢的。
    """
    d0 = date.fromisoformat(today)
    return today, (d0 + timedelta(days=days)).isoformat()


def prune_expired_dates(cfg: dict, today: str) -> list[str]:
    """作废已经过去或就是今天的考期，返回被作废的日期。

    只对 list 模式有意义：rolling 模式的窗口跟着真实日期往前滚，
    结构上不可能过期，不需要清理。
    """
    if cfg.get("dates_mode") != "list":
        return []
    kept, dropped = [], []
    for d in cfg.get("dates") or []:
        (dropped if d <= today else kept).append(d)
    if dropped:
        cfg["dates"] = kept
    return dropped


def validate_config_payload(current, body):
    """只接受已知字段，越界的值一律拒绝而不是静默纠正。"""
    cfg = json.loads(json.dumps(current))

    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])

    if "center_id" in body:
        cid = str(body["center_id"]).strip()
        if not cid:
            raise ValueError("考点不能为空")
        cfg["center_id"] = cid
        cfg["center_name"] = IDP_CENTERS.get(cid, str(body.get("center_name") or cid).strip())

    if "dates_mode" in body:
        mode = str(body["dates_mode"]).strip()
        if mode not in ("rolling", "all", "list"):
            raise ValueError(f"未知日期模式: {mode}")
        cfg["dates_mode"] = mode

    if "rolling_days" in body:
        cfg["rolling_days"] = min(90, max(7, int(body["rolling_days"])))

    if "dates" in body:
        dates = []
        for d in body["dates"] or []:
            d = str(d).strip()
            if not d:
                continue
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                raise ValueError(f"日期格式错误: {d}")
            if d <= today_cn():
                raise ValueError(f"日期已过期: {d}（只能选今天及以后）")
            dates.append(d)
        cfg["dates"] = sorted(set(dates))
        # 不在推断模式：取消勾选只是清空列表，不等于要切换模式。
        # 想切换必须显式传 dates_mode，否则用户取消几个勾选就被悄悄换了监控范围。

    if "times" in body:
        times = []
        for t in body["times"] or []:
            t = str(t).strip()
            if not t:
                continue
            if not re.fullmatch(r"\d{2}:\d{2} (AM|PM)", t):
                raise ValueError(f"场次格式错误: {t}（应形如 14:00 PM）")
            times.append(t)
        if not times:
            raise ValueError("至少选择一个场次")
        cfg["times"] = sorted(set(times))

    if "interval_seconds" in body:
        cfg["interval_seconds"] = min(600, max(5, int(body["interval_seconds"])))
    if "jitter_seconds" in body:
        cfg["jitter_seconds"] = min(120, max(0, int(body["jitter_seconds"])))

    np = body.get("night_pause") or {}
    if np:
        if "enabled" in np:
            cfg["night_pause"]["enabled"] = bool(np["enabled"])
        for k in ("from", "to"):
            if k in np:
                v = str(np[k]).strip()
                if not re.fullmatch(r"\d{2}:\d{2}", v):
                    raise ValueError(f"夜间时段格式错误: {v}")
                cfg["night_pause"][k] = v
        if "interval_seconds" in np:
            cfg["night_pause"]["interval_seconds"] = min(
                3600, max(30, int(np["interval_seconds"])))

    push = body.get("push") or {}
    if push:
        if push.get("provider"):
            if push["provider"] not in PUSH_PROVIDERS:
                raise ValueError(f"不支持的推送渠道: {push['provider']}")
            cfg["push"]["provider"] = push["provider"]
        if "token" in push:
            # 空串表示"不修改"：面板留空就是不想动已配置的 token
            tok = str(push["token"]).strip()
            if tok:
                cfg["push"]["token"] = tok
        if "heartbeat_time" in push:
            v = str(push["heartbeat_time"]).strip()
            if v and not re.fullmatch(r"\d{2}:\d{2}", v):
                raise ValueError(f"心跳时间格式错误: {v}")
            cfg["push"]["heartbeat_time"] = v

    return cfg


def sanitize(cfg):
    """对外输出时隐去 token，只留是否已配置。"""
    out = json.loads(json.dumps(cfg))
    token = out["push"].get("token") or ""
    out["push"]["token"] = ""
    out["push"]["token_set"] = bool(token)
    return out


# ---------------- HTTP 状态接口 ----------------

app = Flask(__name__)
monitor: IdpMonitor | None = None


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/config")
def get_config():
    with monitor.lock:
        return jsonify(sanitize(monitor.cfg))


@app.post("/api/config")
def post_config():
    from flask import request
    try:
        body = request.get_json(force=True) or {}
        with monitor.lock:
            new_cfg = validate_config_payload(monitor.cfg, body)
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    try:
        os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
        # 注意：config.yml 是 docker 单文件 bind mount，挂载点不能用 os.replace
        # 原子替换（会 EBUSY），只能原地截断重写。
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(new_cfg, f, allow_unicode=True, sort_keys=False)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"写配置失败: {exc}"}), 500

    with monitor.lock:
        monitor.cfg = new_cfg
    log.info("面板保存配置: 考点=%s 日期%d个 场次%s 间隔%ss",
             new_cfg["center_name"], len(new_cfg["dates"]),
             new_cfg["times"], new_cfg["interval_seconds"])
    return jsonify({"ok": True})


@app.post("/api/test-push")
def test_push():
    with monitor.lock:
        has_token = bool(monitor.cfg["push"].get("token"))
    if not has_token:
        return jsonify({"ok": False, "error": "请先填写推送 Token"}), 400
    ok = monitor.push("IDP考位监控", "测试推送成功! 通道已打通")
    return jsonify({"ok": ok}), (200 if ok else 502)


@app.get("/api/schedule")
def api_schedule():
    """该考点真实有排期的日期与场次。

    面板不能凭空生成日历：IDP 不是天天有考试，实测深圳福田 10-30~12-30
    共 62 天里只有 47 天有排期，中间有 11 段空档。让用户点到不存在的考期，
    等于制造"选了但永远不会有结果"的假监控。
    """
    try:
        rows = fetch_seats(monitor.cfg["center_id"])
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
    by_date = {}
    for r in rows:
        by_date.setdefault(r["examTimeOrigin"], set()).add(r["time"])
    return jsonify({
        "ok": True,
        "center_name": monitor.cfg["center_name"],
        "dates": sorted(by_date),
        "times": sorted({t for ts in by_date.values() for t in ts}),
        "by_date": {d: sorted(ts) for d, ts in by_date.items()},
    })


@app.get("/")
def index():
    try:
        with open(UI_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "<h1>ui.html 缺失</h1>", 500


@app.get("/api/status")
def api_status():
    with monitor.lock:
        body = dict(monitor.status)
        body["config"] = {
            "center_name": monitor.cfg["center_name"],
            "center_id": monitor.cfg["center_id"],
            "dates": monitor.cfg["dates"],
            "dates_mode": monitor.cfg.get("dates_mode", "rolling"),
            "rolling_days": monitor.cfg.get("rolling_days", 21),
            "times": monitor.cfg["times"],
            "interval_seconds": monitor.cfg["interval_seconds"],
            "enabled": monitor.cfg["enabled"],
            "push_configured": bool(monitor.cfg["push"].get("token")),
        }
        if monitor.cfg.get("dates_mode") == "rolling":
            lo, hi = rolling_window(today_cn(), int(monitor.cfg.get("rolling_days", 21)))
            body["rolling_window"] = {"from": lo, "to": hi}
        body["found_recent"] = monitor.found[:20]
    return jsonify(body)


def main():
    global monitor
    monitor = IdpMonitor()
    monitor.start()
    port = int(os.environ.get("IDP_PORT", "9897"))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
