"""目标分发对账领域服务：按同批标识生成可复核的分片对账文件并核对实收数据。

设计要点：
- 一次实际分发冻结成一份批次快照（列定义、字段范围、数据行、分片范围、合计），
  批次编号是签收时唯一可信的「同批」标识。
- 同一口径重发沿用原批次编号；字段范围或数据变化则生成新批次，旧批次作废，
  避免两套口径被当成同一批签收。
- 核对时按批次分别清点应收分片、实收分片、缺失行与多出行，跨批只做对照，
  不合并签收。
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
from typing import Any, Iterable

from app.services.golden_record import GoldenRecordService
from app.store import store

# 批次快照放在下划线开头的内部表里，不进入模块清单、运营概览和通用资源路由。
BATCH_TABLE = "_mdm_sync_recon_batch"
DEFAULT_SHARD_SIZE = 500
KEY_COLUMN = "黄金ID"
VALID_OUTCOMES = (
    "同批可签收",
    "缺片待补",
    "缺行待补",
    "实收明细不符",
    "跨批不可合并签收",
    "批次已作废",
    "批次不存在",
)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:16]


def _cell(value: Any) -> Any:
    """统一单元格取值：空值留空，复合值转成稳定 JSON，避免导出后丢结构。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _csv_cell(value: Any) -> Any:
    """CSV 注入防护：以公式符号开头的值前置单引号。"""
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


