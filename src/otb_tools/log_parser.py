"""OpenTenBase stderr 日志解析与跨节点 coord(pid,vxid) 关联。

日志行格式（v5.0 默认 log_line_prefix='%m [%p] '，实测样本）：
  2026-11-20 17:43:54.411 CST [2545,coord(2533,76)] FATAL:  terminating connection ...
  - pid 为本进程后端 pid；coord(pid,vxid) 为该会话的协调者信息：
    DN 上由 CN 每条语句下发的 'U' 消息填充（postgres.c:8018-8041），
    因此 coord 前项 = CN 后端 pid，可作为 CN/DN 日志关联键。
  - coord(0,0) 表示本节点内部事件（InitMultinodeExecutor、pooler 等），不参与关联。
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import rules

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) (?P<tz>\S+) "
    r"\[(?P<pid>\d+),coord\((?P<coord_pid>-?\d+),(?P<coord_vxid>-?\d+)\)\] "
    r"(?P<level>\w+):\s+(?P<message>.*)$"
)
SQLSTATE_RE = re.compile(r"\(([0-9A-Z]{5})\)")

ERROR_LEVELS = {"ERROR", "FATAL", "PANIC"}
CORRELATABLE_LEVELS = {"WARNING", "ERROR", "FATAL", "PANIC"}
EVENT_LEVELS = {"LOG", *CORRELATABLE_LEVELS}
MAX_MESSAGE_CHARS = 500

# 日志行中的时区缩写 → 固定 UTC 偏移（小时）。
# OpenTenBase 中文环境日志默认打印 CST（中国标准时间，UTC+8，实验环境实测）。
# 未知时区按 UTC 处理：窗口过滤保守（宁多勿漏），诊断输出保留原始时间字符串可人工核对。
TZ_OFFSETS = {"CST": 8, "UTC": 0, "GMT": 0, "Z": 0}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def corr_in_window(c: dict, cutoff: datetime) -> bool:
    """按这条关联记录实际涉及的最新事件时间过滤；兼容旧的 CN 单侧记录。"""
    try:
        ts = datetime.fromisoformat(c.get("time") or c["cn_entry"]["time"])
    except (KeyError, ValueError):
        try:
            times = [datetime.fromisoformat(e["time"]) for e in c.get("dn_entries", []) if e.get("time")]
        except ValueError:
            return True
        if not times:
            return True
        ts = max(times)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= cutoff


@dataclass
class LogEntry:
    ts: datetime
    pid: int
    coord_pid: int
    coord_vxid: int
    level: str
    message: str
    node: str
    source: str = ""
    lineno: int = 0
    sqlstate: str = ""
    categories: list = field(default_factory=list)

    def coord_key(self) -> str | None:
        if self.coord_pid > 0:
            return f"{self.coord_pid},{self.coord_vxid}"
        return None

    def to_dict(self) -> dict:
        message = self.message
        truncated = len(message) > MAX_MESSAGE_CHARS
        if truncated:
            message = message[: MAX_MESSAGE_CHARS - 1] + "…"
        return {
            "time": self.ts.isoformat(timespec="milliseconds"),
            "pid": self.pid,
            "coord": f"{self.coord_pid},{self.coord_vxid}",
            "level": self.level,
            "node": self.node,
            "source": f"{self.source}:{self.lineno}" if self.source else "",
            "sqlstate": self.sqlstate or "",
            "message": message,
            "message_truncated": truncated,
            "categories": self.categories,
        }


def parse_line(line: str, node: str = "", source: str = "", lineno: int = 0) -> LogEntry | None:
    """解析单行；格式不匹配返回 None（调用方统计 unparsed）。时间统一为 UTC aware。"""
    m = LINE_RE.match(line.rstrip("\n"))
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None
    offset = TZ_OFFSETS.get(m.group("tz").upper(), 0)
    ts = ts.replace(tzinfo=timezone(timedelta(hours=offset))).astimezone(timezone.utc)
    msg = m.group("message")
    sm = SQLSTATE_RE.search(msg)
    return LogEntry(
        ts=ts,
        pid=int(m.group("pid")),
        coord_pid=int(m.group("coord_pid")),
        coord_vxid=int(m.group("coord_vxid")),
        level=m.group("level"),
        message=msg,
        node=node,
        source=source,
        lineno=lineno,
        sqlstate=sm.group(1) if sm else "",
        categories=rules.lookup_by_category(msg),
    )


def parse_file(path: str, node: str = "") -> tuple[list[LogEntry], int]:
    """返回 (解析出的条目, 未解析行数)。空行不计入未解析。文件缺失/不可读抛 OSError。"""
    entries: list[LogEntry] = []
    unparsed = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            e = parse_line(line, node=node, source=path, lineno=lineno)
            if e is None:
                unparsed += 1
            else:
                entries.append(e)
    return entries, unparsed


def _level_rank(level: str) -> int:
    return {"LOG": 0, "WARNING": 1, "ERROR": 2, "FATAL": 3, "PANIC": 4}.get(level, -1)


def correlate(
    cn_entries: list[LogEntry],
    dn_entries: list[LogEntry],
    window: timedelta = timedelta(seconds=30),
) -> list[dict]:
    """按 coord(pid,vxid) 聚合 CN 与 DN 两侧事件，双向关联。

    DN 故障时 CN 侧往往只有 LOG 级连接状态变化行（Changing connection state），
    因此不单向依赖 CN 的 ERROR/FATAL 做种子。

    置信度分级（按 coord 键聚合后）：
      confirmed   - 窗口内存在配对，且（CN 侧有 ERROR/FATAL 且 DN 侧有 WARNING 及以上）
                    或（DN 侧有 WARNING 及以上且 CN 侧有任意级别记录，如连接状态迁移 LOG）；
      candidate   - 两侧都有记录但级别不足（仅 LOG），或同键但所有配对都超出时间窗口
                    （可能为 PID 复用）；
      insufficient- 只有单侧记录（对侧日志缺失/轮转/未记录该事件）。

    两个时间约束必须区分，由不同层负责：
      - 查询时间窗口（用户要求的日志范围）：调用方在调用本函数前过滤输入事件
        （e.ts >= cutoff），保证关联结果中不出现窗口外的事件；
      - 配对窗口（window 参数，默认 30s）：同一 coord 键两侧事件可视为同一执行
        上下文的最大时间间隔。
    """
    cn_index: dict[str, list[LogEntry]] = {}
    dn_index: dict[str, list[LogEntry]] = {}
    for e in cn_entries:
        key = e.coord_key()
        if key and e.level in EVENT_LEVELS:
            cn_index.setdefault(key, []).append(e)
    for e in dn_entries:
        key = e.coord_key()
        if key and e.level in EVENT_LEVELS:
            dn_index.setdefault(key, []).append(e)

    keys = set(cn_index) | set(dn_index)
    results: list[dict] = []
    window_seconds = max(0.0, window.total_seconds())

    def make_result(key, cn, dns, confidence, reason, event_time, total=None):
        shown = dns[-5:]
        payload = {
            "coord": key,
            "time": event_time.isoformat(timespec="milliseconds"),
            "cn_entry": cn.to_dict() if cn else {},
            "dn_entries": [d.to_dict() for d in shown],
            "confidence": confidence,
            "reason": reason,
        }
        if total is not None and total > len(shown):
            payload["dn_entry_count"] = total
            payload["dn_entries_truncated"] = True
        return payload

    for key in sorted(keys):
        cn_ev = sorted(cn_index.get(key, []), key=lambda e: e.ts)
        dn_ev = sorted(dn_index.get(key, []), key=lambda e: e.ts)
        if not cn_ev:
            if dn_ev:
                results.append(make_result(
                    key, None, dn_ev, "insufficient",
                    "CN 侧日志无同 coord 记录（CN 日志缺失/轮转，该事件未在 CN 侧落盘，或对侧事件在请求窗口外）",
                    dn_ev[-1].ts, len(dn_ev),
                ))
            continue
        if not dn_ev:
            results.append(make_result(
                key, cn_ev[-1], [], "insufficient",
                "DN 侧日志无同 coord 记录（DN 日志缺失/轮转/未记录，或对侧事件在请求窗口外）", cn_ev[-1].ts,
            ))
            continue
        dn_times = [d.ts for d in dn_ev]
        dn_warn = [d for d in dn_ev if d.level in CORRELATABLE_LEVELS]
        dn_warn_times = [d.ts for d in dn_warn]
        confirmed_pair = None
        low_level_pair = None
        for c in cn_ev:
            low = bisect_left(dn_times, c.ts - timedelta(seconds=window_seconds))
            high = bisect_right(dn_times, c.ts + timedelta(seconds=window_seconds))
            if high > low:
                low_level_pair = (c, dn_ev[low:high])
            warn_low = bisect_left(dn_warn_times, c.ts - timedelta(seconds=window_seconds))
            warn_high = bisect_right(dn_warn_times, c.ts + timedelta(seconds=window_seconds))
            if warn_high > warn_low:
                confirmed_pair = (c, dn_warn[warn_low:warn_high])

        if confirmed_pair:
            c, matched_dns = confirmed_pair
            event_time = max([c.ts, *(d.ts for d in matched_dns)])
            results.append(make_result(
                key, c, matched_dns, "confirmed",
                "CN 与 DN 同 coord 事件在配对窗口内命中（DN 侧含 WARNING 及以上事件），两侧属于同一执行上下文",
                event_time, len(matched_dns),
            ))
        elif low_level_pair:
            c, matched_dns = low_level_pair
            event_time = max([c.ts, *(d.ts for d in matched_dns)])
            results.append(make_result(
                key, c, matched_dns, "candidate",
                "两侧存在窗口内的同 coord 事件（DN 侧均为 LOG 级）",
                event_time, len(matched_dns),
            ))
        else:
            event_time = max(cn_ev[-1].ts, dn_ev[-1].ts)
            results.append(make_result(
                key, cn_ev[-1], dn_ev, "candidate",
                "同 coord 键但没有时间窗口内的配对（可能为 PID 复用），关联为候选",
                event_time, len(dn_ev),
            ))
    return results
