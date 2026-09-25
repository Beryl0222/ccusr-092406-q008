"""culture_finance_progress 领域资料的基础结构。"""

from __future__ import annotations

EVENT_KINDS = [
    "CONTRACT_COMMITTED",    # 资金合同登记：锁定用途、成本项、出资比例与生效条件
    "APPLICATION_SUBMITTED",  # 验收申请提交
    "OVERLAP_FLAGGED",       # 跨资金来源重复覆盖提示（仅提示，不自动处置）
    "OVERLAP_RESOLVED",      # 独立审核人对重叠提示的拆分/拒绝/驳回决定
    "MILESTONE_ACCEPTED",    # 里程碑验收（支持部分验收与版本化调整）
    "TRANCHE_RELEASED",      # 分批放款
    "RECOVERY_RECONCILED",   # 追偿核销
]
REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")

def validate_event(record: dict) -> list[str]:
    """检查样例事件是否具备可交换的最小字段。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems
