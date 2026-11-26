"""otb-tools CLI 入口。

退出码语义：
  - preflight / inspect（健康检查类）：0 检查完成且无 FAIL（WARN 允许，--strict 时
    WARN 计 1）/ 1 存在 FAIL / 2 存在 ERROR（关键检查未完成或证据不完整）/ 3 参数错误。
  - diagnose（离线诊断类）：0 分析完成 / 2 证据不完整（输入文件读取失败或日志全部
    行无法解析）/ 3 参数错误；--strict 为检测模式：识别到 FAIL/WARN 级签名或
    confirmed 关联时返回 1（表示识别到异常事件，供脚本联动）。
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import timedelta

from . import diagnostics, inspector, preflight
from .models import Report


def _parse_ports(s: str) -> list[int]:
    try:
        ports = [int(p) for p in s.split(",") if p.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"端口列表格式错误: {s}")
    for p in ports:
        if not 1 <= p <= 65535:
            raise argparse.ArgumentTypeError(f"端口超出 1-65535 范围: {p}")
    return ports


def _positive_float(s: str) -> float:
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"需要数字: {s!r}")
    if not math.isfinite(v) or not v > 0:
        raise argparse.ArgumentTypeError(f"需要正数: {s!r}")
    return v


def _positive_int(s: str) -> int:
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"需要整数: {s!r}")
    if v <= 0:
        raise argparse.ArgumentTypeError(f"需要正整数: {s!r}")
    return v


def _ratio(s: str) -> float:
    v = _positive_float(s)
    if v > 1:
        raise argparse.ArgumentTypeError(f"需要在 0-1 之间: {s!r}")
    return v


def _non_negative_float(s: str) -> float:
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"需要数字: {s!r}")
    if not math.isfinite(v) or v < 0:
        raise argparse.ArgumentTypeError(f"需要非负有限数: {s!r}")
    return v


def _parse_env_overrides(s: str) -> dict:
    out = {}
    for pair in s.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise argparse.ArgumentTypeError(f"env 覆盖格式错误: {pair}（应为 NAME=VALUE,...）")
        k, v = pair.split("=", 1)
        out[k.strip()] = v
    return out


def _parse_log_files(s: str) -> dict:
    out = {}
    for pair in s.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" in pair:
            node, path = pair.split("=", 1)
        else:
            node, path = pair, pair
        out[node.strip()] = path.strip()
    return out


def _add_common_output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", choices=["json", "text", "markdown"], default="text",
                   help="输出格式（默认 text）")
    p.add_argument("-o", "--output", help="写入文件（默认 stdout）")
    p.add_argument("--strict", action="store_true",
                   help="WARN 级检查项也计入退出码 1（默认只有 FAIL 级才非零）")


def _emit(report: Report, fmt: str, output: str | None) -> bool:
    """输出报告；写入文件失败时打印错误并返回 False（调用方返回退出码 2）。"""
    if fmt == "json":
        text = report.to_json()
    elif fmt == "markdown":
        text = report.to_markdown()
    else:
        text = report.to_text()
    if output:
        try:
            with open(output, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError as exc:
            print(f"写入 {output} 失败: {exc}", file=sys.stderr)
            return False
    else:
        print(text)
    return True


def _build_preflight_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("preflight", help="部署环境检查（不依赖数据库已启动）")
    p.add_argument("--pg-bin", default=preflight.DEFAULT_PG_BIN)
    p.add_argument("--runtime-libpq", default=preflight.DEFAULT_RUNTIME_LIBPQ)
    p.add_argument("--compat-dirs", default=",".join(preflight.DEFAULT_COMPAT_DIRS))
    p.add_argument("--data-dirs", default=",".join(preflight.DEFAULT_DATA_DIRS))
    p.add_argument("--log-dir", default=preflight.DEFAULT_LOG_DIR)
    p.add_argument("--ports", type=_parse_ports, default=list(preflight.DEFAULT_PORTS))
    p.add_argument("--conf-files", default="")
    p.add_argument("--pg-hba-files", default="")
    p.add_argument("--conn", default="", help="libpq 连接串；提供后追加部署后验证（数据库连接探测），默认为部署前模式、不要求数据库已启动")
    p.add_argument("--expect-running", action="store_true",
                   help="数据库应已在运行（端口未监听/连接拒绝视为 FAIL 而非预期）")
    p.add_argument("--probe-env", type=_parse_env_overrides, default={},
                   help="psql 探测时的环境变量覆盖（如 LD_LIBRARY_PATH=/wrong/path），用于复现依赖故障")
    p.add_argument("--disk-min-free-gb", type=_non_negative_float, default=1.0)
    p.add_argument("--timeout", type=_positive_int, default=10)
    _add_common_output_args(p)


def _cmd_preflight(args) -> int:
    cfg = preflight.PreflightConfig(
        pg_bin=args.pg_bin,
        runtime_libpq=args.runtime_libpq,
        compat_dirs=[d for d in args.compat_dirs.split(",") if d],
        data_dirs=[d for d in args.data_dirs.split(",") if d],
        log_dir=args.log_dir,
        ports=args.ports,
        conf_files=[f for f in args.conf_files.split(",") if f],
        pg_hba_files=[f for f in args.pg_hba_files.split(",") if f],
        conninfo=args.conn,
        expect_running=args.expect_running,
        probe_env=args.probe_env,
        disk_min_free_gb=args.disk_min_free_gb,
        timeout=args.timeout,
    )
    report = preflight.run(cfg, strict=args.strict)
    if not _emit(report, args.format, args.output):
        return 2
    return report.overall_exit_code()


def _build_inspect_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("inspect", help="运行期集群巡检（只读）")
    p.add_argument("--conn", default=inspector.DEFAULT_CONNINFO, help="libpq 连接串（CN）")
    p.add_argument("--ports", type=_parse_ports, default=[], help="数据库不可用时的备选端口列表")
    p.add_argument("--log-dir", default=inspector.DEFAULT_LOG_DIR)
    p.add_argument("--log-files", type=_parse_log_files, default={},
                   help="node=文件名 映射，逗号分隔（默认 cn.log/dn1.log/dn2.log）")
    p.add_argument("--log-window-hours", type=_positive_float, default=24.0)
    p.add_argument("--watermark-threshold", type=_ratio, default=0.8)
    p.add_argument("--skew-max-rows", type=_positive_int, default=inspector.SKEW_MAX_ROWS_DEFAULT,
                   help="reltuples 超过该值的表默认不做精确行数统计")
    p.add_argument("--skew-fail-min-rows", type=_positive_int, default=inspector.SKEW_FAIL_MIN_ROWS_DEFAULT,
                   help="完全倾斜（存在 0 行节点）且总行数达到该值才判 FAIL，否则降级 WARN")
    p.add_argument("--skew-tables", default="",
                   help="逗号分隔的 schema.table 或 table，强制精确统计（覆盖规模阈值）")
    p.add_argument("--timeout", type=_positive_int, default=5)
    _add_common_output_args(p)


def _cmd_inspect(args) -> int:
    cfg = inspector.InspectorConfig(
        conninfo=args.conn,
        ports=args.ports,
        log_dir=args.log_dir,
        log_files=args.log_files or inspector.DEFAULT_LOG_FILES,
        log_window_hours=args.log_window_hours,
        watermark_threshold=args.watermark_threshold,
        skew_max_rows=args.skew_max_rows,
        skew_fail_min_rows=args.skew_fail_min_rows,
        skew_tables=[t.strip() for t in args.skew_tables.split(",") if t.strip()],
        timeout=args.timeout,
    )
    report = inspector.run(cfg, strict=args.strict)
    if not _emit(report, args.format, args.output):
        return 2
    return report.overall_exit_code()


def _build_diagnose_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("diagnose", help="SQLSTATE / 错误文本 / 日志诊断")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--sqlstate", help="如 42704")
    g.add_argument("--text", help="错误文本")
    g.add_argument("--text-file", help="包含错误文本的文件")
    p.add_argument("--log-files", type=_parse_log_files, default={},
                   help="日志文件，逗号分隔；node=path 形式显式标角色（cn*/dn* 参与关联），裸路径仅扫描错误不参与关联")
    p.add_argument("--cn-logs", help="CN 日志（逗号分隔，用于关联）")
    p.add_argument("--dn-logs", help="DN 日志（逗号分隔，用于关联）")
    p.add_argument("--window-hours", type=_positive_float, default=24.0)
    p.add_argument("--strict", action="store_true",
                   help="检测模式：识别到 FAIL/WARN 签名或 confirmed 关联时退出码 1（默认 0=分析完成）")
    p.add_argument("--format", choices=["json", "text"], default="text")
    p.add_argument("-o", "--output")


def _fmt_diagnosis(d: dict, fmt: str) -> str:
    if fmt == "json":
        import json

        return json.dumps(d, ensure_ascii=False, indent=2)
    lines = [f"input: {d.get('input', '')}", f"matched: {d.get('matched')}", f"confidence: {d.get('confidence')}"]
    if "name" in d:
        lines.append(f"sqlstate: {d.get('name')}")
    for m in d.get("matches", []):
        lines.append(f"- signature: {m['id']} ({m['category']}, {m['severity']})")
        lines.append(f"  causes: {'；'.join(m['possible_causes'])}")
        lines.append(f"  next: {'；'.join(m['next_steps'])}")
    for k in ("possible_causes", "next_steps", "otb_note", "conclusion"):
        if k in d:
            lines.append(f"{k}: {d[k]}")
    if "errors" in d:
        lines.append(f"errors_in_window: {d['error_fatal_in_window']}  category_counts={d.get('category_counts')}")
        if d.get("unparsed_files"):
            lines.append(f"unparsed_files: {len(d['unparsed_files'])}")
        for e in d["errors"][:10]:
            lines.append(f"  [{e['time']}] {e['node']} {e['level']}: {e['message'][:100]}")
        lines.append(f"correlations: {len(d['correlations'])}")
        for c in d["correlations"][:10]:
            lines.append(f"  [{c['confidence']}] coord={c['coord']} reason={c['reason']}")
        if d.get("root_cause"):
            lines.append(f"root_cause: determined={d['root_cause']['determined']} note={d['root_cause']['note']}")
    return "\n".join(lines)


def _classify_log_files(mapping: dict) -> tuple[list, list, list]:
    """按显式节点键分类日志：cn*/dn* 参与关联，其余（含裸路径）只扫描。

    裸路径（node==path）一律归 other：禁止按文件名子串猜测节点角色，
    避免把 /var/log/scanner.log 之类误判为 CN 日志。
    """
    cn, dn, other = [], [], []
    for node, path in mapping.items():
        if node == path:
            other.append(path)
        elif node.lower().startswith("cn"):
            cn.append(path)
        elif node.lower().startswith("dn"):
            dn.append(path)
        else:
            other.append(path)
    return cn, dn, other


def _cmd_diagnose(args) -> int:
    has_text = bool(args.sqlstate or args.text or args.text_file)
    has_logs = bool(args.log_files or args.cn_logs or args.dn_logs)
    if not has_text and not has_logs:
        print("需要 --sqlstate / --text / --text-file / --log-files / --cn-logs/--dn-logs 之一", file=sys.stderr)
        return 3
    if has_text and has_logs:
        print("--sqlstate/--text/--text-file 与 --log-files/--cn-logs/--dn-logs 不能同时使用", file=sys.stderr)
        return 3
    if args.sqlstate:
        d = diagnostics.diagnose_sqlstate(args.sqlstate)
    elif args.text:
        d = diagnostics.diagnose_text(args.text)
    elif args.text_file:
        try:
            with open(args.text_file, "r", encoding="utf-8", errors="replace") as fh:
                d = diagnostics.diagnose_text(fh.read())
        except OSError as exc:
            print(f"读取 {args.text_file} 失败: {exc}", file=sys.stderr)
            return 2
    else:
        cn = [f for f in (args.cn_logs or "").split(",") if f]
        dn = [f for f in (args.dn_logs or "").split(",") if f]
        f_cn, f_dn, other = _classify_log_files(args.log_files)
        cn.extend(f_cn)
        dn.extend(f_dn)
        if not cn and not dn and not other:
            print("日志诊断需要 --cn-logs/--dn-logs 或 --log-files", file=sys.stderr)
            return 3
        d = diagnostics.diagnose_logs(
            cn_paths=cn,
            dn_paths=dn,
            window=timedelta(hours=args.window_hours),
            correlate_enabled=True,
            other_paths=other,
        )
    text = _fmt_diagnosis(d, args.format)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError as exc:
            print(f"写入 {args.output} 失败: {exc}", file=sys.stderr)
            return 2
    else:
        print(text)
    if d.get("read_errors") or d.get("unparsed_files"):
        return 2
    if args.strict:
        # 检测模式：签名命中（FAIL/WARN）或 confirmed 关联 → 1。
        # 表示识别到异常事件，或双侧日志属于同一执行上下文。
        sevs = [d.get("severity")] + [m.get("severity") for m in d.get("matches", [])]
        if any(s in ("FAIL", "WARN") for s in sevs if s):
            return 1
        confirmed = [c for c in d.get("correlations", []) if c.get("confidence") == "confirmed"]
        if confirmed:
            return 1
    return 0


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        sys.exit(3)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="otb-tools", description="OpenTenBase Agentic Troubleshooting Toolkit")
    sub = parser.add_subparsers(dest="command", required=True)
    _build_preflight_parser(sub)
    _build_inspect_parser(sub)
    _build_diagnose_parser(sub)
    args = parser.parse_args(argv)
    if args.command == "preflight":
        return _cmd_preflight(args)
    if args.command == "inspect":
        return _cmd_inspect(args)
    return _cmd_diagnose(args)


if __name__ == "__main__":
    sys.exit(main())
