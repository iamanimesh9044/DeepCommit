#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Trac API 封装 - 通过 CSV 接口访问 Trac tickets
"""

import csv
import time
from io import StringIO
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter


class TracAPI:
    """Trac CSV API 操作封装类"""

    def __init__(self, base_url: str):
        """
        初始化 Trac API

        Args:
            base_url: Trac 基础 URL，例如 "https://code.djangoproject.com"
        """
        self.base_url = base_url.rstrip("/")
        self.session = self._make_session()

    def _make_session(self) -> requests.Session:
        """创建 HTTP 会话"""
        s = requests.Session()
        s.headers.update(
            {
                "Accept": "text/csv",
                "User-Agent": "trac-collector",
            }
        )

        # 启用连接池 (不用 HTTPAdapter 内置 retry, 自己控制 retry + backoff)
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    def _get_csv(self, url: str, params=None, retries: int = 5) -> requests.Response:
        """
        发送 GET 请求获取 CSV 数据，支持 429/5xx 重试 + 指数退避

        Args:
            url: 请求 URL
            params: 查询参数 (dict 或 list of tuples)
            retries: 最大重试次数

        Returns:
            Response 对象
        """
        for i in range(retries):
            try:
                r = self.session.get(url, params=params, timeout=30)

                # 429 Too Many Requests — 等待后重试
                if r.status_code == 429:
                    wait = min(2 ** (i + 1), 60)  # 2, 4, 8, 16, 32, max 60s
                    if i < retries - 1:
                        print(f"    429 rate limited, waiting {wait}s (retry {i + 1}/{retries})...")
                        time.sleep(wait)
                        continue
                    r.raise_for_status()

                # 5xx — 短暂等待后重试
                if r.status_code >= 500:
                    if i < retries - 1:
                        time.sleep(1.5 * (i + 1))
                        continue
                    r.raise_for_status()

                r.raise_for_status()
                return r

            except requests.exceptions.ConnectionError:
                if i < retries - 1:
                    time.sleep(2 * (i + 1))
                    continue
                raise

        r.raise_for_status()
        return r

    def _parse_csv_response(self, response: requests.Response) -> List[Dict[str, Any]]:
        """
        解析 CSV 响应

        Args:
            response: requests Response 对象

        Returns:
            字典列表，每个字典代表一行数据
        """
        content = response.text.lstrip("\ufeff")  # Strip UTF-8 BOM
        reader = csv.DictReader(StringIO(content))
        return list(reader)

    def get_ticket(self, ticket_id: int) -> Optional[Dict[str, Any]]:
        """
        获取单个 ticket 的详细信息

        Args:
            ticket_id: Ticket ID

        Returns:
            Ticket 信息字典，如果不存在则返回 None
        """
        url = f"{self.base_url}/ticket/{ticket_id}"
        params = {"format": "csv"}

        try:
            response = self._get_csv(url, params)
            tickets = self._parse_csv_response(response)

            if tickets:
                ticket = tickets[0]
                ticket["id"] = ticket_id
                return ticket
            return None
        except Exception as e:
            print(f"  警告: 无法获取 Trac ticket #{ticket_id}: {e}")
            return None

    def get_tickets_batch(self, ticket_ids: List[int], chunk_size: int = 80) -> Dict[int, Dict[str, Any]]:
        """
        批量获取 tickets（使用 query 接口，分 chunk 避免 URI Too Long）
        每个 chunk 之间加间隔避免触发 rate limit

        Args:
            ticket_ids: Ticket ID 列表
            chunk_size: 每批查询的 ticket 数量（默认 80，避免 414 URI Too Long）

        Returns:
            {ticket_id: ticket_data} 字典
        """
        if not ticket_ids:
            return {}

        result = {}
        chunks = [ticket_ids[i : i + chunk_size] for i in range(0, len(ticket_ids), chunk_size)]
        url = f"{self.base_url}/query"

        for chunk_idx, chunk in enumerate(chunks):
            id_params = [("id", str(tid)) for tid in chunk] + [("format", "csv")]

            # chunk 之间加间隔, 避免触发 rate limit
            if chunk_idx > 0:
                time.sleep(1.0)

            try:
                response = self._get_csv(url, params=id_params)

                tickets = self._parse_csv_response(response)

                for ticket in tickets:
                    try:
                        ticket_id = int(ticket.get("id", 0))
                        if ticket_id > 0:
                            result[ticket_id] = ticket
                    except (ValueError, TypeError):
                        continue

            except Exception as e:
                print(f"  警告: 批量获取 chunk {chunk_idx + 1}/{len(chunks)} 失败: {e}")
                # Fallback: 逐个获取这个 chunk, 带间隔
                for ticket_id in chunk:
                    ticket = self.get_ticket(ticket_id)
                    if ticket:
                        result[ticket_id] = ticket
                    time.sleep(0.5)  # 逐个请求也要限速

        return result

    def check_ticket_exists(self, ticket_id: int) -> bool:
        """
        检查 ticket 是否存在

        Args:
            ticket_id: Ticket ID

        Returns:
            True 如果存在，否则 False
        """
        ticket = self.get_ticket(ticket_id)
        return ticket is not None
