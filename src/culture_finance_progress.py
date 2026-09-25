"""文化项目多方资金穿透式进度账本。

设计要点
========

* **只追加事件溯源**：所有事实都是不可改写的事件，状态由事件序列折叠得到。
  后补票据与撤销结论通过新版本（``ACCEPTANCE_AMENDED``）表达，已发生的放款
  （``TRANCHE_RELEASED``）永不被修改。
* **自动结果只提示**：提交验收时系统把跨资金来源的重复覆盖写入
  ``OVERLAP_FLAGGED`` 提示，但能否放款取决于独立审核人的
  ``ACCEPTANCE_REVIEWED``（拆分 / 批准 / 拒绝）。
* **金额守恒**：``已承诺 = 可用额度 + 净放款额（放款-追偿）``；成本项累计
  净覆盖不得超过成本金额；追偿冲减欠款并恢复可用额度。
* **故障安全**：提交按客户请求号幂等、放款按回单号幂等；提交带期望序号做
  乐观并发；JSONL 落盘后 fsync，重放时丢弃末尾撕裂行，崩溃恢复不多付不漏记。

模块只依赖标准库，可直接在容器内用 unittest 验证。
"""

from __future__ import annotations

import functools
import json
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 事件契约
# ---------------------------------------------------------------------------

#: 账本支持的全部事件种类（保留基线五类语义，并补充资金锁定、验收提交、
#: 独立审核、生效条件与验收版本）。
EVENT_KINDS = [
    "APPLICATION_SUBMITTED",   # 资金申请 / 项目立项
    "FUND_LOCKED",             # 一笔资金合同锁定：用途、成本项、出资比例、生效条件
    "CONDITION_MET",           # 合同生效条件被满足
    "ACCEPTANCE_SUBMITTED",    # 项目方提交验收（可携带多资金来源的覆盖申请）
    "OVERLAP_FLAGGED",         # 系统自动识别的跨来源重复覆盖（仅提示）
    "ACCEPTANCE_REVIEWED",     # 独立审核人决定：批准 / 拆分 / 拒绝
    "MILESTONE_ACCEPTED",      # 验收结论定稿（某个版本），决定各资金可释放额
    "ACCEPTANCE_AMENDED",      # 新版本：后补票据或撤销原结论，不改写原放款
    "TRANCHE_RELEASED",        # 分批放款（按回单号幂等）
    "RECOVERY_RECONCILED",     # 追偿入账，恢复对应资金可用额度
]

REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")

FUND_TYPES = ("BANK_LOAN", "FISCAL_SUBSIDY", "SOCIAL_INVESTMENT")
REVIEW_APPROVE = "APPROVE"
REVIEW_SPLIT = "SPLIT"
REVIEW_REJECT = "REJECT"
REVIEW_DECISIONS = (REVIEW_APPROVE, REVIEW_SPLIT, REVIEW_REJECT)
AMEND_SUPPLEMENT = "SUPPLEMENT"  # 后补票据，结论可能上调
AMEND_REVOCATION = "REVOCATION"  # 撤销 / 下调结论，不足部分转入追偿
AMEND_TYPES = (AMEND_SUPPLEMENT, AMEND_REVOCATION)

_ZERO = Decimal("0")


def D(value: Any) -> Decimal:
    """把字符串 / 数字安全转为 Decimal。"""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - 防御
        raise LedgerError(f"非法金额: {value!r}") from exc


