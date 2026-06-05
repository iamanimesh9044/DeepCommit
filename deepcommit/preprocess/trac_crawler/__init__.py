#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Trac Issue Tracker 爬虫模块

用于爬取 Django Trac 等使用 Trac 系统的项目的 Issue 数据
"""

from .converters import extract_trac_tickets, trac_to_issue_data
from .trac_api import TracAPI
from .trac_collector import TracCollector

__all__ = [
    "TracAPI",
    "TracCollector",
    "trac_to_issue_data",
    "extract_trac_tickets",
]
