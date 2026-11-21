"""黄金记录领域服务：黄金记录创建、关联来源、置信度计算与版本管理。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 运行期内部标记，不属于黄金记录业务字段，导出分发数据时必须剔掉。
GOLDEN_INTERNAL_KEYS = frozenset({"id", "status", "pending", "abnormal", "source_count"})


class GoldenRecordService(BaseModuleService):
    MODULE = "golden_record"
    ENTITY = "黄金记录"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["黄金ID", "实体模型", "关联来源", "关键字段", "代表字段", "置信度", "最后合并", "记录版本"]
    REQUIRED_FIELDS = ["黄金ID", "实体模型", "关联来源"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用记录": "已停用"}
    NEGATIVE_ACTIONS = ["停用记录"]

    def create_golden_record(self, entity_model: str, merged_data: dict[str, Any],
                               source_ids: list[str]) -> dict[str, Any]:
        """创建黄金记录：将合并后的代表记录写入黄金记录存储。

        写入黄金记录到golden_records存储，关联来源批次与合并追溯信息，
        设置置信度、关联来源和最后合并时间，生成唯一黄金ID。
        """
        rows = store.rows(self.MODULE)
        golden_id = self._next_golden_id(entity_model)
        confidence = self._compute_confidence(len(source_ids))
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "黄金ID": golden_id,
            "实体模型": entity_model,
            "关联来源": "|".join(source_ids),
            "关键字段": ",".join(k for k in merged_data if not k.startswith("_")),
            "代表字段": str(merged_data.get("representative", {})),
            "置信度": f"{confidence:.2f}",
            "最后合并": datetime.datetime.utcnow().isoformat(),
            "记录版本": "1.0",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "source_count": len(source_ids),
        }
        for key, val in merged_data.items():
            if not key.startswith("_"):
                entry[key] = val
        rows.append(entry)
        # 写入黄金记录到golden_records存储，关联来源批次与合并追溯信息
        return {"ok": True, "golden_record": entry}

    def _next_golden_id(self, entity_model: str) -> str:
        """生成唯一黄金ID：同一秒内创建多条记录也不能撞号，否则对账无法按行定位。"""
        rows = store.rows(self.MODULE)
        existing = {str(row.get("黄金ID", "")) for row in rows}
        prefix = f"GR-{entity_model}-"
        serial = max(
            (int(str(rid)[len(prefix):]) for rid in existing
             if str(rid).startswith(prefix) and str(rid)[len(prefix):].isdigit()),
            default=0,
        )
        while True:
            serial += 1
            candidate = f"{prefix}{serial:06d}"
            if candidate not in existing:
                return candidate

    def _compute_confidence(self, source_count: int) -> float:
        """计算黄金记录置信度：基于来源数量和质量指标综合评估。"""
        base = 0.5
        source_bonus = min(source_count * 0.1, 0.3)
        return min(round(base + source_bonus, 2), 0.99)

    def query_golden_record(self, golden_id: str) -> dict[str, Any] | None:
        """查询黄金记录：按黄金ID检索golden_records存储。

        查询黄金记录的完整字段信息、关联来源列表和置信度评分，
        返回黄金记录的完整快照数据。
        """
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("黄金ID") == golden_id:
                # 查询黄金记录完整信息：包含关联来源和置信度
                return dict(row)
        return None

    def link_source_to_golden(self, golden_id: str, new_source_id: str) -> dict[str, Any]:
        """关联新来源记录到已有黄金记录。

        将新增来源记录关联到已有黄金记录，更新关联来源列表，
        重新计算置信度评分并更新最后合并时间。
        """
        record = self.query_golden_record(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        sources = record.get("关联来源", "").split("|")
        if "" in sources:
            sources.remove("")
        if new_source_id in sources:
            return {"ok": False, "reason": "来源已关联"}
        sources.append(new_source_id)
        record["关联来源"] = "|".join(sources)
        record["source_count"] = len(sources)
        record["置信度"] = f"{self._compute_confidence(len(sources)):.2f}"
        # 关联新来源记录：更新黄金记录的来源列表和置信度
        return {"ok": True, "golden_id": golden_id, "source_count": len(sources)}

    def validate_golden_completeness(self, golden_id: str,
                                      required_fields: list[str]) -> dict[str, Any]:
        """校验黄金记录的字段完整性：检查所有必填字段是否已填充。

        查询黄金记录数据，读取golden_records存储中的字段值，
        校验必填字段完整性并输出完备率统计。
        """
        record = self.query_golden_record(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        missing = [f for f in required_fields if not str(record.get(f, "")).strip()]
        complete_count = len(required_fields) - len(missing)
        return {
            "golden_id": golden_id,
            "total_required": len(required_fields),
            "filled": complete_count,
            "missing": missing,
            "completeness": round(complete_count / max(len(required_fields), 1) * 100, 2),
        }

    def search_golden_records(self, entity_model: str | None = None,
                                min_confidence: float = 0.0) -> list[dict[str, Any]]:
        """搜索黄金记录：按实体模型和最低置信度筛选。

        查询golden_records存储，按实体模型筛选并过滤置信度
        低于阈值的记录，返回符合条件的黄金记录列表。
        """
        rows = store.rows(self.MODULE)
        results = [
            row for row in rows
            if row.get("status") == "已生效"
        ]
        if entity_model:
            results = [r for r in results if r.get("实体模型") == entity_model]
        results = [r for r in results if float(r.get("置信度", "0")) >= min_confidence]
        # 查询黄金记录：筛选已生效、匹配实体模型且置信度达标的记录
        results.sort(key=lambda r: float(r.get("置信度", "0")), reverse=True)
        return results

    def export_golden_records(self, entity_model: str) -> list[dict[str, Any]]:
        """导出黄金记录：将指定实体模型的黄金记录导出供下游系统消费。

        查询golden_records存储中已生效的黄金记录，导出完整的
        记录数据，包含关键字段、代表字段和置信度信息。
        """
        records = self.search_golden_records(entity_model=entity_model, min_confidence=0.5)
        # 导出黄金记录：将已生效记录数据导出供下游系统分发同步
        exported = []
        for r in records:
            exported.append({
                "golden_id": r.get("黄金ID"),
                "entity_model": r.get("实体模型"),
                "confidence": float(r.get("置信度", "0")),
                "sources": (r.get("关联来源", "")).split("|"),
                "key_fields": (r.get("关键字段", "")).split(","),
                # 对账文件要按实际字段取数，完整字段值随导出一起下发
                "fields": self._business_fields(r),
            })
        return exported

    def export_full_golden_records(self, entity_model: str | None = None,
                                   only_active: bool = True) -> list[dict[str, Any]]:
        """导出黄金记录完整数据：保留除内部运行标记外的全部字段值。

        分发对账文件需要按黄金ID逐行核对，因此这里不裁剪字段，
        只去掉 id/status/pending 等内部标记，并按黄金ID稳定排序，
        保证同一批快照每次生成的分片范围和摘要都不变。
        """
        rows = store.rows(self.MODULE)
        if entity_model:
            rows = [row for row in rows if row.get("实体模型") == entity_model]
        if only_active:
            rows = [row for row in rows if row.get("status") == "已生效"]
        ordered = sorted(rows, key=lambda row: str(row.get("黄金ID", "")))
        return [self._business_fields(row) for row in ordered]

    @staticmethod
    def _business_fields(row: dict[str, Any]) -> dict[str, Any]:
        """取出记录的完整业务字段，滤掉内部运行标记。"""
        return {
            key: value for key, value in row.items()
            if key not in GOLDEN_INTERNAL_KEYS
        }