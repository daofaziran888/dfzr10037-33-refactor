"""数据血缘领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。

血缘确认会同步驱动图谱演化（待处理清单）和图谱比对（待审草稿）。
两处产物都读同一份「方向证据」，不再各自回头读链路当前字段。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.lineage_evidence import (
    DISPATCH_MODULE,
    EVIDENCE_MODULE,
    as_text,
    display,
    is_missing,
    link_key,
    projection,
    snapshot,
    view,
)
from app.store import store

# 补投缺件是重放动作，不改变链路自身状态，因此不放进 ACTION_RULES。
REPLAY_ACTIONS = frozenset({
    "补投", "补投缺件", "重新投递", "重投", "重新补投", "重试", "重试投递", "补发缺件",
})


class DataLineageService(BaseModuleService):
    MODULE = "data_lineage"
    ENTITY = "血缘链路"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["链路编号", "上游实体", "下游实体", "转换描述", "关联字段", "影响层级", "链路类型", "最后更新"]
    REQUIRED_FIELDS = ["链路编号", "上游实体", "下游实体"]
    STATUS_ORDER = ["自动推导", "已确认", "待验证", "已废弃"]
    ACTION_RULES = {"确认链路": "已确认", "提交验证": "待验证", "废弃链路": "已废弃"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        normalized = (action or "").strip()
        if normalized in REPLAY_ACTIONS:
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            messages = self._replay_deliveries(entry)
            if not messages:
                return entry, "没有需要补投的缺件"
            return entry, "补投完成：" + "；".join(messages)

        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if entry.get("status") not in ("已确认", "待验证"):
            return entry, message

        # 一次确认只冻结一份方向证据；演化和图谱比对都从这份证据取值，
        # 不再各自读链路当前字段，避免同次确认的两个产物取到不同版本。
        evidence = self._evidence_for(entry)
        # 链路 L12: data_lineage → graph_evolve（血缘变更记录触发演化记录）
        # 链路 L13: data_lineage → graph_compare（血缘链路触发图谱版本比对）
        for target, deliver in (
            ("graph_evolve", self._push_to_graph_evolve),
            ("graph_compare", self._push_to_graph_compare),
        ):
            try:
                message += "；" + deliver(evidence)
            except Exception as exc:  # 单侧中断不能拖垮另一侧，留给补投恢复
                self._record_dispatch(evidence, target, status="待补投", note=str(exc))
                message += f"；{target} 写入中断，已登记待补投"
        return entry, message

    # ===== 方向证据 =====
    def _evidence_rows(self) -> list[dict[str, Any]]:
        return store.rows(EVIDENCE_MODULE)

    def _dispatch_rows(self) -> list[dict[str, Any]]:
        return store.rows(DISPATCH_MODULE)

    def _evidence_for(self, entry: dict[str, Any]) -> dict[str, Any]:
        """取当前确认对应的方向证据。

        指纹只由冻结的方向字段决定，因此：
        - 同一次确认重复投递 → 命中同一份证据，不新增；
        - 关联字段被改写后再次确认 → 冻结值变化，生成新证据版本。
        不会出现「一边沿用旧关联字段、一边用新方向」的错配。
        """
        frozen = snapshot(entry)
        link_no = as_text(entry.get("链路编号"))
        rows = self._evidence_rows()
        for row in rows:
            if (
                row.get("指纹") == frozen["fingerprint"]
                and row.get("来源记录id") == entry.get("id")
                and row.get("链路编号") == link_no
            ):
                return row

        same_link = [
            row for row in rows
            if row.get("链路编号") == link_no and row.get("来源记录id") == entry.get("id")
        ]
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        evidence: dict[str, Any] = {
            "id": next_id,
            "证据编号": f"LINE-EVID-{next_id:04d}",
            "链路编号": link_no,
            "来源记录id": entry.get("id"),
            "确认序号": max((int(row.get("确认序号", 0)) for row in same_link), default=0) + 1,
            "指纹": frozen["fingerprint"],
            "缺失字段": list(frozen["missing"]),
            "方向": "上游实体→下游实体",
        }
        evidence.update(frozen["raw"])
        rows.append(evidence)
        return evidence

    def extract_direction_evidence(self, entry: dict[str, Any]) -> dict[str, Any]:
        """提取（并冻结）一条血缘确认的方向证据数据。"""
        return view(self._evidence_for(entry))

    # 兼容不同调用习惯的别名：收拢后的方向转换入口只有一个。
    build_direction_evidence = extract_direction_evidence
    _direction_evidence = extract_direction_evidence

    def _coerce_evidence(self, entry: dict[str, Any]) -> dict[str, Any]:
        """内部投递入口既接受证据行，也兼容传入链路行的旧调用方式。"""
        if "证据编号" in entry and "确认序号" in entry:
            return entry
        return self._evidence_for(entry)

    # ===== 投递台账 =====
    def _dispatch_for(self, evidence_no: str, target_module: str) -> dict[str, Any] | None:
        for row in self._dispatch_rows():
            if row.get("证据编号") == evidence_no and row.get("目标模块") == target_module:
                return row
        return None

    def _record_alive(self, target_module: str, record_id: Any) -> bool:
        if record_id in (None, ""):
            return False
        try:
            return store.find(target_module, int(record_id)) is not None
        except (TypeError, ValueError):
            return False

    def _record_dispatch(
        self,
        evidence: dict[str, Any],
        target_module: str,
        *,
        record: dict[str, Any] | None = None,
        status: str = "已投递",
        note: str = "",
    ) -> dict[str, Any]:
        row = self._dispatch_for(str(evidence.get("证据编号")), target_module)
        if row is None:
            rows = self._dispatch_rows()
            next_id = max((int(item.get("id", 0)) for item in rows), default=0) + 1
            row = {
                "id": next_id,
                "投递编号": f"LINE-DLV-{next_id:04d}",
                "证据编号": evidence.get("证据编号"),
                "链路编号": evidence.get("链路编号"),
                "确认序号": evidence.get("确认序号"),
                "目标模块": target_module,
            }
            rows.append(row)
        if record is not None:
            row["记录id"] = record.get("id")
            row["记录编号"] = record.get(
                "记录编号" if target_module == "graph_evolve" else "任务编号")
        row["状态"] = status
        row["缺失字段"] = list(evidence.get("缺失字段") or [])
        row["失败原因"] = note
        return row

    def list_deliveries(self, link_no: str) -> list[dict[str, Any]]:
        """按链路编号查询实际发生过的投递，用于补投时回查所用版本。"""
        return [dict(row) for row in self._dispatch_rows() if row.get("链路编号") == link_no]

    # ===== 图谱演化 =====
    def _push_to_graph_evolve(self, source: dict[str, Any]) -> str:
        return self._ensure_evolve(self._coerce_evidence(source), replay=False)

    def _ensure_evolve(self, evidence: dict[str, Any], *, replay: bool) -> str:
        frozen = projection(evidence)
        dispatch = self._dispatch_for(str(frozen["evidence_no"]), "graph_evolve")
        if dispatch is not None and self._record_alive("graph_evolve", dispatch.get("记录id")):
            return f"已记录图谱演化 {dispatch.get('记录编号')}"

        if replay and dispatch is None:
            legacy = self._match_legacy_evolve(evidence)
            if legacy is not None:
                self._record_dispatch(evidence, "graph_evolve", record=legacy)
                return f"已核对图谱演化 {legacy.get('记录编号')}"

        rows = store.rows("graph_evolve")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        ev_entry: dict[str, Any] = {
            "id": next_id,
            "status": "已记录",
            "pending": True,
            "abnormal": False,
            "记录编号": f"GRAP-EVOL-{next_id:04d}",
            "变更类型": f"lineage-{display(frozen['link_type'])}",
            # 影响字段取自同次确认的冻结点：历史缺值保持缺值，不取链路当前值。
            "影响实体": display(frozen["target"]),
            "影响关系": display(frozen["relation"]),
            "版本标签": f"v{next_id}.0",
            "变更描述": f"血缘链路{display(frozen['link_no'])}已变更",
            "回滚版本": f"v{next_id-1}.0",
            "演化时间": display(frozen["date"]),
        }
        rows.append(ev_entry)
        self._record_dispatch(evidence, "graph_evolve", record=ev_entry)
        return f"{'已补投' if replay else '已记录'}图谱演化 {ev_entry['记录编号']}"

    def _match_legacy_evolve(self, evidence: dict[str, Any]) -> dict[str, Any] | None:
        """没有投递台账时，用原草稿字段回查已有的演化记录。

        全部字段按「缺值即空串」精确比对：早期记录方向字段不全时，
        不会把后来某次确认写下的影响关系认领成自己的。
        """
        frozen = projection(evidence)
        bound = {
            row.get("记录id")
            for row in self._dispatch_rows()
            if row.get("目标模块") == "graph_evolve"
        }
        description = f"血缘链路{display(frozen['link_no'])}已变更"
        for row in store.rows("graph_evolve"):
            if row.get("id") in bound or row.get("变更描述") != description:
                continue
            if row.get("影响实体") != display(frozen["target"]):
                continue
            if row.get("影响关系") != display(frozen["relation"]):
                continue
            return row
        return None

    # ===== 图谱比对 =====
    def _push_to_graph_compare(self, source: dict[str, Any]) -> str:
        return self._ensure_compare(self._coerce_evidence(source), replay=False)

    def _ensure_compare(self, evidence: dict[str, Any], *, replay: bool) -> str:
        frozen = projection(evidence)
        dispatch = self._dispatch_for(str(frozen["evidence_no"]), "graph_compare")
        if dispatch is not None and self._record_alive("graph_compare", dispatch.get("记录id")):
            return f"已核对图谱比对 {dispatch.get('记录编号')}"

        # 同一条链路只保留一份待审草稿：后续确认/补投一律沿用原草稿，
        # 不覆盖原草稿的上下游，也不新增第二份相同草稿。
        existing = self._compare_for_link(frozen)
        if existing is None:
            # 旧数据没有投递台账时按两端/日期回查，能对上才沿用。
            existing = self._match_legacy_compare(evidence, allow_fallback=replay)
        if existing is not None:
            self._record_dispatch(evidence, "graph_compare", record=existing)
            return f"已核对图谱比对 {existing.get('任务编号')}"

        rows = store.rows("graph_compare")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        cmp_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待执行",
            "pending": True,
            "abnormal": False,
            "任务编号": f"GRAP-CMP-{next_id:04d}",
            "基准图": display(frozen["source"]),
            "对比图": display(frozen["target"]),
            "比对维度": "lineage-change-diff",
            "新增实体": "auto-calc",
            "删除实体": "auto-calc",
            "变更关系": "auto-calc",
            "比对时间": display(frozen["date"]),
        }
        rows.append(cmp_entry)
        self._record_dispatch(evidence, "graph_compare", record=cmp_entry)
        return f"{'已补投' if replay else '已触发'}图谱比对 {cmp_entry['任务编号']}"

    def _compare_for_link(self, frozen: dict[str, Any]) -> dict[str, Any] | None:
        """按链路编号回查已有的待审草稿（投递台账优先）。"""
        for row in self._dispatch_rows():
            if row.get("目标模块") != "graph_compare":
                continue
            if row.get("链路编号") != frozen["link_no"]:
                continue
            record_id = row.get("记录id")
            if not self._record_alive("graph_compare", record_id):
                continue
            record = store.find("graph_compare", int(record_id))
            if record is not None:
                return record
        return None

    def _match_legacy_compare(
        self, evidence: dict[str, Any], *, allow_fallback: bool
    ) -> dict[str, Any] | None:
        """没有投递台账时回查原草稿（旧数据）。

        两端能对上就复用；对不上时：
        - 补投（allow_fallback）沿用最早的原草稿，只核对不制造副本；
        - 常规确认按同次确认的冻结方向新建，不认领别的草稿。
        """
        frozen = projection(evidence)
        bound = {
            row.get("记录id")
            for row in self._dispatch_rows()
            if row.get("目标模块") == "graph_compare"
        }
        candidates = [
            row for row in store.rows("graph_compare")
            if row.get("id") not in bound
            and str(row.get("比对维度", "")).startswith("lineage")
        ]
        if not candidates:
            return None

        def score(row: dict[str, Any]) -> int:
            value = 0
            if row.get("基准图") == display(frozen["source"]):
                value += 1
            if row.get("对比图") == display(frozen["target"]):
                value += 1
            if row.get("比对时间") == display(frozen["date"]):
                value += 1
            return value

        best = max(candidates, key=score)
        if not is_missing(frozen["source"]) and not is_missing(frozen["target"]) \
                and best.get("基准图") == display(frozen["source"]) \
                and best.get("对比图") == display(frozen["target"]):
            return best
        # 方向字段缺失或对不上时：补投沿用最早的原草稿只做核对，
        # 不制造副本；常规确认宁可新建，也不认领别的草稿。
        return candidates[0] if allow_fallback else None

    # ===== 补投 =====
    def _legacy_evidence(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """旧确认没有证据行时，从原草稿/演化记录里还原那一次的方向。

        旧字段本身不足以还原的部分一律留空并记入「缺失字段」，
        不拿链路当前值顶替，避免把后来的改写错配给早期确认。
        """
        bound = {
            row.get("记录id")
            for row in self._dispatch_rows()
            if row.get("目标模块") == "graph_compare"
        }
        drafts = [
            row for row in store.rows("graph_compare")
            if row.get("id") not in bound
            and str(row.get("比对维度", "")).startswith("lineage")
        ]
        # 旧草稿没有链路编号，只有唯一一份候选时才能确定归属；
        # 多份候选宁可留成缺值，也不把别的链路的上下游安到这次确认上。
        draft = drafts[0] if len(drafts) == 1 else None
        evolve = None
        link_no = as_text(entry.get("链路编号"))
        if link_no is not None:
            description = f"血缘链路{display(link_no)}已变更"
            evolve = next(
                (row for row in store.rows("graph_evolve")
                 if row.get("变更描述") == description),
                None,
            )
        if draft is None and evolve is None:
            return None

        raw = {"上游实体": None, "下游实体": None, "关联字段": None,
               "最后更新": None, "链路类型": None, "链路编号": link_no, "转换描述": None}
        if draft is not None:
            raw["上游实体"] = draft.get("基准图") or None
            raw["下游实体"] = draft.get("对比图") or None
            raw["最后更新"] = draft.get("比对时间") or None
        if evolve is not None:
            raw["下游实体"] = raw["下游实体"] or (evolve.get("影响实体") or None)
            raw["关联字段"] = evolve.get("影响关系") or None
            raw["最后更新"] = raw["最后更新"] or (evolve.get("演化时间") or None)
        missing = [name for name, value in raw.items() if is_missing(value)]
        evidence = {
            "id": 0,
            "证据编号": f"LINE-EVID-LEGACY-{entry.get('id')}",
            "链路编号": link_no,
            "来源记录id": entry.get("id"),
            "确认序号": 0,
            "指纹": f"legacy-{link_key(entry)}",
            "缺失字段": missing,
            "方向": "上游实体→下游实体",
        }
        evidence.update(raw)
        return evidence

    def _replay_deliveries(self, entry: dict[str, Any]) -> list[str]:
        """补投只回查实际用过的确认版本，不按链路当前字段重新生成证据。"""
        rows = self._evidence_rows()
        evidences = [row for row in rows if row.get("来源记录id") == entry.get("id")]
        if not evidences:
            key = link_key(entry)
            evidences = [row for row in rows if link_key(row) == key]
        evidences.sort(key=lambda row: int(row.get("确认序号", 0)))
        if not evidences:
            # 没有证据行时，只从原草稿/原演化记录能查到的那次投递还原。
            # 连这个都查不到，就不补——旧字段不足以还原时保留缺值，不凭空造一份。
            legacy = self._legacy_evidence(entry)
            if legacy is None:
                return []
            evidences = [legacy]

        messages: list[str] = []
        for evidence in evidences:
            for target, deliver in (
                ("graph_compare", self._ensure_compare),
                ("graph_evolve", self._ensure_evolve),
            ):
                try:
                    messages.append(deliver(evidence, replay=True))
                except Exception as exc:
                    self._record_dispatch(evidence, target, status="待补投", note=str(exc))
                    messages.append(f"{target} 仍待补投")
        return messages

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["graph_evolve", "graph_compare"]
        base["direction_evidence"] = len(self._evidence_rows())
        base["pending_deliveries"] = sum(
            1 for row in self._dispatch_rows() if row.get("状态") != "已投递"
        )
        return base
