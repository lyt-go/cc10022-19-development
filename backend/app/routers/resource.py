"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import (
    ActionPayload,
    ActionResult,
    DistributionAckPayload,
    DistributionBuildPayload,
    EntryPayload,
    PageResult,
)
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


def require_reconciliation(module: str):
    """只有主数据分发同步提供目标分发对账能力。"""
    if module != "mdm_sync":
        raise HTTPException(
            status_code=404,
            detail=f"模块 {module} 未提供分发对账接口",
        )
    return require_service(module)


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


# ── 目标分发对账：固定路径必须先于 /{module}/{entry_id} 注册，避免被当成 entry_id ──

@router.post("/{module}/distribution-batches", response_model=dict)
def build_distribution_batch(module: str, payload: DistributionBuildPayload,
                             sync_id: int = Query(...)) -> dict[str, Any]:
    """按同步配置的字段范围生成（或同口径重发）分发对账批次。"""
    service = require_reconciliation(module)
    return service.build_distribution_batch(sync_id, shard_size=payload.shard_size)


@router.get("/{module}/distribution-batches", response_model=list[dict])
def list_distribution_batches(module: str, sync_id: int | None = Query(default=None)) -> list[dict]:
    """列出对账批次，可按同步配置过滤。"""
    service = require_reconciliation(module)
    return service.list_distribution_batches(sync_id)


@router.get("/{module}/distribution-batches/{batch_no}", response_model=dict)
def get_distribution_batch(module: str, batch_no: str) -> dict[str, Any]:
    service = require_reconciliation(module)
    batch = service.get_distribution_batch(batch_no)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"对账批次 {batch_no} 不存在")
    return batch


@router.get("/{module}/distribution-batches/{batch_no}/verify", response_model=dict)
def verify_distribution_batch(module: str, batch_no: str) -> dict[str, Any]:
    """复核批次分片范围、合计与列定义是否自洽。"""
    service = require_reconciliation(module)
    return service.verify_distribution_batch(batch_no)


@router.get("/{module}/distribution-batches/{batch_no}/export")
def export_distribution_batch(module: str, batch_no: str,
                              shard_no: int | None = Query(default=None)) -> StreamingResponse:
    """导出对账 CSV；带分片序号时只导出该片段。"""
    service = require_reconciliation(module)
    result = service.export_distribution_file(batch_no, shard_no=shard_no)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "导出失败"))
    filename = str(result.get("filename", f"{batch_no}.csv"))
    return StreamingResponse(
        iter([result["content"]]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{module}/distribution-batches/{batch_no}/receipts", response_model=dict)
def acknowledge_distribution_receipts(module: str, batch_no: str,
                                      payload: DistributionAckPayload) -> dict[str, Any]:
    """登记实收片段并立即返回应收分片、缺失片与可确认批次编号。"""
    service = require_reconciliation(module)
    receipts = [item.model_dump() for item in payload.receipts]
    return service.acknowledge_distribution_receipts(
        receipts, default_batch_no=payload.default_batch_no or batch_no)


@router.get("/{module}/distribution-batches/{batch_no}/reconcile", response_model=dict)
def reconcile_distribution_batch(module: str, batch_no: str) -> dict[str, Any]:
    """按已登记实收复核单个批次。"""
    service = require_reconciliation(module)
    return service.reconcile_distribution(default_batch_no=batch_no)


@router.get("/{module}/distribution-batches/{batch_no}/receipts", response_model=dict)
def list_distribution_receipts(module: str, batch_no: str) -> dict[str, Any]:
    service = require_reconciliation(module)
    batch = service.get_distribution_batch(batch_no)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"对账批次 {batch_no} 不存在")
    return {"批次编号": batch_no, "实收": batch.get("实收", []), "分片总数": batch.get("分片总数")}


@router.post("/{module}/{entry_id}/distribution-reconcile", response_model=dict)
def reconcile_sync_distribution(module: str, entry_id: int,
                                payload: DistributionAckPayload | None = None) -> dict[str, Any]:
    """续发后按同步配置整体核对：判断前后片能否一起签收。"""
    service = require_reconciliation(module)
    if payload and payload.receipts:
        receipts = [item.model_dump() for item in payload.receipts]
        return service.reconcile_distribution(
            receipts, default_batch_no=payload.default_batch_no)
    return service.check_distribution_signoff(entry_id)


@router.patch("/{module}/{entry_id}/distribution-scope", response_model=dict)
def update_distribution_scope(module: str, entry_id: int,
                              payload: EntryPayload) -> dict[str, Any]:
    """更正同步配置的字段范围等口径；不会改写已发批次，续发时才判定是否换批。"""
    service = require_reconciliation(module)
    return service.update_sync_scope(entry_id, payload.values)


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
