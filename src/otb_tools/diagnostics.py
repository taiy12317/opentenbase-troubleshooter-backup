"""diagnose 子命令：SQLSTATE / 错误文本 / 日志文件的证据化诊断。

诊断结论分级：
  confirmed    - 文本/日志直接命中的已知签名（基于实测或社区证据库）；
  candidate    - 依据签名提出但未经直接证据确认的可能原因；
  hypothesis   - 需要进一步验证的假设。
未命中已知签名时结论为 insufficient，并说明可补充的证据（完整错误文本或日志）。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from . import log_parser, rules
from .log_parser import LogEntry

_CONF_PRIORITY = {"confirmed": 0, "candidate": 1, "insufficient": 2}


def _corr_sort_key(c: dict) -> tuple:
    try:
        ts = datetime.fromisoformat(c["cn_entry"]["time"])
    except (KeyError, ValueError):
        return (_CONF_PRIORITY.get(c["confidence"], 3), float("-inf"))
    return (_CONF_PRIORITY.get(c["confidence"], 3), -ts.timestamp())


def diagnose_sqlstate(code: str) -> dict:
    case = rules.sqlstate_case(code)
    if not case:
        return {
            "input": code.upper(),
            "matched": False,
            "conclusion": f"SQLSTATE {code.upper()} 不在当前案例库中。可提供完整错误文本或日志继续诊断。",
            "confidence": "insufficient",
        }
    return {
        "input": code.upper(),
        "matched": True,
        "name": case["name"],
        "severity": "FAIL",
        "possible_causes": case["causes"],
        "next_steps": case["next"],
        "otb_note": case["otb_note"],
        "conclusion": (
            f"SQLSTATE {code.upper()}（{case['name']}）标识错误类别；"
            "同一错误码可由不同原因触发，结合完整错误文本与原始 SQL 定位具体原因。"
        ),
        "confidence": "confirmed" if case["otb_note"].startswith("reproduced") else "candidate",
    }


def diagnose_text(text: str) -> dict:
    hits = rules.match_signatures(text)
    if not hits:
        return {
            "input": text[:200],
            "matched": False,
            "conclusion": "未命中已知失败签名。可提供 SQLSTATE 或相关节点日志继续诊断。",
            "confidence": "insufficient",
        }
    # 分层匹配：具体签名命中时覆盖同症状的通用类别签名（后者只识别类别不定位根因）
    specific = [h for h in hits if h.get("layer") != "generic"]
    chosen = specific or hits
    return {
        "input": text[:200],
        "matched": True,
        "matches": chosen,
        "confidence": "confirmed" if all(h["evidence_basis"].startswith("reproduced") for h in chosen) else "candidate",
        "conclusion": "；".join(h["id"] for h in chosen),
    }


def diagnose_logs(
    cn_paths: list[str],
    dn_paths: list[str],
    window: timedelta,
    correlate_enabled: bool = True,
    other_paths: list[str] | None = None,
) -> dict:
    """解析日志、分类统计、输出 ERROR/FATAL 明细与跨节点关联。

    other_paths 中的文件只做错误扫描，不参与跨节点关联。
    关联置信度（confirmed/candidate/insufficient）表示两侧日志是否同属一个
    执行上下文；日志关联用于识别跨节点异常事件，错误签名用于提供可能原因及
    排查建议，结合运行状态、错误上下文及验证结果定位具体原因。
    """
    cutoff = log_parser.now_utc() - window
    entries: list[LogEntry] = []
    cn_entries: list[LogEntry] = []
    dn_entries: list[LogEntry] = []
    unparsed_total = 0
    read_errors: list[str] = []
    format_mismatch: list[str] = []

    for path, role in list(zip(cn_paths, ["coordinator"] * len(cn_paths))) + \
            list(zip(dn_paths, ["datanode"] * len(dn_paths))):
        try:
            es, unparsed = log_parser.parse_file(path, node=role)
        except OSError as exc:
            read_errors.append(f"{path}: {exc}")
            continue
        unparsed_total += unparsed
        if not es and unparsed > 0:
            format_mismatch.append(path)
        for e in es:
            entries.append(e)
            (cn_entries if role == "coordinator" else dn_entries).append(e)
    for path in other_paths or []:
        try:
            es, unparsed = log_parser.parse_file(path, node=os.path.basename(path))
        except OSError as exc:
            read_errors.append(f"{path}: {exc}")
            continue
        unparsed_total += unparsed
        if not es and unparsed > 0:
            format_mismatch.append(path)
        entries.extend(es)

    in_window = [e for e in entries if e.ts >= cutoff]
    error_lines = [e for e in in_window if e.level in log_parser.ERROR_LEVELS]

    category_counts: dict[str, int] = {}
    for e in error_lines:
        for c in e.categories:
            category_counts[c] = category_counts.get(c, 0) + 1
        if not e.categories:
            category_counts["unclassified"] = category_counts.get("unclassified", 0) + 1

    # 查询时间窗口过滤在关联之前：窗口外事件不进入关联结果（配对窗口另行由
    # correlate 的 30s 参数控制，两者语义不同，不得混为一谈）。
    cn_in_window = [e for e in cn_entries if e.ts >= cutoff]
    dn_in_window = [e for e in dn_entries if e.ts >= cutoff]
    correlations = log_parser.correlate(cn_in_window, dn_in_window) if correlate_enabled else []
    corr_in_window = [
        c for c in correlations
        if log_parser.corr_in_window(c, cutoff)
    ]
    corr_in_window.sort(key=_corr_sort_key)

    return {
        "window": f"最近 {window.total_seconds() / 3600:.1f} 小时",
        "read_errors": read_errors,
        "unparsed_lines": unparsed_total,
        "unparsed_files": format_mismatch,
        "total_parsed": len(entries),
        "error_fatal_in_window": len(error_lines),
        "category_counts": category_counts,
        "errors": [e.to_dict() for e in error_lines[:20]],
        "correlations": corr_in_window[:20],
        "root_cause": {
            "determined": False,
            "note": (
                "日志关联用于识别跨节点异常事件，错误签名用于提供可能原因及排查建议；"
                "结合运行状态、错误上下文及验证结果定位具体原因。"
            ),
        },
        "conclusion": _conclusion(len(error_lines), category_counts, read_errors, format_mismatch),
    }


def _conclusion(n_errors: int, cats: dict, read_errors: list, format_mismatch: list) -> str:
    parts = []
    incomplete = bool(read_errors) or bool(format_mismatch)
    if read_errors:
        parts.append(f"{len(read_errors)} 个日志文件无法读取（证据不完整）：{'；'.join(read_errors)}")
    if format_mismatch:
        parts.append(
            f"{len(format_mismatch)} 个日志文件全部行无法解析（格式与解析器不匹配，"
            f"错误扫描与关联无证据）：{'；'.join(format_mismatch)}"
        )
    if n_errors > 0:
        prefix = "可解析部分发现" if incomplete else "时间窗口内发现"
        parts.append(f"{prefix} {n_errors} 条 ERROR/FATAL 事件")
        if cats:
            parts.append("分类：" + "、".join(f"{k}×{v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])))
        parts.append("结合发生时间与跨节点关联判断与当前故障的相关性。")
    elif not incomplete:
        # 只在全部日志均成功解析时才可断言「未发现」
        parts.append("时间窗口内未发现 ERROR/FATAL 日志。")
    parts.append("跨节点关联（confirmed/candidate/insufficient）表示两侧日志是否属于同一执行上下文；日志关联用于识别跨节点异常事件，错误签名用于提供可能原因及排查建议，结合运行状态、错误上下文及验证结果定位具体原因。")
    return "。".join(p.rstrip("。") for p in parts) + "。"
