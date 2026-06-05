"""
Commit SHA Utilities

提供 commit SHA 相关的工具函数，用于标准化和匹配操作。
"""

from typing import Optional

# 默认的 SHA 标准化长度
DEFAULT_SHA_LENGTH = 7


def normalize_commit_sha(sha: str, length: int = DEFAULT_SHA_LENGTH) -> str:
    """
    标准化 commit SHA（取前N位并转为小写）

    Args:
        sha: 完整的 commit SHA
        length: 标准化后的长度（默认 7）

    Returns:
        标准化后的 SHA（小写）

    Example:
        >>> normalize_commit_sha("a1b2c3d4e5f6")
        'a1b2c3d'
        >>> normalize_commit_sha("A1B2C3D")
        'a1b2c3d'
    """
    return sha[:length].lower()


def sha_match(sha1: str, sha2: str, min_length: int = DEFAULT_SHA_LENGTH) -> bool:
    """
    检查两个 SHA 是否匹配（支持短格式）

    Args:
        sha1: 第一个 SHA
        sha2: 第二个 SHA
        min_length: 最小匹配长度（默认 7）

    Returns:
        True 如果两个 SHA 匹配

    Example:
        >>> sha_match("a1b2c3d", "a1b2c3d4e5f6")
        True
        >>> sha_match("a1b2c3d", "xyz1234")
        False
    """
    if not sha1 or not sha2:
        return False

    # 验证两个 SHA 是否有效
    if not validate_commit_sha(sha1, min_length=min_length):
        return False
    if not validate_commit_sha(sha2, min_length=min_length):
        return False

    # 使用较短的长度进行比较
    short_len = min(len(sha1), len(sha2), min_length)
    return sha1[:short_len].lower() == sha2[:short_len].lower()


def find_sha_in_set(target_sha: str, sha_set: set, exact: bool = False) -> Optional[str]:
    """
    在 SHA 集合中查找匹配的 SHA

    Args:
        target_sha: 要查找的 SHA
        sha_set: SHA 集合
        exact: 是否要求精确匹配（默认 False，支持前缀匹配）

    Returns:
        匹配的 SHA 或 None

    Example:
        >>> shas = {"a1b2c3d4e5f6", "xyz1234"}
        >>> find_sha_in_set("a1b2c3d", shas)
        'a1b2c3d4e5f6'
    """
    if exact:
        return target_sha if target_sha in sha_set else None

    for sha in sha_set:
        if sha_match(sha, target_sha):
            return sha

    return None


def batch_normalize_commits(commits: list, length: int = DEFAULT_SHA_LENGTH) -> list:
    """
    批量标准化 commit SHA 列表

    Args:
        commits: commit SHA 列表
        length: 标准化后的长度（默认 7）

    Returns:
        标准化后的 SHA 列表

    Example:
        >>> batch_normalize_commits(["A1B2C3D4", "XYZ1234567"])
        ['a1b2c3d', 'xyz1234']
    """
    return [normalize_commit_sha(c, length) for c in commits]


def validate_commit_sha(sha: str, min_length: int = 7, max_length: int = 40) -> bool:
    """
    验证 commit SHA 格式是否有效

    Args:
        sha: 要验证的 SHA
        min_length: 最小长度（默认 7）
        max_length: 最大长度（默认 40，Git SHA-1 长度）

    Returns:
        True 如果 SHA 格式有效

    Example:
        >>> validate_commit_sha("a1b2c3d")
        True
        >>> validate_commit_sha("xyz")
        False
        >>> validate_commit_sha("not-a-sha!")
        False
    """
    if not sha:
        return False

    if not (min_length <= len(sha) <= max_length):
        return False

    # 检查是否只包含十六进制字符
    try:
        int(sha, 16)
        return True
    except ValueError:
        return False
