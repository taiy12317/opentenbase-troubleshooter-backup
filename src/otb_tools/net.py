"""本机端口监听与远程 TCP 可达性探测（preflight / inspect 共用）。

- ss_listening_ports：解析 `ss -ltn`，返回所有监听端口（含仅绑定特定地址的行）。
- tcp_port_open：对远程注册节点做 TCP connect 探测（本机节点走 ss，避免误连）。
"""

from __future__ import annotations

import re
import socket
import subprocess

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _run(cmd: list[str], timeout: int = 5) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except OSError as exc:
        return 126, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out after {timeout}s"


def ss_listening_ports() -> set[int]:
    """解析 `ss -ltn` 中的监听端口。ss 不可用时抛 OSError。"""
    rc, out, err = _run(["ss", "-ltn"], timeout=5)
    if rc != 0:
        raise OSError(f"ss 不可用: {err or out}")
    ports: set[int] = set()
    for line in out.splitlines():
        m = re.search(r":(\d+)\s", line)
        if m:
            ports.add(int(m.group(1)))
    return ports


def is_local_host(host: str) -> bool:
    return (host or "").lower() in LOCAL_HOSTS


def tcp_port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    """TCP connect 探测：握手完成即认为可达。超时/拒绝返回 False。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