def _money(value: Decimal) -> str:
    """事件载荷中的金额统一序列化为字符串，避免浮点误差。"""
    return format(value.quantize(Decimal("0.01")), "f")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_event(record: dict) -> list[str]:
    """检查事件是否具备可交换的最小字段（与基线契约保持兼容）。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    if "payload" in record and not isinstance(record["payload"], dict):
        problems.append("payload")
    return problems


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class LedgerError(ValueError):
    """所有账本业务错误的基类。"""


class ConcurrencyError(LedgerError):
    """期望序号与账本当前序号不一致（并发修改 / 过期请求）。"""


class InvariantViolation(LedgerError):
    """操作会破坏金额守恒或其他硬不变式。"""


class UnknownReference(LedgerError):
    """引用了尚不存在的资金、验收或成本项。"""


class DuplicateSubmission(LedgerError):
    """验收编号或票据重复提交。"""


# ---------------------------------------------------------------------------
# 纯函数：折叠与派生口径
# ---------------------------------------------------------------------------


def _new_state() -> dict:
    return {
        "seq": 0,
        "journal": [],             # 原始事件序列（内存账本的审计源 / 幂取回取）
        "subjects": {},
        "funds": {},
        "cost_items": {},
        "acceptances": {},
        "flags": [],
        "releases": [],
        "recoveries": [],
        "requests": {},   # client_request_id -> [event_id...]（提交幂等/断点续跑）
        "receipts": {},   # 放款/追偿回单号 -> event_id（到账幂等）
        "event_ids": {},  # event_id -> seq（重放去重）
    }


def _acc_view(acc: dict) -> dict | None:
    """验收当前生效版本；无版本（被拒/未定稿）时返回 None。"""
    return acc["versions"][-1] if acc["versions"] else None


def fund_released(state: dict, fund_id: str) -> Decimal:
    return sum((r["amount"] for r in state["releases"] if r["fund_id"] == fund_id), _ZERO)


def fund_recovered(state: dict, fund_id: str) -> Decimal:
    return sum((r["amount"] for r in state["recoveries"] if r["fund_id"] == fund_id), _ZERO)


def fund_outstanding(state: dict, fund_id: str) -> Decimal:
    """净放款额 = 累计放款 - 累计追偿。"""
    return fund_released(state, fund_id) - fund_recovered(state, fund_id)


def fund_available(state: dict, fund_id: str) -> Decimal:
    """可用额度 = 承诺额 - 净放款额。"""
    fund = state["funds"][fund_id]
    return fund["amount"] - fund_outstanding(state, fund_id)


def acc_net_released(state: dict, fund_id: str, acc_id: str) -> Decimal:
    rel = sum(
        (r["amount"] for r in state["releases"]
         if r["fund_id"] == fund_id and r["acceptance_id"] == acc_id),
        _ZERO,
    )
    rec = sum(
        (r["amount"] for r in state["recoveries"]
         if r["fund_id"] == fund_id and r["acceptance_id"] == acc_id),
        _ZERO,
    )
    return rel - rec


def acc_accepted_by_fund(state: dict, acc_id: str) -> dict[str, Decimal]:
    """验收当前版本中，各出资方获准覆盖的金额合计。"""
    acc = state["acceptances"][acc_id]
    view = _acc_view(acc)
    totals: dict[str, Decimal] = {}
    if view is not None:
        for alloc in view["allocations"].values():
            for fund_id, amount in alloc.items():
                totals[fund_id] = totals.get(fund_id, _ZERO) + amount
    return totals


def recovery_due(state: dict, fund_id: str, acc_id: str) -> Decimal:
    """新版本下调结论后，已净放款超出新结论的部分（必须先追偿）。"""
    accepted = acc_accepted_by_fund(state, acc_id).get(fund_id, _ZERO)
    net = acc_net_released(state, fund_id, acc_id)
    return max(_ZERO, net - accepted)


def cost_accepted_total(state: dict, cost_item_id: str, exclude_acc: str | None = None) -> Decimal:
    """某成本项在所有已定稿验收的当前版本中累计被覆盖的金额。"""
    total = _ZERO
    for acc_id, acc in state["acceptances"].items():
        if acc_id == exclude_acc:
            continue
        view = _acc_view(acc)
        if view is None:
            continue
        total += sum(view["allocations"].get(cost_item_id, {}).values(), _ZERO)
    return total


def _apply(state: dict, event: dict) -> dict:
    """把单个事件折叠进状态（纯函数式更新，事件被信任为已校验）。"""
    kind = event["kind"]
    payload = event["payload"]
    seq = event["seq"]
    state["seq"] = seq
    state["journal"].append(event)
    state["event_ids"][event["event_id"]] = seq
    if "request_id" in event:
        # 重放时重建请求幂等表（崩溃恢复后重试仍返回原事件）。
        state["requests"][f"{event['request_id']}::{event.get('stage', 0)}"] = event["event_id"]

    if kind == "APPLICATION_SUBMITTED":
        state["subjects"][event["subject_id"]] = {
            "subject_id": event["subject_id"],
            "project_name": payload.get("project_name", ""),
            "party_ids": list(payload.get("party_ids", [])),
            "event_id": event["event_id"],
            "occurred_at": event["occurred_at"],
        }

    elif kind == "FUND_LOCKED":
        for item in payload.get("cost_items", []):
            ci = state["cost_items"].setdefault(
                item["cost_item_id"],
                {"cost_item_id": item["cost_item_id"], "name": item.get("name", ""),
                 "amount": D(item["amount"]), "subject_id": event["subject_id"]},
            )
            if ci["amount"] != D(item["amount"]):
                raise InvariantViolation(
                    f"成本项 {item['cost_item_id']} 金额与既有登记不一致"
                )
        conditions = {c["condition_id"]: {"label": c.get("label", ""), "met": False,
                                          "met_at": None, "event_id": None}
                      for c in payload.get("conditions", [])}
        state["funds"][payload["fund_id"]] = {
            "fund_id": payload["fund_id"],
            "subject_id": event["subject_id"],
            "funder_id": payload["funder_id"],
            "funder_name": payload.get("funder_name", payload["funder_id"]),
            "fund_type": payload["fund_type"],
            "amount": D(payload["amount"]),
            "purpose": payload.get("purpose", ""),
            "shares": {ci: D(pct) for ci, pct in payload.get("shares", {}).items()},
            "conditions": conditions,
            "event_id": event["event_id"],
            "occurred_at": event["occurred_at"],
        }

    elif kind == "CONDITION_MET":
        fund = state["funds"][payload["fund_id"]]
        cond = fund["conditions"].setdefault(
            payload["condition_id"],
            {"label": payload.get("label", ""), "met": False, "met_at": None, "event_id": None},
        )
        cond.update(met=True, met_at=event["occurred_at"], event_id=event["event_id"])

    elif kind == "ACCEPTANCE_SUBMITTED":
        requests = {
            ci: {fid: D(amt) for fid, amt in alloc.items()}
            for ci, alloc in payload["requested"].items()
        }
        state["acceptances"][payload["acceptance_id"]] = {
            "acceptance_id": payload["acceptance_id"],
            "subject_id": event["subject_id"],
            "submitted_by": payload["submitted_by"],
            "milestone": payload.get("milestone", ""),
            "requested": requests,
            "receipt_ids": list(payload.get("receipt_ids", [])),
            "status": "SUBMITTED",
            "review": None,
            "versions": [],
            "flag_ids": [],
            "event_id": event["event_id"],
            "occurred_at": event["occurred_at"],
        }

    elif kind == "OVERLAP_FLAGGED":
        flag = {
            "flag_id": payload["flag_id"],
            "acceptance_id": payload["acceptance_id"],
            "subject_id": event["subject_id"],
            "findings": payload["findings"],
            "suggested_split": payload.get("suggested_split", {}),
            "resolved_by": None,
            "event_id": event["event_id"],
            "occurred_at": event["occurred_at"],
        }
        state["flags"].append(flag)
        state["acceptances"][payload["acceptance_id"]]["flag_ids"].append(payload["flag_id"])

    elif kind == "ACCEPTANCE_REVIEWED":
        acc = state["acceptances"][payload["acceptance_id"]]
        acc["status"] = "REJECTED" if payload["decision"] == REVIEW_REJECT else "REVIEWED"
        acc["review"] = {
            "reviewer_id": payload["reviewer_id"],
            "decision": payload["decision"],
            "reason": payload.get("reason", ""),
            "flag_resolutions": payload.get("flag_resolutions", {}),
            "event_id": event["event_id"],
            "occurred_at": event["occurred_at"],
        }
        for flag_id in payload.get("resolved_flags", []):
            for flag in state["flags"]:
                if flag["flag_id"] == flag_id:
                    flag["resolved_by"] = event["event_id"]

    elif kind in ("MILESTONE_ACCEPTED", "ACCEPTANCE_AMENDED"):
        acc = state["acceptances"][payload["acceptance_id"]]
        version = {
            "version": payload["version"],
            "kind": kind,
            "change_type": payload.get("change_type"),
            "allocations": {
                ci: {fid: D(amt) for fid, amt in alloc.items()}
                for ci, alloc in payload["allocations"].items()
            },
            "reviewer_id": payload["reviewer_id"],
            "receipt_ids": list(payload.get("receipt_ids", [])),
            "reason": payload.get("reason", ""),
            "supersedes_event": payload.get("supersedes_event"),
            "event_id": event["event_id"],
            "occurred_at": event["occurred_at"],
        }
        acc["versions"].append(version)
        acc["status"] = "ACCEPTED"
        acc["receipt_ids"] = list(version["receipt_ids"])

    elif kind == "TRANCHE_RELEASED":
        record = {
            "release_id": payload["release_id"],
            "receipt_id": payload["receipt_id"],
            "fund_id": payload["fund_id"],
            "acceptance_id": payload["acceptance_id"],
            "subject_id": event["subject_id"],
            "amount": D(payload["amount"]),
            "version": payload["version"],
            "event_id": event["event_id"],
            "occurred_at": event["occurred_at"],
        }
        state["releases"].append(record)
        state["receipts"][payload["receipt_id"]] = {
            "event_id": event["event_id"], "kind": "TRANCHE_RELEASED"}

    elif kind == "RECOVERY_RECONCILED":
        record = {
            "recovery_id": payload["recovery_id"],
            "receipt_id": payload["receipt_id"],
            "fund_id": payload["fund_id"],
            "acceptance_id": payload["acceptance_id"],
            "subject_id": event["subject_id"],
            "amount": D(payload["amount"]),
            "event_id": event["event_id"],
            "occurred_at": event["occurred_at"],
        }
        state["recoveries"].append(record)
        state["receipts"][payload["receipt_id"]] = {
            "event_id": event["event_id"], "kind": "RECOVERY_RECONCILED"}

    return state


def fold(events: Iterable[dict]) -> dict:
    """从事件序列重放出完整状态（崩溃恢复 / 监管复算都走这里）。"""
    state = _new_state()
    for event in events:
        problems = validate_event(event)
        if problems:
            raise LedgerError(f"事件 {event.get('event_id')} 缺少字段: {problems}")
        expected_seq = state["seq"] + 1
        if event["seq"] != expected_seq:
            raise LedgerError(
                f"事件序号不连续: 期望 {expected_seq}，实际 {event['seq']}"
            )
        if event["event_id"] in state["event_ids"]:
            raise LedgerError(f"事件编号重复: {event['event_id']}")
        _apply(state, event)
    return state


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------


def _locked(method):
    """把整条命令（校验 + 落账）串行化，杜绝并发双花；RLock 允许命令互相调用。"""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class ProgressLedger:
    """穿透式进度账本（线程安全；可选 JSONL 持久化）。

    Parameters
    ----------
    path:
        事件日志文件路径。为空则纯内存账本。文件末尾若存在崩溃留下的撕裂
        行（无换行结尾或 JSON 不完整），打开时自动截断到最后一条完整事件。
    """

    def __init__(self, path: str | Path | None = None):
        self._lock = threading.RLock()
        self._state = _new_state()
        self.path = Path(path) if path else None
        if self.path is not None:
            self._load()

    # -- 持久化 -------------------------------------------------------------

    def _load(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return
        good_lines: list[str] = []
        raw_lines: list[str] = []
        if self.path.exists():
            raw_lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        for line in raw_lines:
            if not line.endswith("\n"):
                # 末尾撕裂行：写入中途崩溃，丢弃以防“半个事件”生效。
                break
            try:
                event = json.loads(line)
                validate_event(event)
            except (ValueError, TypeError):
                break
            good_lines.append(line)
        if len(good_lines) != len(raw_lines):
            # 重写干净日志，确保后续追加从完整边界开始。
            with self.path.open("w", encoding="utf-8") as fh:
                fh.writelines(good_lines)
                fh.flush()
                import os
                os.fsync(fh.fileno())
        events = [json.loads(line) for line in good_lines]
        self._state = fold(events)

    def _resume_event(self, request_id: str | None, stage: int) -> dict | None:
        """按请求号 + 阶段号取回此前已落账的事件（断点续跑）。"""
        if request_id is None:
            return None
        key = f"{request_id}::{stage}"
        event_id = self._state["requests"].get(key)
        return None if event_id is None else self._find_event(event_id)

    def _resume_single(self, kind: str, request_id: str | None) -> dict | None:
        """单事件命令的幂等短路；请求号被用于其他事件种类时拒绝。"""
        prior = self._resume_event(request_id, 0)
        if prior is None:
            return None
        if prior["kind"] != kind:
            raise LedgerError(f"请求号 {request_id} 已用于另一种操作")
        return prior

    def _receipt_lookup(self, receipt_id: str, kind: str) -> dict | None:
        """回单号幂等查询；同号被另一类事件占用时显式拒绝。"""
        entry = self._state["receipts"].get(receipt_id)
        if entry is None:
            return None
        if entry["kind"] != kind:
            raise LedgerError(f"回单号 {receipt_id} 已用于 {entry['kind']}")
        return self._find_event(entry["event_id"])

    def _append(self, kind: str, subject_id: str, payload: dict, *,
                request_id: str | None = None, stage: int = 0,
                expected_seq: int | None = None,
                occurred_at: str | None = None) -> dict:
        with self._lock:
            if request_id is None and expected_seq is not None and \
                    expected_seq != self._state["seq"]:
                raise ConcurrencyError(
                    f"期望序号 {expected_seq}，当前序号 {self._state['seq']}"
                )
            if request_id is not None:
                key = f"{request_id}::{stage}"
                prior = self._state["requests"].get(key)
                if prior is not None:
                    return self._find_event(prior)
                if expected_seq is not None and expected_seq != self._state["seq"]:
                    raise ConcurrencyError(
                        f"期望序号 {expected_seq}，当前序号 {self._state['seq']}"
                    )
            event = {
                "event_id": f"evt-{uuid.uuid4().hex}",
                "kind": kind,
                "occurred_at": occurred_at or now_iso(),
                "subject_id": subject_id,
                "seq": self._state["seq"] + 1,
                "payload": payload,
            }
            if request_id is not None:
                event["request_id"] = request_id
                event["stage"] = stage
            line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
                    import os
                    os.fsync(fh.fileno())
            _apply(self._state, event)
            if request_id is not None:
                self._state["requests"][f"{request_id}::{stage}"] = event["event_id"]
            return event

    def _find_event(self, event_id: str) -> dict:
        for event in self._state["journal"]:
            if event["event_id"] == event_id:
                return event
        raise LedgerError(f"找不到事件 {event_id}")  # pragma: no cover

    # -- 基本查询 -----------------------------------------------------------

    @property
    def seq(self) -> int:
        return self._state["seq"]

    @property
    def state(self) -> dict:
        return self._state

    def events(self) -> list[dict]:
        """按序号返回全部事件（监管复算 / 审计导出）。"""
        with self._lock:
            if self.path is not None:
                return [json.loads(line)
                        for line in self.path.read_text(encoding="utf-8").splitlines()
                        if line]
            return list(self._state["journal"])

    # -- 命令：申请与资金锁定 ----------------------------------------------

    @_locked
    def submit_application(self, subject_id: str, project_name: str,
                           party_ids: list[str] | None = None, *,
                           request_id: str | None = None) -> dict:
        prior = self._resume_single("APPLICATION_SUBMITTED", request_id)
        if prior is not None:
            return prior
        if subject_id in self._state["subjects"]:
            raise DuplicateSubmission(f"项目 {subject_id} 已立项")
        payload = {"project_name": project_name, "party_ids": list(party_ids or [])}
        return self._append("APPLICATION_SUBMITTED", subject_id, payload,
                            request_id=request_id)

    @_locked
    def lock_fund(self, *, fund_id: str, subject_id: str, funder_id: str,
                  fund_type: str, amount: Any, purpose: str,
                  cost_items: list[dict], shares: dict[str, Any] | None = None,
                  conditions: list[dict] | None = None,
                  funder_name: str | None = None,
                  request_id: str | None = None) -> dict:
        """锁定一笔资金合同。

        ``cost_items`` 登记本合同涉及的成本项（金额、用途）；``shares`` 给出
        本资金对各成本项的约定出资比例（百分数，0~100）；``conditions`` 是
        放款前必须满足的生效条件。
        """
        prior = self._resume_single("FUND_LOCKED", request_id)
        if prior is not None:
            return prior
        if subject_id not in self._state["subjects"]:
            raise UnknownReference(f"项目 {subject_id} 尚未立项")
        if fund_id in self._state["funds"]:
            raise DuplicateSubmission(f"资金 {fund_id} 已锁定")
        if fund_type not in FUND_TYPES:
            raise LedgerError(f"未知资金类型: {fund_type}")
        amt = D(amount)
        if amt <= 0:
            raise InvariantViolation("资金承诺额必须为正")
        declared = {item["cost_item_id"] for item in cost_items}
        shares = shares or {}
        unknown = set(shares) - declared
        if unknown:
            # 允许引用其他合同登记的成本项，但若全局不存在则拒绝。
            missing = [ci for ci in unknown if ci not in self._state["cost_items"]]
            if missing:
                raise UnknownReference(f"出资比例引用了不存在的成本项: {missing}")
        for pct in shares.values():
            p = D(pct)
            if p < 0 or p > 100:
                raise InvariantViolation(f"出资比例越界: {pct}")
        payload = {
            "fund_id": fund_id,
            "funder_id": funder_id,
            "funder_name": funder_name or funder_id,
            "fund_type": fund_type,
            "amount": _money(amt),
            "purpose": purpose,
            "cost_items": [
                {"cost_item_id": item["cost_item_id"],
                 "name": item.get("name", ""),
                 "amount": _money(D(item["amount"]))}
                for item in cost_items
            ],
            "shares": {ci: format(D(p), "f") for ci, p in shares.items()},
            "conditions": [
                {"condition_id": c["condition_id"], "label": c.get("label", "")}
                for c in (conditions or [])
            ],
        }
        return self._append("FUND_LOCKED", subject_id, payload, request_id=request_id)

    @_locked
    def mark_condition_met(self, *, fund_id: str, condition_id: str,
                           label: str = "", request_id: str | None = None) -> dict:
        prior = self._resume_single("CONDITION_MET", request_id)
        if prior is not None:
            return prior
        fund = self._require_fund(fund_id)
        if condition_id not in fund["conditions"]:
            raise UnknownReference(f"资金 {fund_id} 未声明生效条件 {condition_id}")
        if fund["conditions"][condition_id]["met"]:
            raise DuplicateSubmission(f"生效条件 {condition_id} 已满足")
        payload = {"fund_id": fund_id, "condition_id": condition_id, "label": label}
        return self._append("CONDITION_MET", fund["subject_id"], payload,
                            request_id=request_id)

    # -- 命令：验收提交与自动重叠提示 --------------------------------------

    @_locked
    def submit_acceptance(self, *, acceptance_id: str, subject_id: str,
                          submitted_by: str, milestone: str,
                          requested: dict[str, dict[str, Any]],
                          receipt_ids: list[str] | None = None,
                          request_id: str | None = None) -> tuple[dict, list[dict]]:
        """提交里程碑验收。

        ``requested`` 形如 ``{成本项: {资金: 申请覆盖金额}}``。系统**只**生成
        重叠提示并给出建议拆分，绝不自动决定覆盖结果——定稿权在独立审核人。
        返回 ``(验收事件, 提示事件列表)``。
        """
        prior = self._resume_event(request_id, 0)
        if prior is not None:
            return self._resume_submission(prior)

        if subject_id not in self._state["subjects"]:
            raise UnknownReference(f"项目 {subject_id} 尚未立项")
        if acceptance_id in self._state["acceptances"]:
            raise DuplicateSubmission(f"验收 {acceptance_id} 已提交")
        receipt_ids = list(receipt_ids or [])

        norm = self._normalize_requested(acceptance_id, requested, check_funds=True)
        findings = self._compute_findings(acceptance_id, norm, receipt_ids)

        payload = {
            "acceptance_id": acceptance_id,
            "submitted_by": submitted_by,
            "milestone": milestone,
            "requested": {
                ci: {fid: _money(amt) for fid, amt in alloc.items()}
                for ci, alloc in norm.items()
            },
            "receipt_ids": receipt_ids,
        }
        event = self._append("ACCEPTANCE_SUBMITTED", subject_id, payload,
                             request_id=request_id)
        flags = self._append_overlap_flag(acceptance_id, subject_id, norm,
                                          findings, request_id=request_id)
        return event, flags

    def _resume_submission(self, submit_event: dict) -> tuple[dict, list[dict]]:
        """提交事件已落账时，确保提示阶段也补齐，然后整体返回。"""
        acc_id = submit_event["payload"]["acceptance_id"]
        flags = [
            e for e in self._state["journal"]
            if e["kind"] == "OVERLAP_FLAGGED"
            and e["payload"]["acceptance_id"] == acc_id
        ]
        if not flags:
            acc = self._state["acceptances"][acc_id]
            if acc["flag_ids"]:  # 状态与日志矛盾不应发生
                flags = [self._find_event(
                    next(f["event_id"] for f in self._state["flags"]
                         if f["flag_id"] == acc["flag_ids"][0]))]
            else:
                norm = {
                    ci: {fid: D(amt) for fid, amt in alloc.items()}
                    for ci, alloc in acc["requested"].items()
                }
                findings = self._compute_findings(acc_id, norm, acc["receipt_ids"])
                if findings:
                    flags = self._append_overlap_flag(
                        acc_id, submit_event["subject_id"], norm, findings)
        return submit_event, flags

    def _normalize_requested(self, acceptance_id: str,
                             requested: dict[str, dict[str, Any]],
                             *, check_funds: bool) -> dict[str, dict[str, Decimal]]:
        norm: dict[str, dict[str, Decimal]] = {}
        for cost_item_id, alloc in requested.items():
            ci = self._state["cost_items"].get(cost_item_id)
            if ci is None:
                raise UnknownReference(f"成本项 {cost_item_id} 未登记")
            norm_alloc = {fid: D(amt) for fid, amt in alloc.items()}
            for fid, amt in norm_alloc.items():
                if check_funds and fid not in self._state["funds"]:
                    raise UnknownReference(f"资金 {fid} 不存在")
                if amt < 0:
                    raise InvariantViolation("申请覆盖金额不能为负")
            norm[cost_item_id] = norm_alloc
        return norm

    def _compute_findings(self, acceptance_id: str,
                          norm: dict[str, dict[str, Decimal]],
                          receipt_ids: list[str]) -> list[dict]:
        """自动机只做识别与建议，不做决定。计算时排除本次验收自身。"""
        findings: list[dict] = []
        for cost_item_id, norm_alloc in norm.items():
            ci = self._state["cost_items"][cost_item_id]
            accepted_elsewhere = cost_accepted_total(
                self._state, cost_item_id, exclude_acc=acceptance_id)
            other_sources = set()
            pending_sources = set()
            for other_id, other in self._state["acceptances"].items():
                if other_id == acceptance_id or cost_item_id not in other["requested"]:
                    continue
                view = _acc_view(other)
                if view is not None:
                    other_sources.update(view["allocations"].get(cost_item_id, {}))
                elif other["status"] != "REJECTED":
                    # 另一笔验收已提交、尚未定稿：在途重复申报
                    pending_sources.update(other["requested"][cost_item_id])
            requested_total = sum(norm_alloc.values(), _ZERO)
            if accepted_elsewhere > 0 or other_sources or pending_sources or len(norm_alloc) > 1:
                # len > 1：同一次验收内多个资金来源同时申报同一成本项
                # （题面“设备验收被重复申报”的典型形态）。
                findings.append({
                    "type": "CROSS_FUND_DUPLICATE",
                    "cost_item_id": cost_item_id,
                    "fund_ids_in_submission": sorted(norm_alloc),
                    "already_accepted": _money(accepted_elsewhere),
                    "other_fund_ids": sorted(other_sources),
                    "pending_fund_ids": sorted(pending_sources),
                    "requested_total": _money(requested_total),
                })
            if accepted_elsewhere + requested_total > ci["amount"]:
                findings.append({
                    "type": "COST_OVER_COVERAGE",
                    "cost_item_id": cost_item_id,
                    "cost_amount": _money(ci["amount"]),
                    "already_accepted": _money(accepted_elsewhere),
                    "requested_total": _money(requested_total),
                    "over_by": _money(accepted_elsewhere + requested_total - ci["amount"]),
                })
            for fid, amt in norm_alloc.items():
                if fid in self._state["funds"] and amt > fund_available(self._state, fid):
                    findings.append({
                        "type": "INSUFFICIENT_AVAILABILITY",
                        "cost_item_id": cost_item_id,
                        "fund_id": fid,
                        "requested": _money(amt),
                        "available": _money(fund_available(self._state, fid)),
                    })
        seen_receipts = {rid for acc in self._state["acceptances"].values()
                         if acc["acceptance_id"] != acceptance_id
                         for rid in acc["receipt_ids"]}
        for rid in receipt_ids:
            if rid in seen_receipts:
                findings.append({"type": "DUPLICATE_RECEIPT", "receipt_id": rid})
        return findings

    def _append_overlap_flag(self, acceptance_id: str, subject_id: str,
                             norm: dict[str, dict[str, Decimal]],
                             findings: list[dict], *,
                             request_id: str | None = None) -> list[dict]:
        if not findings:
            return []
        suggested = self._suggest_split(norm)
        flag_payload = {
            "flag_id": f"flag-{uuid.uuid4().hex[:12]}",
            "acceptance_id": acceptance_id,
            "findings": findings,
            "suggested_split": {
                ci: {fid: _money(amt) for fid, amt in alloc.items()}
                for ci, alloc in suggested.items()
            },
        }
        return [self._append("OVERLAP_FLAGGED", subject_id, flag_payload,
                             request_id=request_id, stage=1)]

    def _suggest_split(self, requested: dict[str, dict[str, Decimal]]) -> dict[str, dict[str, Decimal]]:
        """按各资金合同约定比例，把成本项剩余未覆盖额切成建议方案。"""
        result: dict[str, dict[str, Decimal]] = {}
        for cost_item_id, alloc in requested.items():
            ci = self._state["cost_items"][cost_item_id]
            remaining = ci["amount"] - cost_accepted_total(self._state, cost_item_id)
            fund_ids = list(alloc)
            weights = {
                fid: self._state["funds"][fid]["shares"].get(cost_item_id, _ZERO)
                for fid in fund_ids
            }
            total_weight = sum(weights.values(), _ZERO)
            split: dict[str, Decimal] = {}
            if total_weight > 0 and remaining > 0:
                allocated = _ZERO
                for fid in fund_ids:
                    part = (remaining * weights[fid] / total_weight).quantize(Decimal("0.01"))
                    part = min(part, fund_available(self._state, fid))
                    split[fid] = part
                    allocated += part
                # 舍入残差并入第一个资金，保证建议方案自身金额闭合
                if allocated < remaining and fund_ids:
                    head = fund_ids[0]
                    room = fund_available(self._state, head) - split.get(head, _ZERO)
                    split[head] = split.get(head, _ZERO) + min(remaining - allocated, room)
            result[cost_item_id] = split
        return result

    # -- 命令：独立审核 -----------------------------------------------------

    @_locked
    def review_acceptance(self, *, acceptance_id: str, reviewer_id: str,
                          decision: str, reason: str = "",
                          allocations: dict[str, dict[str, Any]] | None = None,
                          resolved_flags: list[str] | None = None,
                          request_id: str | None = None,
                          expected_seq: int | None = None) -> list[dict]:
        """独立审核人对验收做决定，返回产生的事件（审核 + 定稿版本）。

        审核人不得是提交人（职责分离）。``APPROVE`` 采用提交方案，``SPLIT``
        采用审核人给的拆分，``REJECT`` 拒绝且不产生可放款额度。
        """
        prior = self._resume_event(request_id, 0)
        if prior is not None:
            return self._resume_review(prior)

        acc = self._require_acc(acceptance_id)
        if decision not in REVIEW_DECISIONS:
            raise LedgerError(f"未知审核决定: {decision}")
        if reviewer_id == acc["submitted_by"]:
            raise LedgerError("审核人必须独立于提交人")
        if acc["review"] is not None:
            raise DuplicateSubmission(f"验收 {acceptance_id} 已完成审核")

        if decision == REVIEW_SPLIT:
            if not allocations:
                raise LedgerError("SPLIT 决定必须给出拆分 allocations")
            chosen = {
                ci: {fid: D(amt) for fid, amt in alloc.items()}
                for ci, alloc in allocations.items()
            }
            unknown_ci = set(chosen) - set(acc["requested"])
            if unknown_ci:
                raise UnknownReference(f"拆分引用了未提交的成本项: {sorted(unknown_ci)}")
        elif decision == REVIEW_APPROVE:
            chosen = {ci: dict(alloc) for ci, alloc in acc["requested"].items()}
        else:
            chosen = None

        if chosen is not None:
            self._check_allocation_limits(acceptance_id, chosen)

        review_payload = {
            "acceptance_id": acceptance_id,
            "reviewer_id": reviewer_id,
            "decision": decision,
            "reason": reason,
            "resolved_flags": list(resolved_flags or acc["flag_ids"]),
            "flag_resolutions": {
                fid: ("ACKNOWLEDGED" if decision != REVIEW_REJECT else "REJECTED")
                for fid in acc["flag_ids"]
            },
        }
        if chosen is not None:
            # 最终拆分写入审核事件：服务在定稿前崩溃时，重试可据此重建。
            review_payload["final_allocations"] = {
                ci: {fid: _money(amt) for fid, amt in alloc.items()}
                for ci, alloc in chosen.items()
            }
        review_event = self._append(
            "ACCEPTANCE_REVIEWED", acc["subject_id"], review_payload,
            request_id=request_id, expected_seq=expected_seq, stage=0,
        )
        if decision == REVIEW_REJECT:
            return [review_event]
        milestone_event = self._append_milestone_version(
            acc, chosen, version=1, change_type="ORIGINAL",
            reviewer_id=reviewer_id, reason=reason,
            request_id=request_id, stage=1)
        return [review_event, milestone_event]

    def _resume_review(self, review_event: dict) -> list[dict]:
        """审核事件已落账、定稿事件未落账时，按审核载荷重建定稿事件。"""
        payload = review_event["payload"]
        if payload["decision"] == REVIEW_REJECT:
            return [review_event]
        existing = next(
            (e for e in self._state["journal"]
             if e["kind"] == "MILESTONE_ACCEPTED"
             and e["payload"]["acceptance_id"] == payload["acceptance_id"]),
            None,
        )
        if existing is not None:
            return [review_event, existing]
        acc = self._state["acceptances"][payload["acceptance_id"]]
        chosen = {
            ci: {fid: D(amt) for fid, amt in alloc.items()}
            for ci, alloc in payload["final_allocations"].items()
        }
        milestone_event = self._append_milestone_version(
            acc, chosen, version=1, change_type="ORIGINAL",
            reviewer_id=payload["reviewer_id"], reason=payload.get("reason", ""),
            request_id=review_event.get("request_id"), stage=1)
        return [review_event, milestone_event]

    def _append_milestone_version(self, acc: dict,
                                  chosen: dict[str, dict[str, Decimal]], *,
                                  version: int, change_type: str,
                                  reviewer_id: str, reason: str,
                                  request_id: str | None, stage: int,
                                  receipt_ids: list[str] | None = None,
                                  expected_seq: int | None = None) -> dict:
        kind = "MILESTONE_ACCEPTED" if version == 1 else "ACCEPTANCE_AMENDED"
        version_payload = {
            "acceptance_id": acc["acceptance_id"],
            "version": version,
            "change_type": change_type,
            "allocations": {
                ci: {fid: _money(amt) for fid, amt in alloc.items()}
                for ci, alloc in chosen.items()
            },
            "reviewer_id": reviewer_id,
            "receipt_ids": list(acc["receipt_ids"] if receipt_ids is None else receipt_ids),
            "reason": reason,
        }
        if version > 1:
            current = _acc_view(acc)
            version_payload["supersedes_event"] = current["event_id"]
        return self._append(kind, acc["subject_id"], version_payload,
                            request_id=request_id, stage=stage)

    def _check_allocation_limits(self, acceptance_id: str,
                                 chosen: dict[str, dict[str, Decimal]]) -> None:
        """拆分 / 新版本必须满足的硬不变式（守恒），提示归提示、红线归红线。"""
        for cost_item_id, alloc in chosen.items():
            ci = self._state["cost_items"].get(cost_item_id)
            if ci is None:
                raise UnknownReference(f"成本项 {cost_item_id} 未登记")
            total = sum(alloc.values(), _ZERO)
            if total < 0 or any(amt < 0 for amt in alloc.values()):
                raise InvariantViolation("覆盖金额不能为负")
            elsewhere = cost_accepted_total(self._state, cost_item_id,
                                            exclude_acc=acceptance_id)
            if elsewhere + total > ci["amount"]:
                raise InvariantViolation(
                    f"成本项 {cost_item_id} 覆盖总额 {elsewhere + total} "
                    f"超过成本金额 {ci['amount']}，存在重复覆盖"
                )
        # 单笔资金在全部验收中累计获准额不得超过承诺额
        per_fund: dict[str, Decimal] = {}
        for other_id, other in self._state["acceptances"].items():
            if other_id == acceptance_id:
                continue
            for fid, amt in acc_accepted_by_fund(self._state, other_id).items():
                per_fund[fid] = per_fund.get(fid, _ZERO) + amt
        for alloc in chosen.values():
            for fid, amt in alloc.items():
                per_fund[fid] = per_fund.get(fid, _ZERO) + amt
        for fid, amt in per_fund.items():
            committed = self._state["funds"][fid]["amount"]
            if amt > committed:
                raise InvariantViolation(
                    f"资金 {fid} 累计获准 {amt} 超过承诺额 {committed}"
                )

    @_locked
    def amend_acceptance(self, *, acceptance_id: str, reviewer_id: str,
                         change_type: str,
                         allocations: dict[str, dict[str, Any]],
                         added_receipt_ids: list[str] | None = None,
                         reason: str = "",
                         request_id: str | None = None,
                         expected_seq: int | None = None) -> dict:
        """以新版本后补票据或撤销结论。

        新版本只替换“验收结论”，原 ``TRANCHE_RELEASED`` 保持不变；若新版本
        把某资金获准额下调到其净放款以下，差额转为 :func:`recovery_due`，
        必须先追偿才能继续对该验收放款。
        """
        prior = self._resume_single("ACCEPTANCE_AMENDED", request_id)
        if prior is not None:
            return prior
        acc = self._require_acc(acceptance_id)
        current = _acc_view(acc)
        if current is None:
            raise LedgerError("验收未定稿，无法追加版本")
        if reviewer_id == acc["submitted_by"]:
            raise LedgerError("审核人必须独立于提交人")
        if change_type not in AMEND_TYPES:
            raise LedgerError(f"未知版本类型: {change_type}")

        chosen = {
            ci: {fid: D(amt) for fid, amt in alloc.items()}
            for ci, alloc in allocations.items()
        }
        self._check_allocation_limits(acceptance_id, chosen)

        receipts = list(dict.fromkeys(acc["receipt_ids"] + list(added_receipt_ids or [])))
        return self._append_milestone_version(
            acc, chosen, version=current["version"] + 1, change_type=change_type,
            reviewer_id=reviewer_id, reason=reason, receipt_ids=receipts,
            request_id=request_id, stage=0)

    # -- 命令：分批放款与追偿 ----------------------------------------------

    @_locked
    def release_tranche(self, *, fund_id: str, acceptance_id: str, amount: Any,
                        receipt_id: str, request_id: str | None = None,
                        expected_seq: int | None = None) -> dict:
        """按验收当前版本释放一笔资金（部分验收即部分释放）。

        以回单号幂等：重复回执直接返回原事件，绝不二次放款。并发提交在命令锁
        内校验可用额度，超额请求被拒。
        """
        if (prior := self._receipt_lookup(receipt_id, "TRANCHE_RELEASED")) is not None:
            return prior
        fund = self._require_fund(fund_id)
        acc = self._require_acc(acceptance_id)
        view = _acc_view(acc)
        amt = D(amount)
        if view is None:
            raise LedgerError(f"验收 {acceptance_id} 尚未形成生效结论，不能放款")
        if amt <= 0:
            raise InvariantViolation("放款金额必须为正")
        unmet = [cid for cid, cond in fund["conditions"].items() if not cond["met"]]
        if unmet:
            raise LedgerError(f"资金 {fund_id} 生效条件未满足: {unmet}")
        due = recovery_due(self._state, fund_id, acceptance_id)
        if due > 0:
            raise InvariantViolation(
                f"验收新版本下调 {_money(due)}，须先追偿再放款"
            )
        accepted = acc_accepted_by_fund(self._state, acceptance_id).get(fund_id, _ZERO)
        cap_acceptance = accepted - acc_net_released(self._state, fund_id, acceptance_id)
        cap_available = fund_available(self._state, fund_id)
        if amt > cap_acceptance:
            raise InvariantViolation(
                f"本次申请放款 {_money(amt)} 超过该验收对该资金的可释放余额 "
                f"{_money(cap_acceptance)}"
            )
        if amt > cap_available:
            raise InvariantViolation(
                f"本次申请放款 {_money(amt)} 超过资金可用额度 {_money(cap_available)}"
            )
        payload = {
            "release_id": f"rel-{uuid.uuid4().hex[:12]}",
            "receipt_id": receipt_id,
            "fund_id": fund_id,
            "acceptance_id": acceptance_id,
            "amount": _money(amt),
            "version": view["version"],
        }
        return self._append("TRANCHE_RELEASED", fund["subject_id"], payload,
                            request_id=request_id, expected_seq=expected_seq)

    @_locked
    def record_recovery(self, *, fund_id: str, acceptance_id: str, amount: Any,
                        receipt_id: str, request_id: str | None = None,
                        expected_seq: int | None = None) -> dict:
        """登记追偿到账：冲净欠款并恢复可用额度，全程金额守恒。"""
        if (prior := self._receipt_lookup(receipt_id, "RECOVERY_RECONCILED")) is not None:
            return prior
        fund = self._require_fund(fund_id)
        self._require_acc(acceptance_id)
        amt = D(amount)
        if amt <= 0:
            raise InvariantViolation("追偿金额必须为正")
        outstanding = acc_net_released(self._state, fund_id, acceptance_id)
        if amt > outstanding:
            raise InvariantViolation(
                f"追偿 {_money(amt)} 超过该资金在该验收的净放款 "
                f"{_money(outstanding)}，金额不守恒"
            )
        payload = {
            "recovery_id": f"rec-{uuid.uuid4().hex[:12]}",
            "receipt_id": receipt_id,
            "fund_id": fund_id,
            "acceptance_id": acceptance_id,
            "amount": _money(amt),
        }
        return self._append("RECOVERY_RECONCILED", fund["subject_id"], payload,
                            request_id=request_id, expected_seq=expected_seq)

    # -- 派生视图 -----------------------------------------------------------

    def fund_position(self, fund_id: str) -> dict:
        fund = self._require_fund(fund_id)
        released = fund_released(self._state, fund_id)
        recovered = fund_recovered(self._state, fund_id)
        outstanding = released - recovered
        available = fund["amount"] - outstanding
        return {
            "fund_id": fund_id,
            "fund_type": fund["fund_type"],
            "purpose": fund["purpose"],
            "committed": _money(fund["amount"]),
            "released": _money(released),
            "recovered": _money(recovered),
            "outstanding": _money(outstanding),
            "available": _money(available),
            "shares": {ci: format(p, "f") for ci, p in fund["shares"].items()},
            "conditions": {
                cid: {"label": c["label"], "met": c["met"], "met_at": c["met_at"]}
                for cid, c in fund["conditions"].items()
            },
        }

    def acceptance_status(self, acceptance_id: str) -> dict:
        acc = self._require_acc(acceptance_id)
        view = _acc_view(acc)
        per_fund = acc_accepted_by_fund(self._state, acceptance_id)
        # 即便某资金在新版本中被完全移除，其既有净放款仍可能产生待追偿额。
        fund_ids = set(per_fund) | {
            r["fund_id"] for r in self._state["releases"]
            if r["acceptance_id"] == acceptance_id
        }
        due_map = {
            fid: recovery_due(self._state, fid, acceptance_id)
            for fid in fund_ids
        }
        return {
            "acceptance_id": acceptance_id,
            "milestone": acc["milestone"],
            "status": acc["status"],
            "submitted_by": acc["submitted_by"],
            "review": None if acc["review"] is None else {
                "reviewer_id": acc["review"]["reviewer_id"],
                "decision": acc["review"]["decision"],
                "reason": acc["review"]["reason"],
            },
            "flag_ids": list(acc["flag_ids"]),
            "current_version": None if view is None else view["version"],
            "accepted_by_fund": {fid: _money(per_fund.get(fid, _ZERO))
                                 for fid in fund_ids},
            "net_released_by_fund": {
                fid: _money(acc_net_released(self._state, fid, acceptance_id))
                for fid in fund_ids
            },
            "recovery_due": {
                fid: _money(d) for fid, d in due_map.items() if d > 0
            },
            "versions": [
                {"version": v["version"], "kind": v["kind"],
                 "change_type": v["change_type"], "reason": v["reason"],
                 "occurred_at": v["occurred_at"], "event_id": v["event_id"]}
                for v in acc["versions"]
            ],
        }

    @staticmethod
    def _mask_party(party_id: str) -> str:
        return f"{party_id[:2]}***{party_id[-3:]}" if len(party_id) > 5 else "***"

    def _mask_fund_ref(self, fund_id: str) -> str:
        """对方资金的对外标识：只保留资金类型 + 出资方脱敏串。"""
        fund = self._state["funds"].get(fund_id)
        if fund is None:
            return "OTHER"
        return f"{fund['fund_type']}:{self._mask_party(fund['funder_id'])}"

    def _finding_visible(self, finding: dict, fund_set: set[str]) -> bool:
        # 额度不足属于单个资金的内部信息：只有该资金自身可见。
        if finding["type"] == "INSUFFICIENT_AVAILABILITY":
            return finding.get("fund_id") in fund_set
        # 其余（跨来源重复、超额覆盖、重复票据）是拆分所必需的共享摘要。
        return True

    def _redact_finding(self, finding: dict, fund_set: set[str]) -> dict:
        """剔除/脱敏 finding 中对方资金合同号。"""
        if finding["type"] == "INSUFFICIENT_AVAILABILITY":
            return dict(finding)  # 可见时 fund_id 必为己方
        redacted = dict(finding)
        for key in ("fund_ids_in_submission", "other_fund_ids", "pending_fund_ids"):
            if key in redacted:
                redacted[key] = sorted({
                    fid if fid in fund_set else self._mask_fund_ref(fid)
                    for fid in redacted[key]
                })
        return redacted

    def funder_view(self, funder_id: str) -> dict:
        """出资方视图：仅见自身合同、自身到账与“必要的”重叠摘要。

        重叠摘要中其他出资方只显示资金类型与脱敏编号，金额仅暴露拆分该成本
        项所必需的部分，不暴露对方合同条款。
        """
        fund_ids = [fid for fid, f in self._state["funds"].items()
                    if f["funder_id"] == funder_id]
        fund_set = set(fund_ids)
        releases = [
            {"receipt_id": r["receipt_id"], "fund_id": r["fund_id"],
             "acceptance_id": r["acceptance_id"], "amount": _money(r["amount"]),
             "occurred_at": r["occurred_at"]}
            for r in self._state["releases"] if r["fund_id"] in fund_set
        ]
        recoveries = [
            {"receipt_id": r["receipt_id"], "fund_id": r["fund_id"],
             "acceptance_id": r["acceptance_id"], "amount": _money(r["amount"]),
             "occurred_at": r["occurred_at"]}
            for r in self._state["recoveries"] if r["fund_id"] in fund_set
        ]
        overlap_summaries = []
        for flag in self._state["flags"]:
            acc = self._state["acceptances"][flag["acceptance_id"]]
            involves_me = any(
                fid in fund_set
                for ci in acc["requested"].values() for fid in ci
            )
            if not involves_me:
                continue
            view = _acc_view(acc)
            summary_costs = []
            for cost_item_id, requested in acc["requested"].items():
                mine = {fid: _money(amt) for fid, amt in requested.items() if fid in fund_set}
                if not mine:
                    continue
                others = []
                if view is not None:
                    for fid, amt in view["allocations"].get(cost_item_id, {}).items():
                        if fid not in fund_set:
                            f = self._state["funds"][fid]
                            others.append({"fund_type": f["fund_type"],
                                           "party_ref": self._mask_party(f["funder_id"]),
                                           "amount": _money(amt)})
                summary_costs.append({
                    "cost_item_id": cost_item_id,
                    "my_requested": mine,
                    "other_accepted": others,
                })
            overlap_summaries.append({
                "flag_id": flag["flag_id"],
                "acceptance_id": flag["acceptance_id"],
                "findings": [self._redact_finding(f, fund_set)
                             for f in flag["findings"]
                             if self._finding_visible(f, fund_set)],
                "costs": summary_costs,
                "resolved": flag["resolved_by"] is not None,
            })
        return {
            "funder_id": funder_id,
            "contracts": [self.fund_position(fid) for fid in fund_ids],
            "releases": releases,
            "recoveries": recoveries,
            "overlap_summaries": overlap_summaries,
        }

    def regulator_view(self) -> dict:
        """监管视图：全局可见，且可独立复算所有守恒口径。"""
        report = self.recompute()
        return {
            "subjects": [
                {"subject_id": sid, "project_name": s["project_name"],
                 "party_ids": s["party_ids"]}
                for sid, s in self._state["subjects"].items()
            ],
            "funds": [self.fund_position(fid) for fid in self._state["funds"]],
            "acceptances": [
                self.acceptance_status(aid) for aid in self._state["acceptances"]
            ],
            "overlap_flags": [
                {"flag_id": f["flag_id"], "acceptance_id": f["acceptance_id"],
                 "findings": f["findings"], "resolved": f["resolved_by"] is not None}
                for f in self._state["flags"]
            ],
            "releases": [
                {"receipt_id": r["receipt_id"], "fund_id": r["fund_id"],
                 "acceptance_id": r["acceptance_id"], "amount": _money(r["amount"])}
                for r in self._state["releases"]
            ],
            "recoveries": [
                {"receipt_id": r["receipt_id"], "fund_id": r["fund_id"],
                 "acceptance_id": r["acceptance_id"], "amount": _money(r["amount"])}
                for r in self._state["recoveries"]
            ],
            "recomputation": report,
        }

    def project_view(self, subject_id: str) -> dict:
        """项目方视图：资金、验收、版本、到账全景，可逐笔下钻。"""
        fund_ids = [fid for fid, f in self._state["funds"].items()
                    if f["subject_id"] == subject_id]
        acc_ids = [aid for aid, a in self._state["acceptances"].items()
                   if a["subject_id"] == subject_id]
        receipts = []
        for r in self._state["releases"]:
            if r["subject_id"] == subject_id:
                receipts.append(self.trace_release(r["receipt_id"]))
        return {
            "subject_id": subject_id,
            "funds": [self.fund_position(fid) for fid in fund_ids],
            "acceptances": [self.acceptance_status(aid) for aid in acc_ids],
            "receipts": receipts,
        }

    def trace_release(self, receipt_id: str) -> dict:
        """从任一到账回单追到：成本项 → 验收版本 → 审核决定 → 重叠提示 → 合同 → 申请。"""
        entry = self._state["receipts"].get(receipt_id)
        if entry is None:
            raise UnknownReference(f"回单 {receipt_id} 不存在")
        release = next(r for r in self._state["releases"] if r["receipt_id"] == receipt_id)
        fund = self._state["funds"][release["fund_id"]]
        acc = self._state["acceptances"][release["acceptance_id"]]
        version = next(v for v in acc["versions"] if v["version"] == release["version"])
        costs = []
        for cost_item_id, alloc in version["allocations"].items():
            if release["fund_id"] in alloc:
                ci = self._state["cost_items"][cost_item_id]
                costs.append({
                    "cost_item_id": cost_item_id,
                    "name": ci["name"],
                    "cost_amount": _money(ci["amount"]),
                    "covered_by_this_fund_in_version": _money(alloc[release["fund_id"]]),
                })
        chain = {
            "receipt_id": receipt_id,
            "amount": _money(release["amount"]),
            "occurred_at": release["occurred_at"],
            "release_event_id": release["event_id"],
            "fund": {
                "fund_id": release["fund_id"],
                "funder_id": fund["funder_id"],
                "fund_type": fund["fund_type"],
                "purpose": fund["purpose"],
                "committed": _money(fund["amount"]),
                "shares": {ci: format(p, "f") for ci, p in fund["shares"].items()},
                "lock_event_id": fund["event_id"],
            },
            "acceptance": {
                "acceptance_id": acc["acceptance_id"],
                "milestone": acc["milestone"],
                "submitted_by": acc["submitted_by"],
                "submit_event_id": acc["event_id"],
                "version_paid": release["version"],
                "version_event_id": version["event_id"],
                "supersedes_event": version.get("supersedes_event"),
                "receipt_ids": version["receipt_ids"],
                "costs": costs,
            },
            "review": None if acc["review"] is None else {
                "reviewer_id": acc["review"]["reviewer_id"],
                "decision": acc["review"]["decision"],
                "reason": acc["review"]["reason"],
                "review_event_id": acc["review"]["event_id"],
            },
            "overlap_flags": [
                {"flag_id": fid,
                 "event_id": next(f["event_id"] for f in self._state["flags"]
                                  if f["flag_id"] == fid)}
                for fid in acc["flag_ids"]
            ],
            "application_event_id": self._state["subjects"][release["subject_id"]]["event_id"],
            "subject_id": release["subject_id"],
        }
        return chain

    def recompute(self) -> dict:
        """从当前状态复算全局守恒口径（监管可用事件序列独立再算一遍核对）。"""
        fund_rows = []
        funds_ok = True
        for fid, fund in self._state["funds"].items():
            released = fund_released(self._state, fid)
            recovered = fund_recovered(self._state, fid)
            outstanding = released - recovered
            available = fund["amount"] - outstanding
            conserved = (available + outstanding == fund["amount"]) and outstanding >= 0
            funds_ok &= conserved
            fund_rows.append({
                "fund_id": fid,
                "committed": _money(fund["amount"]),
                "released": _money(released),
                "recovered": _money(recovered),
                "outstanding": _money(outstanding),
                "available": _money(available),
                "conserved": conserved,
            })
        cost_rows = []
        costs_ok = True
        for ci_id, ci in self._state["cost_items"].items():
            accepted = sum(
                (sum(_acc_view(a)["allocations"].get(ci_id, {}).values(), _ZERO)
                 for a in self._state["acceptances"].values() if _acc_view(a)),
                _ZERO,
            )
            within = accepted <= ci["amount"]
            costs_ok &= within
            cost_rows.append({
                "cost_item_id": ci_id,
                "cost_amount": _money(ci["amount"]),
                "accepted_coverage": _money(accepted),
                "within_cost": within,
            })
        due = []
        for aid, acc in self._state["acceptances"].items():
            if _acc_view(acc) is None:
                continue
            fund_ids = set(acc_accepted_by_fund(self._state, aid)) | {
                r["fund_id"] for r in self._state["releases"]
                if r["acceptance_id"] == aid
            }
            for fid in fund_ids:
                d = recovery_due(self._state, fid, aid)
                if d > 0:
                    due.append({"acceptance_id": aid, "fund_id": fid,
                                "due": _money(d)})
        total_committed = sum((f["amount"] for f in self._state["funds"].values()), _ZERO)
        total_outstanding = sum(
            (fund_outstanding(self._state, fid) for fid in self._state["funds"]), _ZERO)
        total_available = sum(
            (fund_available(self._state, fid) for fid in self._state["funds"]), _ZERO)
        return {
            "ok": funds_ok and costs_ok and not due
            and total_committed == total_outstanding + total_available,
            "funds": fund_rows,
            "cost_items": cost_rows,
            "recovery_due": due,
            "totals": {
                "committed": _money(total_committed),
                "outstanding": _money(total_outstanding),
                "available": _money(total_available),
                "conserved": total_committed == total_outstanding + total_available,
            },
        }

    # -- 内部 ---------------------------------------------------------------

    def _require_fund(self, fund_id: str) -> dict:
        fund = self._state["funds"].get(fund_id)
        if fund is None:
            raise UnknownReference(f"资金 {fund_id} 不存在")
        return fund

    def _require_acc(self, acceptance_id: str) -> dict:
        acc = self._state["acceptances"].get(acceptance_id)
        if acc is None:
            raise UnknownReference(f"验收 {acceptance_id} 不存在")
        return acc
