#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Trac 数据收集器 - 收集 Trac Issues 并转换为 IssueData 格式
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Set

from tqdm import tqdm

from ..collect.data_models import IssueData
from .converters import trac_to_issue_data
from .trac_api import TracAPI


class TracCollector:
    """Trac Issue 收集器"""

    def __init__(self, base_url: str, max_workers: int = 10):
        """
        初始化 Trac 收集器

        Args:
            base_url: Trac 基础 URL (e.g., "https://code.djangoproject.com")
            max_workers: 并发线程数
        """
        self.trac_api = TracAPI(base_url)
        self.max_workers = max_workers
        self.base_url = base_url

    def collect_issues(self, issue_numbers: Set[int]) -> Dict[int, IssueData]:
        """
        收集指定的 issues

        Args:
            issue_numbers: Issue 编号集合

        Returns:
            {issue_number: IssueData} 字典
        """
        if not issue_numbers:
            return {}

        issues = {}

        print(f"  从 Trac 收集 {len(issue_numbers)} 个 tickets...")

        # 尝试批量获取
        try:
            ticket_batch = self.trac_api.get_tickets_batch(list(issue_numbers))

            # 转换为 IssueData
            for ticket_id, trac_ticket in ticket_batch.items():
                try:
                    issue_dict = trac_to_issue_data(trac_ticket)
                    # 更新 URL 为正确的 base_url
                    issue_dict["url"] = f"{self.base_url}/ticket/{ticket_id}"
                    issue_data = IssueData.from_dict(issue_dict)
                    issues[ticket_id] = issue_data
                except Exception as e:
                    print(f"    警告: 转换 Trac ticket #{ticket_id} 失败: {e}")

            # 检查是否有遗漏的 tickets
            missing_ids = issue_numbers - set(ticket_batch.keys())
            if missing_ids:
                print(f"    批量获取遗漏 {len(missing_ids)} 个 tickets，逐个获取...")
                self._collect_missing_issues(missing_ids, issues)

        except Exception as e:
            print(f"    警告: 批量获取失败 ({e})，改用逐个获取...")
            self._collect_missing_issues(issue_numbers, issues)

        print(f"  ✓ 成功收集 {len(issues)} 个 Trac tickets")
        return issues

    def _collect_missing_issues(self, issue_numbers: Set[int], issues: Dict[int, IssueData]):
        """
        逐个收集遗漏的 issues

        Args:
            issue_numbers: Issue 编号集合
            issues: 已有的 issues 字典（会被更新）
        """
        with tqdm(total=len(issue_numbers), desc="    获取 Trac tickets", leave=False) as pbar:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self._fetch_single_issue, ticket_id): ticket_id for ticket_id in issue_numbers
                }

                for future in as_completed(futures):
                    ticket_id = futures[future]
                    try:
                        issue_data = future.result()
                        if issue_data:
                            issues[ticket_id] = issue_data
                    except Exception as e:
                        pbar.write(f"      错误处理 Trac ticket #{ticket_id}: {e}")
                    pbar.update(1)

    def _fetch_single_issue(self, ticket_id: int) -> IssueData:
        """
        获取单个 issue

        Args:
            ticket_id: Ticket ID

        Returns:
            IssueData 对象，如果失败则返回 None
        """
        try:
            trac_ticket = self.trac_api.get_ticket(ticket_id)
            if not trac_ticket:
                return None

            issue_dict = trac_to_issue_data(trac_ticket)
            # 更新 URL
            issue_dict["url"] = f"{self.base_url}/ticket/{ticket_id}"
            return IssueData.from_dict(issue_dict)

        except Exception as e:
            print(f"      获取 Trac ticket #{ticket_id} 失败: {e}")
            return None
