"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class DistributionReceipt(BaseModel):
    """客服登记的一个实收文件片段：按批次编号+分片序号定位，可带黄金ID明细。"""

    batch_no: str | None = None
    shard_no: int | str | None = None
    row_count: int | None = None
    golden_ids: list[str] | None = None
    rows: list[dict[str, Any]] | None = None


class DistributionAckPayload(BaseModel):
    default_batch_no: str | None = None
    receipts: list[DistributionReceipt] = []


class DistributionBuildPayload(BaseModel):
    shard_size: int | None = None
