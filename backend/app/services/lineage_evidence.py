"""血缘方向证据：把一次血缘确认记录冻结成不可变的方向证据。

数据血缘确认会同时驱动图谱演化（待处理清单）和图谱比对（待审草稿）。
两边必须读取同一次确认冻结下来的源/目标方向和日期，而不是各自回头读
链路当前字段——链路在两次确认之间被改写时，当前字段已经不是那一次
确认实际使用的版本了。

历史记录里方向字段可能缺键、为 None 或空串；这里一律按缺值处理，
只标注「缺失字段」，不做补全，保证原文缺值可以继续被识别。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# 一次确认需要冻结的源字段：方向（上游/下游）、关联字段、日期和链路属性。
EVIDENCE_FIELDS = ("上游实体", "下游实体", "关联字段", "最后更新", "链路类型", "链路编号", "转换描述")

# 内部证据/投递台账表名：双下划线开头，不进入业务模块清单和运营概览。
EVIDENCE_MODULE = "__lineage_direction_evidence"
DISPATCH_MODULE = "__lineage_dispatch"


def as_text(value: Any) -> Any:
    """转换成对外输出的值，保留缺值语义：None/空串不臆造内容。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def display(value: Any) -> str:
    """写入下游记录时的文本形态：缺值保持为空串，与原有输出一致。"""
    if is_missing(value):
        return ""
    return str(value)


def is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _encoded(value: Any) -> list[str]:
    """区分 None 和空串，避免历史缺值被同一确认的后续值覆盖后指纹不变。"""
    if value is None:
        return ["none"]
    if isinstance(value, str):
        return ["str", value]
    return [type(value).__name__, str(value)]


def link_key(entry: dict[str, Any]) -> str:
    link_no = entry.get("链路编号")
    if not is_missing(link_no):
        return str(link_no)
    return f"record-{entry.get('id')}"


def fingerprint(*, link: str, record: Any, fields: dict[str, Any]) -> str:
    """确认指纹 = 冻结下来的方向字段集合。

    关联字段等被改写后再次确认，冻结值变化，必然产生新指纹；
    同一份确认原样反复投递时指纹不变，因此稳定复用它自己的那份证据。
    """
    payload = {
        "link": link,
        "record": record,
        "fields": {name: _encoded(value) for name, value in fields.items()},
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    """冻结链路当前值；缺键与 None 都记为缺失。"""
    raw = {field: (entry[field] if field in entry else None) for field in EVIDENCE_FIELDS}
    missing = [field for field, value in raw.items() if is_missing(value)]
    return {
        "raw": raw,
        "missing": missing,
        "fingerprint": fingerprint(
            link=link_key(entry), record=entry.get("id"), fields=raw),
    }


def projection(evidence: dict[str, Any]) -> dict[str, Any]:
    """同次确认的唯一方向投影，演化和图谱比对都只读这一份。"""
    return {
        "source": evidence.get("上游实体"),
        "target": evidence.get("下游实体"),
        "relation": evidence.get("关联字段"),
        "date": evidence.get("最后更新"),
        "link_no": evidence.get("链路编号"),
        "link_type": evidence.get("链路类型"),
        "evidence_no": evidence.get("证据编号"),
        "version": evidence.get("确认序号"),
        "missing": list(evidence.get("缺失字段") or []),
    }


def view(evidence: dict[str, Any]) -> dict[str, Any]:
    """对外证据视图：方向/日期字段沿用原义，缺失字段只标注不补全。"""
    return {
        "证据编号": evidence.get("证据编号"),
        "链路编号": evidence.get("链路编号"),
        "确认序号": evidence.get("确认序号"),
        "源实体": evidence.get("上游实体"),
        "目标实体": evidence.get("下游实体"),
        "上游实体": evidence.get("上游实体"),
        "下游实体": evidence.get("下游实体"),
        "关联字段": evidence.get("关联字段"),
        "日期": evidence.get("最后更新"),
        "最后更新": evidence.get("最后更新"),
        "方向": evidence.get("方向"),
        "缺失字段": list(evidence.get("缺失字段") or []),
    }
