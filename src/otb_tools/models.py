"""统一结果模型与退出码语义。

退出码约定（CLI 全子命令一致）：
  0  执行成功且未发现 FAIL 级问题（WARN 允许存在，不触发非零）
  1  发现明确的 FAIL（健康检查判定异常；诊断命中 FAIL 级确认签名/确认关联）
  2  工具执行失败或关键检查未完成（存在 ERROR 项，优先级最高）
  3  参数错误
  --strict 模式下 WARN 也触发退出码 1（ERROR 仍为 2，FAIL 仍为 1）。

历史 ERROR/FATAL 日志仅因存在不构成 FAIL；必须结合时间窗口与跨节点确认。
子进程自身的退出码（如共享库缺失的 127）只作为证据记录，不透传为工具退出码。

JSON 输出同时提供 execution_status（工具是否执行完成）与 health_status（目标是否健康），
避免把"工具执行失败"与"数据库不健康"混为一谈。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Status(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIP = "SKIP"


@dataclass
class Item:
    """单项检查结果。facts=已确认事实，causes=可能原因，hypotheses=待验证假设。"""

    id: str
    name: str
    status: Status
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    source: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    causes: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    fix_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "source": self.source,
            "facts": self.facts,
            "possible_causes": self.causes,
            "hypotheses": self.hypotheses,
            "fix_hint": self.fix_hint,
        }


@dataclass
class Correlation:
    """跨节点日志关联结果。confidence: confirmed / candidate / insufficient。

    注意：confidence 只回答"两侧日志是否属于同一执行上下文"（关联可靠性），
    不回答"故障根因是否已确认"（诊断证据）。关联 confirmed 不等于根因确定。
    """

    coord: str
    cn_entry: dict
    dn_entries: list[dict]
    confidence: str
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "coord": self.coord,
            "confidence": self.confidence,
            "reason": self.reason,
            "cn_entry": self.cn_entry,
            "dn_entries": self.dn_entries,
        }


@dataclass
class Report:
    command: str
    items: list[Item] = field(default_factory=list)
    correlations: list[Correlation] = field(default_factory=list)
    strict: bool = False
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def overall_exit_code(self) -> int:
        codes = [i.status for i in self.items]
        if Status.ERROR in codes:
            return 2
        if Status.FAIL in codes:
            return 1
        if self.strict and Status.WARN in codes:
            return 1
        return 0

    def execution_status(self) -> str:
        """工具是否执行完成：存在 ERROR（关键检查未完成/证据不完整）即 failed。"""
        return "failed" if any(i.status == Status.ERROR for i in self.items) else "ok"

    def health_status(self) -> str:
        """目标是否健康。ERROR 时无法判定为 unknown；FAIL→failed；WARN→degraded。"""
        codes = [i.status for i in self.items]
        if Status.ERROR in codes:
            return "unknown"
        if Status.FAIL in codes:
            return "failed"
        if Status.WARN in codes:
            return "degraded"
        return "healthy"

    def summary(self) -> dict:
        counts = {s.value: 0 for s in Status}
        for i in self.items:
            counts[i.status.value] += 1
        return counts

    def to_dict(self) -> dict:
        out = {
            "command": self.command,
            "exit_code": self.overall_exit_code(),
            "execution_status": self.execution_status(),
            "health_status": self.health_status(),
            "strict": self.strict,
            "generated_at": self.generated_at,
            "summary": self.summary(),
            "items": [i.to_dict() for i in self.items],
        }
        if self.correlations:
            out["correlations"] = [c.to_dict() for c in self.correlations]
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def to_text(self) -> str:
        lines = [
            f"== {self.command} ==",
            f"exit_code={self.overall_exit_code()}  execution={self.execution_status()}  health={self.health_status()}  summary={self.summary()}",
        ]
        for i in self.items:
            lines.append(f"[{i.status.value}] {i.id}  {i.summary}")
            for e in i.evidence[:6]:
                lines.append(f"        evidence: {e}")
            if i.fix_hint:
                lines.append(f"        fix: {i.fix_hint}")
        if self.correlations:
            lines.append("-- correlations --")
            for c in self.correlations:
                lines.append(f"  [{c.confidence}] {c.coord}: {c.reason}")
        return "\n".join(lines)

    def to_markdown(self) -> str:
        lines = [
            f"# {self.command} 报告",
            "",
            f"**退出码**: {self.overall_exit_code()}　**执行状态**: {self.execution_status()}　**健康状态**: {self.health_status()}",
            "",
        ]
        counts = self.summary()
        lines.append(f"PASS {counts['PASS']} / WARN {counts['WARN']} / FAIL {counts['FAIL']} / ERROR {counts['ERROR']} / SKIP {counts['SKIP']}")
        lines.append("")
        lines.append("| 状态 | 检查项 | 结论 |")
        lines.append("|---|---|---|")
        for i in self.items:
            lines.append(f"| {i.status.value} | {i.id} | {i.summary} |")
        for i in self.items:
            if i.evidence or i.causes or i.fix_hint:
                lines.append("")
                lines.append(f"### {i.id}")
                if i.evidence:
                    lines.append("证据：")
                    for e in i.evidence[:8]:
                        lines.append(f"- `{e}`")
                if i.causes:
                    lines.append("可能原因：" + "；".join(i.causes))
                if i.fix_hint:
                    lines.append(f"建议：{i.fix_hint}")
        if self.correlations:
            lines.append("")
            lines.append("## 跨节点日志关联（关联确认 ≠ 根因确认）")
            lines.append("")
            lines.append("| 置信度 | coord | 说明 |")
            lines.append("|---|---|---|")
            for c in self.correlations:
                lines.append(f"| {c.confidence} | {c.coord} | {c.reason} |")
        return "\n".join(lines)
