"""血缘方向证据与补投行为测试（标准库 unittest，不引入额外依赖）。

覆盖：
1. 同一次确认只冻结一份方向证据，演化/比对读同一份；
2. 关联字段改写后再次确认，生成新证据，旧草稿与旧演化不被改写；
3. 单侧写入中断后，另一侧产物仍在；补投只补缺件；
4. 反复补投不新增第二份比对草稿、也不重复写演化；
5. 历史缺值（缺键/空串）原样保留，不被链路当前值补全。
"""
from __future__ import annotations

import unittest

from app.services import data_lineage as lineage_module
from app.services.data_lineage import DataLineageService
from app.services.lineage_evidence import DISPATCH_MODULE, EVIDENCE_MODULE
from app.store import store


def link(**overrides):
    row = {
        "id": 1,
        "status": "自动推导",
        "pending": True,
        "abnormal": False,
        "链路编号": "DATA-9001",
        "上游实体": "A",
        "下游实体": "B",
        "转换描述": "order sync",
        "关联字段": "ORDER_ID",
        "影响层级": "table",
        "链路类型": "batch",
        "最后更新": "2026-10-01",
    }
    row.update(overrides)
    return row


class LineageEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        for module in ("data_lineage", "graph_evolve", "graph_compare"):
            store._tables[module] = []
        for module in (EVIDENCE_MODULE, DISPATCH_MODULE):
            store._tables.pop(module, None)
        self.service = DataLineageService()
        self.entry = link()
        store.rows("data_lineage").append(self.entry)

    def interrupt_evolve(self) -> None:
        def boom(source):
            raise RuntimeError("graph_evolve 写入中断")

        self.service._push_to_graph_evolve = boom

    def test_single_confirmation_feeds_both_sides_from_one_evidence(self) -> None:
        self.service.run_action(1, "确认链路")

        evidences = store.rows(EVIDENCE_MODULE)
        self.assertEqual(len(evidences), 1)
        evidence = evidences[0]

        evolves = store.rows("graph_evolve")
        compares = store.rows("graph_compare")
        self.assertEqual(len(evolves), 1)
        self.assertEqual(len(compares), 1)

        self.assertEqual(evolves[0]["影响实体"], evidence["下游实体"])
        self.assertEqual(evolves[0]["影响关系"], evidence["关联字段"])
        self.assertEqual(evolves[0]["演化时间"], evidence["最后更新"])
        self.assertEqual(compares[0]["基准图"], evidence["上游实体"])
        self.assertEqual(compares[0]["对比图"], evidence["下游实体"])
        self.assertEqual(compares[0]["比对时间"], evidence["最后更新"])

    def test_rewritten_field_creates_new_evidence_without_touching_old_draft(self) -> None:
        self.interrupt_evolve()
        self.service.run_action(1, "确认链路")
        del self.service._push_to_graph_evolve

        self.entry["关联字段"] = "CUSTOMER_ID"
        self.entry["最后更新"] = "2026-10-05"
        self.service.run_action(1, "确认链路")

        evidences = store.rows(EVIDENCE_MODULE)
        self.assertEqual([item["确认序号"] for item in evidences], [1, 2])
        self.assertEqual([item["关联字段"] for item in evidences], ["ORDER_ID", "CUSTOMER_ID"])

        draft = store.rows("graph_compare")
        self.assertEqual(len(draft), 1)
        self.assertEqual((draft[0]["基准图"], draft[0]["对比图"]), ("A", "B"))
        # 原草稿保持第一次确认的日期，不被第二次确认改写。
        self.assertEqual(draft[0]["比对时间"], "2026-10-01")

        evolves = store.rows("graph_evolve")
        self.assertEqual(len(evolves), 1)
        self.assertEqual(evolves[0]["影响关系"], "CUSTOMER_ID")

        # 补投完成第一次确认的缺件：演化补上，比对侧不出现第二份草稿。
        self.service.run_action(1, "补投")
        self.assertEqual(len(store.rows("graph_compare")), 1)
        self.assertEqual(len(store.rows("graph_evolve")), 2)
        restored = [row for row in store.rows("graph_evolve") if row["影响关系"] == "ORDER_ID"]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["演化时间"], "2026-10-01")

    def test_interrupted_side_is_replayed_without_duplicates(self) -> None:
        self.interrupt_evolve()
        self.service.run_action(1, "确认链路")
        del self.service._push_to_graph_evolve

        self.assertEqual(len(store.rows("graph_evolve")), 0)
        self.assertEqual(len(store.rows("graph_compare")), 1)

        for _ in range(3):
            self.service.run_action(1, "补投")

        self.assertEqual(len(store.rows("graph_evolve")), 1)
        self.assertEqual(len(store.rows("graph_compare")), 1)

    def test_replay_uses_the_version_actually_delivered(self) -> None:
        self.interrupt_evolve()
        self.service.run_action(1, "确认链路")
        del self.service._push_to_graph_evolve
        self.assertEqual(store.rows(EVIDENCE_MODULE)[0]["关联字段"], "ORDER_ID")

        self.entry["关联字段"] = "CUSTOMER_ID"
        self.entry["上游实体"] = "A2"
        self.service.run_action(1, "确认链路")

        before = [dict(row) for row in store.rows("graph_compare")]
        self.service.run_action(1, "补投")
        self.service.run_action(1, "补投")

        after = [dict(row) for row in store.rows("graph_compare")]
        self.assertEqual(before, after)
        self.assertEqual((after[0]["基准图"], after[0]["对比图"]), ("A", "B"))

    def test_historical_missing_values_stay_missing(self) -> None:
        # 早期记录：方向字段缺键、关联字段缺失，确认已发生过但没有留下投递台账。
        legacy = {"id": 2, "status": "已确认", "pending": True, "abnormal": False,
                  "链路编号": "DATA-9002", "下游实体": "B", "影响层级": "table",
                  "关联字段": "LATER_VALUE", "最后更新": "2026-10-02"}
        store.rows("data_lineage").append(legacy)
        self.service._evidence_rows().append({
            "id": 900,
            "证据编号": "LINE-EVID-0900",
            "链路编号": "DATA-9002",
            "来源记录id": 2,
            "确认序号": 1,
            "指纹": "legacy-fingerprint",
            "缺失字段": ["上游实体", "关联字段"],
            "上游实体": None,
            "下游实体": "B",
            "关联字段": None,
            "最后更新": "2026-10-01",
            "链路类型": "batch",
            "转换描述": None,
        })

        self.service.run_action(2, "补投")

        evolves = [row for row in store.rows("graph_evolve")
                   if row["变更描述"].startswith("血缘链路DATA-9002")]
        self.assertEqual(len(evolves), 1)
        # 影响字段取旧确认的缺值，而不是链路当前的 LATER_VALUE。
        self.assertEqual(evolves[0]["影响实体"], "B")
        self.assertEqual(evolves[0]["影响关系"], "")
        self.assertEqual(evolves[0]["演化时间"], "2026-10-01")

        drafts = [row for row in store.rows("graph_compare")
                  if row["对比图"] == "B" and row["比对维度"].startswith("lineage")]
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["基准图"], "")
        self.assertEqual(drafts[0]["比对时间"], "2026-10-01")

        view = self.service.extract_direction_evidence({"id": 3, "链路编号": "DATA-9003"})
        self.assertIsNone(view["上游实体"])
        self.assertIsNone(view["关联字段"])
        self.assertEqual(
            view["缺失字段"],
            ["上游实体", "下游实体", "关联字段", "最后更新", "链路类型", "转换描述"],
        )

    def test_unknown_action_still_rejected(self) -> None:
        entry, message = self.service.run_action(1, "不存在的动作")
        self.assertIsNone(entry)
        self.assertIn("不属于", message)
        self.assertEqual(store.rows(EVIDENCE_MODULE), [])


if __name__ == "__main__":
    unittest.main()
