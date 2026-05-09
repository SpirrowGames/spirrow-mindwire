"""Phanthand HTTP client integration.

The watcher uses Phanthand (``SpirrowGames/spirrow-phanthand``) as its
read-only file-access layer (architecture.md §6.5). MCP tool wrappers
that expose ``read_file`` / ``list_dir`` / etc. to claude-code sit on
top of :class:`PhanthandClient` and arrive in a later sub-PR.
"""

from __future__ import annotations

from .client import (
    PhanthandAPIError,
    PhanthandClient,
    PhanthandError,
    PhanthandHTTPError,
)
from .models import (
    ApiResponse,
    FileExistsData,
    FileInfoData,
    FileListData,
    FileListEntry,
    FileReadData,
    FileSearchData,
    FileTreeData,
    HealthData,
    TreeNode,
)

__all__ = [
    "ApiResponse",
    "FileExistsData",
    "FileInfoData",
    "FileListData",
    "FileListEntry",
    "FileReadData",
    "FileSearchData",
    "FileTreeData",
    "HealthData",
    "PhanthandAPIError",
    "PhanthandClient",
    "PhanthandError",
    "PhanthandHTTPError",
    "TreeNode",
]
