"""Response shapes returned by the Phanthand HTTP API.

Mirrors the public contracts of ``SpirrowGames/spirrow-phanthand``
(see its ``phanthand/models.py``). We keep our own copy rather than
depending on the Phanthand package because:

- Phanthand is a server, not a library — installing it as a runtime
  dependency would pull FastAPI / Starlette / uvicorn for nothing.
- The on-the-wire contract is a small, stable surface; mirroring it
  here gives us a single review point if Phanthand evolves.

``extra='allow'`` on response models so a forward-compatible
Phanthand release adding new fields doesn't break MindWire.
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class _LooseModel(BaseModel):
    """Allow unknown fields so future Phanthand additions don't break us."""

    model_config = ConfigDict(extra="allow")


class ApiResponse(_LooseModel, Generic[T]):
    """Phanthand's standard envelope: ``{success, data, error}``."""

    success: bool = True
    data: T | None = None
    error: str | None = None


class HealthData(_LooseModel):
    status: str
    version: str
    hostname: str
    uptime_seconds: float


class FileReadData(_LooseModel):
    path: str
    content: str
    size: int
    encoding: str


class FileListEntry(_LooseModel):
    name: str
    path: str
    is_dir: bool
    size: int | None = None


class FileListData(_LooseModel):
    path: str
    entries: list[FileListEntry]
    count: int


class FileExistsData(_LooseModel):
    path: str
    exists: bool
    is_file: bool
    is_dir: bool


class FileInfoData(_LooseModel):
    path: str
    name: str
    size: int
    created: datetime | None = None
    modified: datetime | None = None
    is_file: bool
    is_dir: bool
    readonly: bool


class TreeNode(_LooseModel):
    name: str
    path: str
    is_dir: bool
    children: list[TreeNode] | None = None


class FileTreeData(_LooseModel):
    path: str
    tree: TreeNode


class FileSearchData(_LooseModel):
    path: str
    pattern: str
    matches: list[str]
    count: int
    truncated: bool


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
    "TreeNode",
]