class DistributionReconciliationService:
    """分发对账：生成批次快照、导出分片文件、登记实收并核对签收。"""

    def __init__(self) -> None:
        self.golden_service = GoldenRecordService()

    # ────────────────────────────── 公共入口 ──────────────────────────────

    def build_batch(self, sync_id: int, shard_size: int | None = None) -> dict[str, Any]:
        """按同步配置当前的口径生成或续发一份分发对账批次。

        同一配置下，如果列定义和数据快照都没变，重发沿用原批次编号；
        只要列范围或数据有任何变化，就生成新批次编号并作废旧批次，
        防止两种口径混在一起签收。
        """
        sync_cfg = self._sync_config(sync_id)
        if sync_cfg is None:
            return {"ok": False, "reason": f"同步配置 {sync_id} 不存在或已归档"}
        if sync_cfg.get("status") != "同步中":
            return {
                "ok": False,
                "reason": f"同步配置 {sync_cfg.get('配置编号')} 未处于同步中，不能生成分发对账批次",
            }

        columns = self._columns_for(sync_cfg)
        snapshot, data_digest = self._snapshot(sync_cfg.get("黄金实体", ""), columns)
        column_fingerprint = _digest(columns)

        existing = self._active_batch_for_config(str(sync_cfg.get("配置编号", "")))
        if existing and existing.get("列定义指纹") == column_fingerprint \
                and existing.get("数据指纹") == data_digest:
            # 同一口径、同一份数据：重发不得换号，否则客服无法判断前后片是否同一批。
            return {
                "ok": True,
                "resend": True,
                "created": False,
                "message": (
                    f"口径与数据未变化，沿用原批次 {existing['批次编号']} 续发；"
                    f"分片布局保持 {existing['分片总数']} 片不变"
                ),
                "batch": self._batch_detail(existing),
            }

        size = self._normalise_shard_size(shard_size)
        seq = len(self._batches_for_config(str(sync_cfg.get("配置编号", "")))) + 1
        batch_no = self._next_batch_no(str(sync_cfg.get("配置编号", "")), seq)
        shards = self._build_shards(batch_no, snapshot, size)
        batch = {
            "id": self._next_id(),
            "批次编号": batch_no,
            "配置编号": sync_cfg.get("配置编号"),
            "目标系统": sync_cfg.get("目标系统"),
            "黄金实体": sync_cfg.get("黄金实体"),
            "同步方式": sync_cfg.get("同步方式"),
            "字段范围原文": sync_cfg.get("字段范围", ""),
            "列定义": [{"序号": index, "列名": name, "来源字段": name}
                       for index, name in enumerate(columns, start=1)],
            "列定义指纹": column_fingerprint,
            "数据快照": snapshot,
            "数据指纹": data_digest,
            "总行数": len(snapshot),
            "重复黄金ID": self._duplicate_ids(snapshot),
            "分片大小": size,
            "分片总数": len(shards),
            "分片": shards,
            "生成时间": _now(),
            "状态": "待接收",
            "作废原因": "",
            "替代批次": "",
            "重发来源": existing.get("批次编号", "") if existing else "",
            "实收": {},
        }

        superseded: dict[str, Any] | None = None
        if existing:
            existing["状态"] = "已作废"
            existing["替代批次"] = batch_no
            existing["作废原因"] = (
                "字段范围或黄金数据已变化，原批次口径不再可签收"
            )
            superseded = {"batch_no": existing["批次编号"], "reason": existing["作废原因"]}

        store.rows(BATCH_TABLE).append(batch)
        message = (
            f"已生成分发对账批次 {batch_no}：{len(snapshot)} 行 / {len(shards)} 片，"
            f"列定义 {len(columns)} 列"
        )
        if superseded:
            message += f"；原批次 {superseded['batch_no']} 已作废，需按新批次整批重收"
        return {
            "ok": True,
            "resend": False,
            "created": True,
            "message": message,
            "superseded": superseded,
            "batch": self._batch_detail(batch),
        }

    def list_batches(self, sync_id: int | None = None) -> list[dict[str, Any]]:
        """列出对账批次：默认按生成顺序倒序，可按同步配置过滤。"""
        rows = list(store.rows(BATCH_TABLE))
        if sync_id is not None:
            sync_cfg = self._sync_config(sync_id)
            config_no = str(sync_cfg.get("配置编号", "")) if sync_cfg else ""
            rows = [row for row in rows if row.get("配置编号") == config_no]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        return [self._batch_summary(row) for row in rows]

    def get_batch(self, batch_no: str, include_rows: bool = False) -> dict[str, Any] | None:
        batch = self._find_batch(batch_no)
        if batch is None:
            return None
        return self._batch_detail(batch, include_rows=include_rows)

    def verify_batch(self, batch_no: str) -> dict[str, Any]:
        """按一次实际分发复核批次自洽性：分片范围、合计与列定义必须对得上。"""
        batch = self._find_batch(batch_no)
        if batch is None:
            return {"ok": False, "reason": f"批次 {batch_no} 不存在"}
        columns = [item["列名"] for item in batch.get("列定义", [])]
        shards = batch.get("分片", [])
        rows = batch.get("数据快照", [])
        checks = {
            "列定义非空": bool(columns),
            "列定义指纹一致": _digest(columns) == batch.get("列定义指纹"),
            "数据指纹一致": _digest([columns, rows]) == batch.get("数据指纹"),
            "分片行数合计等于总行数": sum(int(s.get("行数", 0)) for s in shards) == int(batch.get("总行数", 0)),
            "分片范围连续无重叠": self._shards_contiguous(shards),
            "分片数一致": len(shards) == int(batch.get("分片总数", 0)),
            "黄金ID不重复": not batch.get("重复黄金ID"),
        }
        return {
            "ok": all(checks.values()),
            "batch_no": batch_no,
            "checks": checks,
            "总行数": batch.get("总行数"),
            "分片总数": batch.get("分片总数"),
            "列定义": columns,
            "列定义指纹": batch.get("列定义指纹"),
        }

    # ────────────────────────────── 实收登记与核对 ──────────────────────────────

    def acknowledge(self, receipts: Iterable[dict[str, Any]],
                    default_batch_no: str | None = None) -> dict[str, Any]:
        """登记一个或多个文件片段的实收情况，返回登记结果与当前核对结论。"""
        normalised, errors = self._normalise_receipts(receipts, default_batch_no)
        if errors:
            return {"ok": False, "outcome": "批次不存在", "message": "；".join(errors),
                    "errors": errors, "receipts": []}
        for receipt in normalised:
            batch = receipt["_batch"]
            batch.setdefault("实收", {})[str(receipt["分片序号"])] = {
                "行数": receipt["行数"],
                "黄金ID": receipt["黄金ID"],
                "逐行明细": receipt["逐行明细"],
                "登记时间": _now(),
            }
        result = self.reconcile(normalised, persist=False)
        result["receipts"] = [self._receipt_public(item) for item in normalised]
        result["message"] = "实收登记完成；" + result["message"]
        return result

    def reconcile(self, receipts: Iterable[dict[str, Any]] | None = None,
                  persist: bool = True,
                  default_batch_no: str | None = None) -> dict[str, Any]:
        """核对实收数据：清点应收分片、缺失分片、缺失行与本次可确认的批次编号。

        receipts 为空时，从批次里已登记的实收记录汇总；显式传入时只核对本次片段。
        """
        if receipts is None:
            normalised, errors = self._stored_receipts(default_batch_no)
        else:
            normalised, errors = self._normalise_receipts(receipts, default_batch_no)
        if errors:
            return self._result("批次不存在", [], None, "；".join(errors), errors=errors)
        if not normalised:
            return self._result("缺片待补", [], None, "尚未登记任何实收分片，无法签收")

        by_batch: dict[str, list[dict[str, Any]]] = {}
        for receipt in normalised:
            by_batch.setdefault(receipt["批次编号"], []).append(receipt)

        batch_reports: dict[str, dict[str, Any]] = {}
        expected_shards: list[dict[str, Any]] = []
        received_shards: list[dict[str, Any]] = []
        missing_shards: list[dict[str, Any]] = []
        missing_ids: list[str] = []
        extra_ids: list[str] = []
        duplicate_ids: list[str] = []

        for batch_no, items in by_batch.items():
            batch = self._find_batch(batch_no)
            if batch is None:
                errors.append(f"批次 {batch_no} 不存在")
                continue
            report = self._reconcile_batch(batch, items)
            batch_reports[batch_no] = report
            expected_shards.extend(report["应收分片"])
            received_shards.extend(report["实收分片"])
            missing_shards.extend(report["缺失分片"])
            missing_ids.extend(report["缺失黄金ID"])
            extra_ids.extend(report["多出黄金ID"])
            duplicate_ids.extend(report["重复黄金ID"])

        if errors:
            return self._result("批次不存在", [], None, "；".join(errors), errors=errors,
                                batch_reports=batch_reports)

        column_fingerprints = {
            report["列定义指纹"] for report in batch_reports.values()
        }
        superseded = [
            report["批次编号"] for report in batch_reports.values()
            if report["批次状态"] == "已作废"
        ]
        confirmable = [
            report["批次编号"] for report in batch_reports.values()
            if report["完整"] and report["批次状态"] != "已作废"
        ]
        incomplete = [
            report["批次编号"] for report in batch_reports.values()
            if not report["完整"]
        ]

        if len(batch_reports) > 1:
            outcome = "跨批不可合并签收"
            replacement_note = ""
            if superseded:
                replacements = [
                    f"{report['批次编号']}→{report.get('替代批次') or '新批次'}"
                    for report in batch_reports.values() if report["批次状态"] == "已作废"
                ]
                replacement_note = f"；已作废批次需按替代批次整批重收：{'、'.join(replacements)}"
            if len(column_fingerprints) > 1:
                message = (
                    "本次实收片段来自不同批次且列定义不同，不能作为同一批签收；"
                    f"可分别确认的批次：{'、'.join(confirmable) or '无'}；"
                    f"未收齐的批次：{'、'.join(incomplete) or '无'}"
                    f"{replacement_note}"
                )
            else:
                message = (
                    "本次实收片段跨多个批次编号，即使列定义相同也需按批次分别签收；"
                    f"可分别确认的批次：{'、'.join(confirmable) or '无'}"
                    f"{replacement_note}"
                )
        else:
            report = next(iter(batch_reports.values()))
            outcome, message = self._single_batch_outcome(report, confirmable)

        if superseded and not confirmable and len(batch_reports) == 1:
            outcome = "批次已作废"
            batch = self._find_batch(superseded[0])
            replacement = batch.get("替代批次") if batch else ""
            replacement_batch = self._find_batch(replacement) if replacement else None
            replacement_hint = ""
            if replacement_batch:
                replacement_hint = (
                    f"；替代批次 {replacement} 列为 "
                    f"{'/'.join(item['列名'] for item in replacement_batch.get('列定义', []))}，"
                    "需整批接收后签收"
                )
            elif replacement:
                replacement_hint = f"；替代批次 {replacement} 需整批重新接收"
            message = (
                f"批次 {superseded[0]} 已作废，不能签收"
                f"{replacement_hint or '；请重新生成批次后整批接收'}"
            )

        if persist:
            for receipt in normalised:
                receipt["_batch"].setdefault("实收", {})[str(receipt["分片序号"])] = {
                    "行数": receipt["行数"],
                    "黄金ID": receipt["黄金ID"],
                    "逐行明细": receipt["逐行明细"],
                    "登记时间": _now(),
                }

        total_expected = sum(int(report["应收行数"]) for report in batch_reports.values())
        total_received = sum(int(report["实收行数"]) for report in batch_reports.values())
        return {
            "ok": outcome == "同批可签收",
            "outcome": outcome,
            "确认批次编号": confirmable[0] if len(confirmable) == 1 and len(batch_reports) == 1 else None,
            "可确认批次编号": confirmable,
            "不可确认批次编号": incomplete,
            "列定义指纹": sorted(column_fingerprints),
            "列定义一致": len(column_fingerprints) <= 1,
            "应收分片": expected_shards,
            "实收分片": received_shards,
            "缺失分片": missing_shards,
            "合计": {
                "应收行数": total_expected,
                "实收行数": total_received,
                "缺失行数": len(missing_ids),
                "多出行数": len(extra_ids),
            },
            "缺失黄金ID": missing_ids,
            "多出黄金ID": extra_ids,
            "重复黄金ID": sorted(set(duplicate_ids)),
            "批次核对": batch_reports,
            "message": message,
        }

    def check_sync(self, sync_id: int) -> dict[str, Any]:
        """核对某同步配置下所有批次的已登记实收，用于续发后整体签收判断。"""
        sync_cfg = self._sync_config(sync_id)
        if sync_cfg is None:
            return self._result("批次不存在", [], None, f"同步配置 {sync_id} 不存在")
        config_no = str(sync_cfg.get("配置编号", ""))
        batches = [
            batch for batch in store.rows(BATCH_TABLE)
            if batch.get("配置编号") == config_no
        ]
        if not batches:
            return self._result(
                "缺片待补", [], None,
                f"同步配置 {config_no} 尚未生成分发对账批次，请先生成批次",
            )
        # 整体签收以当前有效批次为准；作废批次的旧片段只在当前批次未收齐时参与对照，
        # 这样「前半批旧口径 + 后半批新口径」会明确判为跨批不可合并签收。
        active = [batch for batch in batches if batch.get("状态") != "已作废"]
        active.sort(key=lambda batch: int(batch.get("id", 0)))
        if not active:
            return self._result(
                "批次已作废", [], None,
                f"同步配置 {config_no} 的批次均已作废，请重新生成批次后再签收",
            )
        latest = active[-1]
        latest_receipts = self._receipts_of(latest)
        if latest_receipts:
            latest_result = self.reconcile(
                latest_receipts, persist=False, default_batch_no=str(latest.get("批次编号", "")))
            if latest_result["outcome"] == "同批可签收":
                # 新批次可签收时，仍把被替代的旧批次列进对照，方便客服区分前后片口径。
                for batch in batches:
                    if batch.get("状态") == "已作废":
                        latest_result["批次核对"][str(batch.get("批次编号"))] = (
                            self._reconcile_batch(batch, self._normalise_receipts(
                                self._receipts_of(batch), None)[0]))
                return latest_result
        superseded_receipts: list[dict[str, Any]] = []
        for batch in batches:
            if batch.get("状态") == "已作废":
                superseded_receipts.extend(self._receipts_of(batch))
        if superseded_receipts:
            combined = superseded_receipts + latest_receipts
            result = self.reconcile(combined, persist=False,
                                    default_batch_no=str(latest.get("批次编号", "")))
            if str(latest.get("批次编号")) not in result["批次核对"]:
                result["message"] += (
                    f"；新批次 {latest.get('批次编号')}（列定义 "
                    f"{'/'.join(item['列名'] for item in latest.get('列定义', []))}）尚未登记实收，需整批接收"
                )
            return result
        return self.reconcile(None, persist=False,
                              default_batch_no=str(latest.get("批次编号", "")))

    def _receipts_of(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        for shard_no, stored in sorted((batch.get("实收", {}) or {}).items()):
            receipts.append({
                "batch_no": batch.get("批次编号"),
                "shard_no": shard_no,
                "row_count": stored.get("行数"),
                "golden_ids": stored.get("黄金ID"),
            })
        return receipts

    # ────────────────────────────── 导出 ──────────────────────────────

    def export_csv(self, batch_no: str, shard_no: int | None = None) -> dict[str, Any]:
        """导出对账 CSV：文件头带批次、列定义、分片范围与合计，数据行带分片定位列。"""
        batch = self._find_batch(batch_no)
        if batch is None:
            return {"ok": False, "reason": f"批次 {batch_no} 不存在"}
        shards = batch.get("分片", [])
        if shard_no is not None:
            shards = [s for s in shards if int(s.get("分片序号", 0)) == int(shard_no)]
            if not shards:
                return {"ok": False, "reason": f"批次 {batch_no} 没有分片 {shard_no}"}
        shard_nos = {int(s["分片序号"]) for s in shards}
        columns = [item["列名"] for item in batch.get("列定义", [])]
        row_count = sum(int(s.get("行数", 0)) for s in shards)
        start = min((int(s["起始行"]) for s in shards), default=0)
        end = max((int(s["结束行"]) for s in shards), default=0)

        output = io.StringIO()
        output.write("﻿")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["# 分发对账文件"])
        writer.writerow(["# 批次编号", _csv_cell(batch.get("批次编号"))])
        writer.writerow(["# 配置编号", _csv_cell(batch.get("配置编号"))])
        writer.writerow(["# 目标系统", _csv_cell(batch.get("目标系统"))])
        writer.writerow(["# 黄金实体", _csv_cell(batch.get("黄金实体"))])
        writer.writerow(["# 列定义指纹", batch.get("列定义指纹")])
        writer.writerow(["# 列定义", "|".join(columns)])
        writer.writerow(["# 字段范围", _csv_cell(batch.get("字段范围原文"))])
        writer.writerow(["# 分片范围", f"{start}-{end}", f"共{len(shards)}片", f"共{row_count}行"])
        writer.writerow(["# 批次合计", f"总行数={batch.get('总行数')}", f"分片总数={batch.get('分片总数')}"])
        writer.writerow(["# 生成时间", _csv_cell(batch.get("生成时间"))])
        writer.writerow(["# 核对说明", "请按批次编号+列定义指纹核对实收；跨批次片段不可合并签收"])
        header = ["批次编号", "列定义指纹", "分片编号", "分片序号", "分片起始行",
                  "分片结束行", "行号", *columns]
        writer.writerow(header)
        rows = batch.get("数据快照", [])
        for shard in shards:
            shard_no_value = int(shard["分片序号"])
            begin = int(shard["起始行"])
            finish = int(shard["结束行"])
            for offset, row in enumerate(rows[begin - 1:finish], start=begin):
                writer.writerow([
                    _csv_cell(batch.get("批次编号")),
                    batch.get("列定义指纹"),
                    _csv_cell(shard.get("分片编号")),
                    shard_no_value,
                    begin,
                    finish,
                    offset,
                    *[_csv_cell(row.get(column, "")) for column in columns],
                ])
        return {
            "ok": True,
            "filename": f"{batch_no}-S{shard_no:02d}.csv" if shard_no is not None else f"{batch_no}.csv",
            "content": output.getvalue(),
            "batch_no": batch_no,
            "shard_no": shard_no,
        }

    # ────────────────────────────── 内部实现 ──────────────────────────────

    def _sync_config(self, sync_id: int) -> dict[str, Any] | None:
        return store.find("mdm_sync", sync_id)

    @staticmethod
    def _normalise_shard_size(value: Any) -> int:
        try:
            size = int(value)
        except (TypeError, ValueError):
            return DEFAULT_SHARD_SIZE
        return size if size > 0 else DEFAULT_SHARD_SIZE

    @staticmethod
    def _columns_for(sync_cfg: dict[str, Any]) -> list[str]:
        raw = str(sync_cfg.get("字段范围", "") or "")
        fields = [item.strip() for item in raw.split(",") if item.strip()]
        columns: list[str] = []
        for name in [KEY_COLUMN, *fields]:
            if name not in columns:
                columns.append(name)
        return columns

    def _snapshot(self, entity_model: str, columns: list[str]) -> tuple[list[dict[str, Any]], str]:
        source = self.golden_service.export_full_golden_records(
            entity_model=entity_model, only_active=True)
        projected = [
            {column: _cell(row.get(column, "")) for column in columns}
            for row in source
        ]
        projected.sort(key=lambda row: str(row.get(KEY_COLUMN, "")))
        return projected, _digest([columns, projected])

    @staticmethod
    def _duplicate_ids(rows: list[dict[str, Any]]) -> list[str]:
        seen: set[str] = set()
        duplicated: set[str] = set()
        for row in rows:
            golden_id = str(row.get(KEY_COLUMN, ""))
            if golden_id in seen:
                duplicated.add(golden_id)
            seen.add(golden_id)
        return sorted(duplicated)

    def _build_shards(self, batch_no: str, rows: list[dict[str, Any]],
                      size: int) -> list[dict[str, Any]]:
        shards: list[dict[str, Any]] = []
        total = len(rows)
        if total == 0:
            return shards
        index = 1
        for start in range(1, total + 1, size):
            finish = min(start + size - 1, total)
            chunk = rows[start - 1:finish]
            shards.append({
                "分片编号": f"{batch_no}-S{index:02d}",
                "分片序号": index,
                "起始行": start,
                "结束行": finish,
                "行数": len(chunk),
                "黄金ID范围": [str(row.get(KEY_COLUMN, "")) for row in chunk],
                "摘要": _digest(chunk),
            })
            index += 1
        return shards

    @staticmethod
    def _shards_contiguous(shards: list[dict[str, Any]]) -> bool:
        expected_start = 1
        for shard in shards:
            if int(shard.get("起始行", 0)) != expected_start:
                return False
            if int(shard.get("结束行", 0)) < int(shard.get("起始行", 0)):
                return False
            expected_start = int(shard.get("结束行", 0)) + 1
        return True

    def _next_id(self) -> int:
        rows = store.rows(BATCH_TABLE)
        return max((int(row.get("id", 0)) for row in rows), default=0) + 1

    @staticmethod
    def _next_batch_no(config_no: str, seq: int) -> str:
        safe = config_no.strip() or "SYNC"
        return f"DIST-{safe}-{seq:04d}"

    def _batches_for_config(self, config_no: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(BATCH_TABLE)
            if row.get("配置编号") == config_no
        ]

    def _active_batch_for_config(self, config_no: str) -> dict[str, Any] | None:
        active = [
            row for row in self._batches_for_config(config_no)
            if row.get("状态") != "已作废"
        ]
        if not active:
            return None
        return max(active, key=lambda row: int(row.get("id", 0)))

    def _find_batch(self, batch_no: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_TABLE):
            if row.get("批次编号") == batch_no:
                return row
        return None

    def _batch_summary(self, batch: dict[str, Any]) -> dict[str, Any]:
        receipts = batch.get("实收", {}) or {}
        received = len(receipts)
        return {
            "批次编号": batch.get("批次编号"),
            "配置编号": batch.get("配置编号"),
            "目标系统": batch.get("目标系统"),
            "黄金实体": batch.get("黄金实体"),
            "列定义指纹": batch.get("列定义指纹"),
            "列定义": [item["列名"] for item in batch.get("列定义", [])],
            "字段范围原文": batch.get("字段范围原文"),
            "总行数": batch.get("总行数"),
            "分片大小": batch.get("分片大小"),
            "分片总数": batch.get("分片总数"),
            "已登记分片数": received,
            "生成时间": batch.get("生成时间"),
            "状态": batch.get("状态"),
            "替代批次": batch.get("替代批次", ""),
            "重发来源": batch.get("重发来源", ""),
            "自检": self.verify_batch(str(batch.get("批次编号"))),
        }

    def _batch_detail(self, batch: dict[str, Any], include_rows: bool = False) -> dict[str, Any]:
        detail = self._batch_summary(batch)
        detail["分片"] = [dict(shard) for shard in batch.get("分片", [])]
        detail["重复黄金ID"] = list(batch.get("重复黄金ID", []))
        detail["实收"] = [
            {"分片序号": int(key), **value}
            for key, value in sorted((batch.get("实收", {}) or {}).items(), key=lambda kv: int(kv[0]))
        ]
        if include_rows:
            detail["数据快照"] = list(batch.get("数据快照", []))
        return detail

    @staticmethod
    def _receipt_public(receipt: dict[str, Any]) -> dict[str, Any]:
        return {
            "批次编号": receipt["批次编号"],
            "分片编号": receipt["分片编号"],
            "分片序号": receipt["分片序号"],
            "行数": receipt["行数"],
            "黄金ID": receipt["黄金ID"],
            "逐行明细": receipt["逐行明细"],
        }

    def _normalise_receipts(self, receipts: Iterable[dict[str, Any]],
                            default_batch_no: str | None) -> tuple[list[dict[str, Any]], list[str]]:
        normalised: list[dict[str, Any]] = []
        errors: list[str] = []
        for raw in receipts or []:
            if not isinstance(raw, dict):
                errors.append("实收片段格式不正确")
                continue
            batch_no = str(
                raw.get("batch_no") or raw.get("批次编号") or default_batch_no or ""
            ).strip()
            if not batch_no:
                errors.append("实收片段缺少批次编号")
                continue
            batch = self._find_batch(batch_no)
            if batch is None:
                errors.append(f"批次 {batch_no} 不存在")
                continue
            shard_ref = raw.get("shard_no", raw.get("分片序号"))
            shard = self._resolve_shard(batch, shard_ref, raw)
            if shard is None:
                errors.append(f"批次 {batch_no} 未登记分片 {shard_ref}")
                continue
            golden_ids_raw = raw.get("golden_ids", raw.get("黄金ID"))
            if golden_ids_raw is None and isinstance(raw.get("rows"), list):
                golden_ids_raw = [
                    row.get(KEY_COLUMN) for row in raw["rows"] if isinstance(row, dict)
                ]
            if golden_ids_raw is None:
                golden_ids = list(shard.get("黄金ID范围", []))
                detail_complete = False
            elif isinstance(golden_ids_raw, str):
                golden_ids = [item.strip() for item in golden_ids_raw.split(",") if item.strip()]
                detail_complete = True
            elif isinstance(golden_ids_raw, (list, tuple, set)):
                golden_ids = [str(item) for item in golden_ids_raw]
                detail_complete = True
            else:
                golden_ids = [str(golden_ids_raw)]
                detail_complete = True
            claimed = raw.get("row_count", raw.get("行数"))
            try:
                row_count = int(claimed) if claimed is not None else len(golden_ids)
            except (TypeError, ValueError):
                row_count = len(golden_ids)
            normalised.append({
                "批次编号": batch_no,
                "分片编号": shard.get("分片编号"),
                "分片序号": int(shard.get("分片序号", 0)),
                "行数": row_count,
                "黄金ID": golden_ids,
                "逐行明细": detail_complete,
                "_batch": batch,
                "_shard": shard,
            })
        return normalised, errors

    @staticmethod
    def _resolve_shard(batch: dict[str, Any], shard_ref: Any,
                       raw: dict[str, Any]) -> dict[str, Any] | None:
        for shard in batch.get("分片", []):
            if shard_ref is not None and str(shard_ref) in (
                    str(shard.get("分片序号")), str(shard.get("分片编号"))):
                return shard
            if shard_ref is None and raw.get("分片编号") == shard.get("分片编号"):
                return shard
        return None

    def _stored_receipts(self, default_batch_no: str | None) -> tuple[list[dict[str, Any]], list[str]]:
        if default_batch_no:
            found = self._find_batch(default_batch_no)
            if found is None:
                return [], [f"批次 {default_batch_no} 不存在"]
            # 指定批次时只核对该批次的已登记实收，不把作废批次的旧片段混进来。
            batches = [found]
        else:
            batches = list(store.rows(BATCH_TABLE))
        receipts: list[dict[str, Any]] = []
        for batch in batches:
            for shard_no, stored in sorted((batch.get("实收", {}) or {}).items()):
                receipts.append({
                    "batch_no": batch.get("批次编号"),
                    "shard_no": shard_no,
                    "row_count": stored.get("行数"),
                    "golden_ids": stored.get("黄金ID"),
                })
        if not receipts:
            return [], []
        return self._normalise_receipts(receipts, None)

    def _reconcile_batch(self, batch: dict[str, Any],
                         receipts: list[dict[str, Any]]) -> dict[str, Any]:
        shards = list(batch.get("分片", []))
        shard_by_no = {int(shard.get("分片序号", 0)): shard for shard in shards}
        received_by_no: dict[int, dict[str, Any]] = {}
        duplicate_ids: set[str] = set()
        seen_ids: set[str] = set()
        for receipt in receipts:
            shard_no = int(receipt["分片序号"])
            if shard_no in received_by_no:
                # 同一分片重复登记以最后一次为准，但把重复行暴露出来。
                duplicate_ids.update(receipt["黄金ID"])
            received_by_no[shard_no] = receipt
            for golden_id in receipt["黄金ID"]:
                if golden_id in seen_ids:
                    duplicate_ids.add(golden_id)
                seen_ids.add(golden_id)

        all_expected_ids = [str(row.get(KEY_COLUMN, "")) for row in batch.get("数据快照", [])]
        expected_id_set = set(all_expected_ids)
        received_ids: list[str] = []
        expected_shards: list[dict[str, Any]] = []
        received_shards: list[dict[str, Any]] = []
        missing_shards: list[dict[str, Any]] = []
        missing_ids: list[str] = []
        extra_ids: set[str] = set()
        shard_ok = True

        for shard in shards:
            shard_no = int(shard.get("分片序号", 0))
            expected_ids = [str(item) for item in shard.get("黄金ID范围", [])]
            info = {
                "批次编号": batch.get("批次编号"),
                "分片编号": shard.get("分片编号"),
                "分片序号": shard_no,
                "起始行": shard.get("起始行"),
                "结束行": shard.get("结束行"),
                "行数": shard.get("行数"),
                "黄金ID": expected_ids,
            }
            expected_shards.append(info)
            receipt = received_by_no.get(shard_no)
            if receipt is None:
                missing_shards.append(info)
                missing_ids.extend(expected_ids)
                shard_ok = False
                continue
            got = [str(item) for item in receipt["黄金ID"]]
            received_ids.extend(got)
            got_set = set(got)
            shard_missing = [item for item in expected_ids if item not in got_set]
            shard_extra = sorted(got_set - set(expected_ids))
            extra_ids.update(shard_extra)
            missing_ids.extend(shard_missing)
            shard_complete = not shard_missing and not shard_extra \
                and receipt["逐行明细"]
            if shard_missing or shard_extra:
                shard_ok = False
            received_shards.append({
                **info,
                "实收行数": receipt["行数"],
                "明细完整": receipt["逐行明细"],
                "缺失黄金ID": shard_missing,
                "多出黄金ID": shard_extra,
                "状态": "已接收" if shard_complete else "实收明细不符",
            })

        received_id_set = set(received_ids)
        missing_ids = [item for item in all_expected_ids if item not in received_id_set]
        extra_ids.update(received_id_set - expected_id_set)
        if duplicate_ids:
            shard_ok = False
        duplicate_ids.update(batch.get("重复黄金ID", []))
        complete = (
            shard_ok
            and len(received_by_no) == len(shards)
            and not missing_ids
            and not extra_ids
            and not duplicate_ids
        )
        return {
            "批次编号": batch.get("批次编号"),
            "批次状态": batch.get("状态"),
            "列定义": [item["列名"] for item in batch.get("列定义", [])],
            "列定义指纹": batch.get("列定义指纹"),
            "替代批次": batch.get("替代批次", ""),
            "应收分片": expected_shards,
            "实收分片": received_shards,
            "缺失分片": missing_shards,
            "应收行数": int(batch.get("总行数", 0)),
            "实收行数": len(received_id_set) if all(
                item["逐行明细"] for item in received_by_no.values()) else sum(
                    int(item["行数"]) for item in received_by_no.values()),
            "缺失黄金ID": missing_ids,
            "多出黄金ID": sorted(extra_ids),
            "重复黄金ID": sorted(duplicate_ids),
            "完整": complete,
        }

    @staticmethod
    def _single_batch_outcome(report: dict[str, Any], confirmable: list[str]) -> tuple[str, str]:
        batch_no = report["批次编号"]
        if report["批次状态"] == "已作废":
            return "批次已作废", (
                f"批次 {batch_no} 已作废，不能签收；"
                f"请按替代批次 {report.get('替代批次') or '新批次'} 整批重新接收"
            )
        if report["完整"]:
            return "同批可签收", f"批次 {batch_no} 应收 {report['应收行数']} 行、实收一致，可确认签收"
        missing_shards = [item["分片编号"] for item in report["缺失分片"]]
        if missing_shards:
            return "缺片待补", (
                f"批次 {batch_no} 缺失分片：{'、'.join(missing_shards)}；"
                f"本次不可确认批次编号，补齐后再签收"
            )
        return "缺行待补", (
            f"批次 {batch_no} 分片已到齐但行数据不一致：缺失 {len(report['缺失黄金ID'])} 行、"
            f"多出 {len(report['多出黄金ID'])} 行、重复 {len(report['重复黄金ID'])} 行"
        )

    @staticmethod
    def _result(outcome: str, confirmable: list[str], batch_no: str | None,
                message: str, *, errors: list[str] | None = None,
                batch_reports: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "outcome": outcome,
            "确认批次编号": batch_no,
            "可确认批次编号": confirmable,
            "不可确认批次编号": [],
            "列定义指纹": [],
            "列定义一致": True,
            "应收分片": [],
            "实收分片": [],
            "缺失分片": [],
            "合计": {"应收行数": 0, "实收行数": 0, "缺失行数": 0, "多出行数": 0},
            "缺失黄金ID": [],
            "多出黄金ID": [],
            "重复黄金ID": [],
            "批次核对": batch_reports or {},
            "errors": errors or [],
            "message": message,
        }


distribution_reconciliation = DistributionReconciliationService()
