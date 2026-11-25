"""数据库连接封装：psycopg2 惰性导入，会话强制只读，凭证脱敏。"""

from __future__ import annotations

import re
import urllib.parse

READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
_DSN_PASSWORD_RE = re.compile(
    r"(?i)(?<!\S)(password\s*=\s*)(?:"
    r"'(?:(?:\\.)|[^'\\])*'|"
    r'"(?:(?:\\.)|[^"\\])*"|'
    r"(?:\\.|[^\s])*)"
)


def redact_conninfo(conninfo: str) -> str:
    """脱敏连接串中的密码，支持 key=value 与 postgresql:// URI 两种形式。"""
    if conninfo.startswith(("postgresql://", "postgres://")):
        try:
            parts = urllib.parse.urlsplit(conninfo)
            netloc = parts.netloc
            if "@" in netloc:
                userinfo, host = netloc.rsplit("@", 1)
                username, separator, _password = userinfo.partition(":")
                netloc = f"{username}{':***' if separator else ''}@{host}"
            query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            query = [
                (key, "***" if key.lower() in {"password", "sslpassword"} else value)
                for key, value in query
            ]
            return urllib.parse.urlunsplit(
                (parts.scheme, netloc, parts.path, urllib.parse.urlencode(query), "")
            )
        except ValueError:
            # A malformed URI may still contain a password. Never echo it verbatim.
            return "<redacted connection URI>"

    def redact_dsn(match: re.Match) -> str:
        prefix = match.group(1)
        value = match.group(0)[len(prefix):]
        if value.startswith(("'", '"')):
            return f"{prefix}{value[0]}***{value[0]}"
        return f"{prefix}***"

    return _DSN_PASSWORD_RE.sub(redact_dsn, conninfo)


def connect(conninfo: str, timeout: int = 5, **overrides):
    """建立只读会话。psycopg2 未安装时抛 ImportError，由调用方降级为 ERROR 项。"""
    import psycopg2  # 惰性导入：preflight 离线检查不依赖数据库驱动

    options = f"{READ_ONLY_OPTIONS} -c statement_timeout={timeout * 1000}"
    conn = psycopg2.connect(
        conninfo,
        connect_timeout=timeout,
        options=options,
        **overrides,
    )
    # Each inspection statement gets its own read-only transaction. A failed
    # catalog probe must not poison every later check with SQLSTATE 25P02.
    conn.autocommit = True
    return conn


def run_sql(conn, sql: str, params=None):
    """执行单条只读 SQL，返回 (行列表, 列名)。失败抛 RuntimeError(消息)。"""
    with conn.cursor() as cur:
        try:
            cur.execute(sql, params)
        except Exception as exc:  # 统一转消息，保留原始错误文本
            raise RuntimeError(str(exc).strip()) from exc
        cols = [d[0] for d in (cur.description or [])]
        try:
            rows = cur.fetchall()
        except Exception as exc:
            raise RuntimeError(str(exc).strip()) from exc
    return rows, cols
