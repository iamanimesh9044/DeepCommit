#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Trac 数据转换器 - 将 Trac ticket 数据转换为 IssueData 格式
"""

import re
from datetime import datetime
from typing import Any, Dict, List


def trac_to_issue_data(trac_ticket: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 Trac ticket 转换为 IssueData 兼容的字典格式

    Trac CSV 字段映射:
    - id -> number
    - summary -> title
    - description -> body
    - status -> state (closed/fixed -> "closed", 其他 -> "open")
    - reporter -> author/user
    - type, component -> labels
    - time -> created_at
    - changetime -> updated_at

    Args:
        trac_ticket: Trac ticket 原始数据字典

    Returns:
        IssueData 兼容的字典
    """
    # 提取基本字段
    ticket_id = int(trac_ticket.get("id", 0))
    summary = trac_ticket.get("summary", "")
    description = trac_ticket.get("description", "")
    status = trac_ticket.get("status", "").lower()
    reporter = trac_ticket.get("reporter", "")

    # 状态映射: Trac 的 closed/fixed/wontfix/invalid/duplicate 等都映射为 "closed"
    # 其他状态 (new/assigned/accepted/reopened) 映射为 "open"
    closed_statuses = {"closed", "fixed", "wontfix", "invalid", "duplicate"}
    state = "closed" if status in closed_statuses else "open"

    # 关闭原因映射
    state_reason = None
    if status == "wontfix":
        state_reason = "not_planned"
    elif status in {"fixed", "closed"}:
        state_reason = "completed"

    # 提取标签 (type + component + keywords)
    labels = []
    ticket_type = trac_ticket.get("type", "")
    if ticket_type:
        labels.append(f"type:{ticket_type}")

    component = trac_ticket.get("component", "")
    if component:
        labels.append(f"component:{component}")

    keywords = trac_ticket.get("keywords", "")
    if keywords:
        # keywords 可能是逗号分隔的
        for kw in keywords.split(","):
            kw = kw.strip()
            if kw:
                labels.append(kw)

    # 时间字段
    created_at = _parse_trac_time(trac_ticket.get("time", ""))
    updated_at = _parse_trac_time(trac_ticket.get("changetime", ""))
    closed_at = None
    if state == "closed":
        # Trac 没有单独的 closed_at 字段，使用 changetime
        closed_at = updated_at

    # 构建 IssueData 兼容字典
    issue_data = {
        "number": ticket_id,
        "title": summary,
        "body": description,
        "state": state,
        "author": reporter,
        "user": reporter,
        "labels": labels,
        "created_at": created_at,
        "updated_at": updated_at,
        "closed_at": closed_at,
        "comments_count": 0,  # Trac CSV 不提供评论数
        # 新增字段
        "author_association": "NONE",  # Trac 不区分角色
        "url": _build_trac_url(trac_ticket),
        "state_reason": state_reason,
        "assignees": _extract_assignees(trac_ticket),
        "milestone": trac_ticket.get("milestone", None),
        "closed_by": trac_ticket.get("owner", None) if state == "closed" else None,
        "locked": False,
        "active_lock_reason": None,
        "reactions": {},
        "conversations": {},
        "events": [],
    }

    return issue_data


def _parse_trac_time(time_str: str) -> str:
    """
    解析 Trac 时间格式为 ISO 8601 格式

    Trac 时间格式示例:
    - "2023-10-15T14:30:00Z"
    - "10/15/23 14:30:00"

    Args:
        time_str: Trac 时间字符串

    Returns:
        ISO 8601 格式时间字符串
    """
    if not time_str:
        return None

    # 如果已经是 ISO 格式，直接返回
    if "T" in time_str and time_str.endswith("Z"):
        return time_str

    # 尝试解析常见格式
    try:
        # 格式1: MM/DD/YY HH:MM:SS
        if "/" in time_str:
            dt = datetime.strptime(time_str, "%m/%d/%y %H:%M:%S")
            return dt.isoformat() + "Z"

        # 格式2: YYYY-MM-DD HH:MM:SS
        if "-" in time_str and " " in time_str:
            dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            return dt.isoformat() + "Z"

        # 格式3: Unix timestamp
        if time_str.isdigit():
            dt = datetime.fromtimestamp(int(time_str))
            return dt.isoformat() + "Z"

    except Exception:
        pass

    # 无法解析，返回原始字符串
    return time_str


def _build_trac_url(trac_ticket: Dict[str, Any]) -> str:
    """
    构建 Trac ticket URL

    Args:
        trac_ticket: Trac ticket 数据

    Returns:
        Ticket URL 字符串
    """
    # 从环境或配置中获取 base_url，这里暂时使用硬编码
    # TODO: 从配置传入
    base_url = "https://code.djangoproject.com"

    ticket_id = trac_ticket.get("id", "")
    if ticket_id:
        return f"{base_url}/ticket/{ticket_id}"

    return base_url


def _extract_assignees(trac_ticket: Dict[str, Any]) -> List[str]:
    """
    提取 assignees

    Args:
        trac_ticket: Trac ticket 数据

    Returns:
        Assignees 列表
    """
    owner = trac_ticket.get("owner", "")
    if owner and owner != "nobody":
        return [owner]
    return []


def extract_trac_tickets(message: str) -> List[int]:
    """
    从 commit message 中提取 Trac ticket 编号

    Django commit message 格式:
    - "Fixed #36213 -- Added support for..."
    - "Refs #34619 -- Updated documentation"
    - "[5.1.x] Fixed #35123 -- Backport..."

    Args:
        message: Commit message

    Returns:
        Ticket ID 列表
    """
    ticket_ids = []

    # 模式1: Fixed #12345 -- ...
    # 模式2: Refs #12345 -- ...
    # 模式3: Ref #12345 -- ...
    pattern = r"(?:Fixed|Refs?)\s*#(\d+)"

    matches = re.findall(pattern, message, re.IGNORECASE)
    for match in matches:
        try:
            ticket_ids.append(int(match))
        except ValueError:
            continue

    return ticket_ids
