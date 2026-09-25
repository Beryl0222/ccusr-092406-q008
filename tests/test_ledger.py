"""穿透式进度账本的契约与不变式测试。"""

from __future__ import annotations

import json
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from src.culture_finance_progress import (
    AMEND_REVOCATION,
    AMEND_SUPPLEMENT,
    ConcurrencyError,
    D,
    DuplicateSubmission,
    EVENT_KINDS,
    InvariantViolation,
    LedgerError,
    ProgressLedger,
    UnknownReference,
    fold,
    validate_event,
)

SUBJECT = "p-demo"


def build_ledger(*, conditions_bank=None, conditions_gov=None) -> ProgressLedger:
    """标准三方资金场景：同一台 100 元的设备，银行 60 / 财政 40 / 社投另立。"""
    led = ProgressLedger()
    led.submit_application(SUBJECT, "数字非遗展厅", ["proj-co"], request_id="app-1")
    led.lock_fund(
        fund_id="F-BANK", subject_id=SUBJECT, funder_id="bank-001",
        fund_type="BANK_LOAN", amount="60.00", purpose="设备采购贷款",
        cost_items=[{"cost_item_id": "C-EQ", "name": "4K放映设备", "amount": "100.00"}],
        shares={"C-EQ": "60"},
        conditions=[{"condition_id": "G1", "label": "授信批复"}] if conditions_bank is None else conditions_bank,
        request_id="lock-bank",
    )
    led.lock_fund(
        fund_id="F-GOV", subject_id=SUBJECT, funder_id="gov-001",
        fund_type="FISCAL_SUBSIDY", amount="40.00", purpose="设备购置补贴",
        cost_items=[{"cost_item_id": "C-EQ", "name": "4K放映设备", "amount": "100.00"}],
        shares={"C-EQ": "40"},
        conditions=conditions_gov or [],
        request_id="lock-gov",
    )
    led.lock_fund(
        fund_id="F-SOC", subject_id=SUBJECT, funder_id="soc-001",
        fund_type="SOCIAL_INVESTMENT", amount="50.00", purpose="内容制作跟投",
        cost_items=[{"cost_item_id": "C-CONTENT", "name": "纪录片制作", "amount": "50.00"}],
        shares={"C-CONTENT": "100"},
        request_id="lock-soc",
    )
    return led


def submit_duplicate_device(led: ProgressLedger, *, acceptance_id="A1",
                            requested=None, receipt_ids=("INV-1",)):
    return led.submit_acceptance(
        acceptance_id=acceptance_id, subject_id=SUBJECT, submitted_by="proj-user",
        milestone="设备到场验收",
        requested=requested or {"C-EQ": {"F-BANK": "100", "F-GOV": "100"}},
        receipt_ids=list(receipt_ids), request_id=f"sub-{acceptance_id.lower()}",
    )


def split_and_accept(led: ProgressLedger, acceptance_id="A1", *,
                     allocation=None, reviewer="auditor-1", request_id=None):
    return led.review_acceptance(
        acceptance_id=acceptance_id, reviewer_id=reviewer, decision="SPLIT",
        reason="按合同 60/40 拆分，覆盖同一台设备的全额成本",
        allocations=allocation or {"C-EQ": {"F-BANK": "60", "F-GOV": "40"}},
        request_id=request_id or f"rev-{acceptance_id.lower()}",
    )


class EventContractTest(unittest.TestCase):
    def test_event_kinds_cover_full_lifecycle(self):
        for kind in ("APPLICATION_SUBMITTED", "FUND_LOCKED", "CONDITION_MET",
                     "ACCEPTANCE_SUBMITTED", "OVERLAP_FLAGGED",
                     "ACCEPTANCE_REVIEWED", "MILESTONE_ACCEPTED",
                     "ACCEPTANCE_AMENDED", "TRANCHE_RELEASED",
                     "RECOVERY_RECONCILED"):
            self.assertIn(kind, EVENT_KINDS)

    def test_sample_still_matches_contract(self):
        sample = json.loads(
            (Path(__file__).parents[1] / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_validate_rejects_bad_kind_and_payload(self):
        self.assertIn("kind", validate_event(
            {"event_id": "x", "kind": "NOPE", "occurred_at": "t",
             "subject_id": "s", "payload": {}}))
        self.assertIn("payload", validate_event(
            {"event_id": "x", "kind": "FUND_LOCKED", "occurred_at": "t",
             "subject_id": "s", "payload": []}))


class OverlapDetectionTest(unittest.TestCase):
    def setUp(self):
        self.led = build_ledger()

    def test_same_acceptance_cross_source_duplicate_is_flagged(self):
        _, flags = submit_duplicate_device(self.led)
        self.assertEqual(len(flags), 1)
        types = {f["type"] for f in flags[0]["payload"]["findings"]}
        self.assertIn("CROSS_FUND_DUPLICATE", types)
        self.assertIn("COST_OVER_COVERAGE", types)
        finding = next(f for f in flags[0]["payload"]["findings"]
                       if f["type"] == "CROSS_FUND_DUPLICATE")
        self.assertEqual(set(finding["fund_ids_in_submission"]), {"F-BANK", "F-GOV"})

    def test_suggested_split_follows_contract_shares(self):
        _, flags = submit_duplicate_device(self.led)
        split = flags[0]["payload"]["suggested_split"]["C-EQ"]
        self.assertEqual(split, {"F-BANK": "60.00", "F-GOV": "40.00"})

    def test_flag_is_advisory_no_money_moves_and_status_stays_submitted(self):
        submit_duplicate_device(self.led)
        status = self.led.acceptance_status("A1")
        self.assertEqual(status["status"], "SUBMITTED")
        self.assertIsNone(status["current_version"])
        with self.assertRaises(LedgerError):
            self.led.release_tranche(fund_id="F-GOV", acceptance_id="A1",
                                     amount="40", receipt_id="R-1")

    def test_duplicate_invoice_is_flagged_across_acceptances(self):
        submit_duplicate_device(self.led, acceptance_id="A1", receipt_ids=("INV-DUP",))
        _, flags = submit_duplicate_device(
            self.led, acceptance_id="A2", receipt_ids=("INV-DUP",))
        types = {f["type"] for f in flags[0]["payload"]["findings"]}
        self.assertIn("DUPLICATE_RECEIPT", types)

    def test_pending_parallel_submission_is_flagged(self):
        # A1 已提交但未审定，A2 又报同一设备：在途重复也要提示。
        submit_duplicate_device(self.led, acceptance_id="A1")
        _, flags = submit_duplicate_device(
            self.led, acceptance_id="A2",
            requested={"C-EQ": {"F-GOV": "40"}})
        finding = next(f for f in flags[0]["payload"]["findings"]
                       if f["type"] == "CROSS_FUND_DUPLICATE")
        self.assertIn("F-GOV", finding["pending_fund_ids"])


class IndependentReviewTest(unittest.TestCase):
    def setUp(self):
        self.led = build_ledger()
        submit_duplicate_device(self.led)

    def test_submitter_cannot_review(self):
        with self.assertRaises(LedgerError):
            self.led.review_acceptance(
                acceptance_id="A1", reviewer_id="proj-user", decision="APPROVE")

    def test_reviewer_can_reject_and_nothing_is_releasable(self):
        events = self.led.review_acceptance(
            acceptance_id="A1", reviewer_id="auditor-1", decision="REJECT",
            reason="票据不足", request_id="rev-reject")
        self.assertEqual([e["kind"] for e in events], ["ACCEPTANCE_REVIEWED"])
        self.assertEqual(self.led.acceptance_status("A1")["status"], "REJECTED")
        with self.assertRaises(LedgerError):
            self.led.release_tranche(fund_id="F-GOV", acceptance_id="A1",
                                     amount="1", receipt_id="R-1")

    def test_reviewer_cannot_approve_coverage_exceeding_cost(self):
        # 自动机只提示，但成本红线谁都不能越过：全额双赔被拒。
        with self.assertRaises(InvariantViolation):
            self.led.review_acceptance(
                acceptance_id="A1", reviewer_id="auditor-1", decision="APPROVE")

    def test_review_is_one_shot(self):
        split_and_accept(self.led)
        with self.assertRaises(DuplicateSubmission):
            split_and_accept(self.led, request_id="rev-again")

    def test_split_allocations_must_reference_submitted_cost_items(self):
        with self.assertRaises(UnknownReference):
            self.led.review_acceptance(
                acceptance_id="A1", reviewer_id="auditor-1", decision="SPLIT",
                allocations={"C-CONTENT": {"F-SOC": "10"}})


class TrancheReleaseTest(unittest.TestCase):
    def setUp(self):
        self.led = build_ledger()
        submit_duplicate_device(self.led)
        split_and_accept(self.led)
        self.led.mark_condition_met(fund_id="F-BANK", condition_id="G1")

    def test_partial_acceptance_releases_only_matching_share(self):
        self.led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                                 amount="30", receipt_id="BK-1")
        pos = self.led.fund_position("F-BANK")
        self.assertEqual(pos["released"], "30.00")
        self.assertEqual(pos["outstanding"], "30.00")
        self.assertEqual(pos["available"], "30.00")
        status = self.led.acceptance_status("A1")
        self.assertEqual(status["net_released_by_fund"]["F-BANK"], "30.00")

    def test_release_caps_at_accepted_amount_and_availability(self):
        self.led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                                 amount="60", receipt_id="BK-1")
        with self.assertRaises(InvariantViolation):
            self.led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                                     amount="0.01", receipt_id="BK-2")

    def test_unmet_effective_condition_blocks_release(self):
        # F-GOV 没有条件；给它补一个条件后应被闸门拦住。
        led = build_ledger(conditions_gov=[{"condition_id": "H1", "label": "公示完成"}])
        submit_duplicate_device(led)
        split_and_accept(led)
        with self.assertRaises(LedgerError):
            led.release_tranche(fund_id="F-GOV", acceptance_id="A1",
                                amount="40", receipt_id="GV-1")
        led.mark_condition_met(fund_id="F-GOV", condition_id="H1")
        event = led.release_tranche(fund_id="F-GOV", acceptance_id="A1",
                                    amount="40", receipt_id="GV-1")
        self.assertEqual(event["payload"]["amount"], "40.00")

    def test_repeated_receipt_never_double_pays(self):
        first = self.led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                                         amount="30", receipt_id="BK-1")
        again = self.led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                                         amount="30", receipt_id="BK-1")
        self.assertEqual(first["event_id"], again["event_id"])
        self.assertEqual(self.led.fund_position("F-BANK")["released"], "30.00")

    def test_receipt_id_collision_across_release_and_recovery_rejected(self):
        self.led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                                 amount="10", receipt_id="SAME")
        with self.assertRaises(LedgerError):
            self.led.record_recovery(fund_id="F-BANK", acceptance_id="A1",
                                     amount="10", receipt_id="SAME")

    def test_concurrent_releases_cannot_overpay(self):
        barrier = threading.Barrier(2)
        outcomes: list[BaseException | None] = []

        def worker(amount, receipt):
            barrier.wait()
            try:
                self.led.release_tranche(
                    fund_id="F-BANK", acceptance_id="A1",
                    amount=amount, receipt_id=receipt)
                outcomes.append(None)
            except BaseException as exc:  # noqa: BLE001 - 记录断言用
                outcomes.append(exc)

        t1 = threading.Thread(target=worker, args=("40", "BK-C1"))
        t2 = threading.Thread(target=worker, args=("40", "BK-C2"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sum(1 for o in outcomes if o is None), 1)
        self.assertIsInstance(next(o for o in outcomes if o), InvariantViolation)
        self.assertEqual(self.led.fund_position("F-BANK")["released"], "40.00")

    def test_optimistic_concurrency_token_rejects_stale_request(self):
        stale = self.led.seq
        self.led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                                 amount="10", receipt_id="BK-O1")
        with self.assertRaises(ConcurrencyError):
            self.led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                                     amount="10", receipt_id="BK-O2",
                                     expected_seq=stale)


class VersionAndRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.led = build_ledger()
        submit_duplicate_device(self.led)
        split_and_accept(self.led)
        self.led.mark_condition_met(fund_id="F-BANK", condition_id="G1")
        self.led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                                 amount="60", receipt_id="BK-FULL")
        self.led.release_tranche(fund_id="F-GOV", acceptance_id="A1",
                                 amount="40", receipt_id="GV-FULL")

    def test_original_release_is_never_rewritten(self):
        before = next(e for e in self.led.events()
                      if e["payload"].get("receipt_id") == "BK-FULL")
        seq_before = self.led.seq
        self.led.amend_acceptance(
            acceptance_id="A1", reviewer_id="auditor-1",
            change_type=AMEND_REVOCATION,
            allocations={"C-EQ": {"F-BANK": "30", "F-GOV": "40"}},
            reason="验收复核发现设备折价", request_id="amend-1")
        after = next(e for e in self.led.events()
                     if e["payload"].get("receipt_id") == "BK-FULL")
        self.assertEqual(before, after)
        self.assertEqual(after["seq"], before["seq"])
        self.assertGreater(self.led.seq, seq_before)
        status = self.led.acceptance_status("A1")
        self.assertEqual([v["version"] for v in status["versions"]], [1, 2])
        self.assertEqual(status["versions"][1]["change_type"], AMEND_REVOCATION)

    def test_revocation_creates_recovery_due_and_blocks_further_release(self):
        self.led.amend_acceptance(
            acceptance_id="A1", reviewer_id="auditor-1",
            change_type=AMEND_REVOCATION,
            allocations={"C-EQ": {"F-BANK": "30", "F-GOV": "40"}},
            reason="折价", request_id="amend-down")
        self.assertEqual(self.led.acceptance_status("A1")["recovery_due"],
                         {"F-BANK": "30.00"})
        with self.assertRaises(InvariantViolation):
            self.led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                                     amount="1", receipt_id="BK-X")

    def test_recovery_restores_availability_and_conserves_money(self):
        self.led.amend_acceptance(
            acceptance_id="A1", reviewer_id="auditor-1",
            change_type=AMEND_REVOCATION,
            allocations={"C-EQ": {"F-BANK": "30", "F-GOV": "40"}},
            reason="折价", request_id="amend-down2")
        # 追偿总额不得超过净放款（60）；recovery_due(30) 是再放款前的最低追偿额
        with self.assertRaises(InvariantViolation):
            self.led.record_recovery(fund_id="F-BANK", acceptance_id="A1",
                                     amount="60.01", receipt_id="RC-X")
        # 只追回 10：欠款仍有 20，继续放款仍被拦
        self.led.record_recovery(fund_id="F-BANK", acceptance_id="A1",
                                 amount="10", receipt_id="RC-1")
        self.assertEqual(self.led.acceptance_status("A1")["recovery_due"],
                         {"F-BANK": "20.00"})
        with self.assertRaises(InvariantViolation):
            self.led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                                     amount="1", receipt_id="BK-MORE")
        # 补足剩余 20 后守恒闭合
        self.led.record_recovery(fund_id="F-BANK", acceptance_id="A1",
                                 amount="20", receipt_id="RC-2")
        pos = self.led.fund_position("F-BANK")
        self.assertEqual(pos["released"], "60.00")
        self.assertEqual(pos["recovered"], "30.00")
        self.assertEqual(pos["outstanding"], "30.00")
        self.assertEqual(pos["available"], "30.00")
        self.assertEqual(D(pos["available"]) + D(pos["outstanding"]), D(pos["committed"]))
        self.assertEqual(self.led.acceptance_status("A1")["recovery_due"], {})
        report = self.led.recompute()
        self.assertTrue(report["ok"])
        self.assertTrue(report["totals"]["conserved"])

    def test_revocation_removing_fund_entirely_still_creates_recovery_due(self):
        # 新版本把财政份额完全移除：其净放款 40 应全部转追偿，且复算可见。
        self.led.amend_acceptance(
            acceptance_id="A1", reviewer_id="auditor-1",
            change_type=AMEND_REVOCATION,
            allocations={"C-EQ": {"F-BANK": "60"}},
            reason="财政补贴资格撤销", request_id="amend-pull")
        status = self.led.acceptance_status("A1")
        self.assertEqual(status["recovery_due"], {"F-GOV": "40.00"})
        self.assertEqual(status["accepted_by_fund"]["F-GOV"], "0.00")
        self.assertFalse(self.led.recompute()["ok"])  # 尚有未追回欠款
        with self.assertRaises(InvariantViolation):
            self.led.release_tranche(fund_id="F-GOV", acceptance_id="A1",
                                     amount="1", receipt_id="GV-X")
        self.led.record_recovery(fund_id="F-GOV", acceptance_id="A1",
                                 amount="40", receipt_id="RC-GOV")
        self.assertTrue(self.led.recompute()["ok"])
        self.assertEqual(self.led.fund_position("F-GOV")["available"], "40.00")

    def test_supplement_version_can_lift_cap_and_release_more(self):
        # 先只定稿银行 30，后补票据新版本上调到 60，可继续放差额。
        led = build_ledger()
        submit_duplicate_device(led)
        led.review_acceptance(
            acceptance_id="A1", reviewer_id="auditor-1", decision="SPLIT",
            allocations={"C-EQ": {"F-BANK": "30", "F-GOV": "20"}},
            request_id="rev-small")
        led.mark_condition_met(fund_id="F-BANK", condition_id="G1")
        led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                            amount="30", receipt_id="BK-1")
        led.amend_acceptance(
            acceptance_id="A1", reviewer_id="auditor-2",
            change_type=AMEND_SUPPLEMENT, added_receipt_ids=["INV-S2"],
            allocations={"C-EQ": {"F-BANK": "60", "F-GOV": "40"}},
            reason="后补发票", request_id="amend-up")
        status = led.acceptance_status("A1")
        self.assertEqual(status["current_version"], 2)
        self.assertEqual(status["accepted_by_fund"]["F-BANK"], "60.00")
        led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                            amount="30", receipt_id="BK-2")
        self.assertEqual(led.fund_position("F-BANK")["released"], "60.00")

    def test_cumulative_accepted_coverage_cannot_exceed_cost_across_acceptances(self):
        # A1 已覆盖设备全额；A2 再就同一设备申报时只收到提示，审核定稿才触红线。
        led = build_ledger()
        submit_duplicate_device(led, acceptance_id="A1")
        split_and_accept(led, acceptance_id="A1", request_id="rev-a1")
        _, flags = submit_duplicate_device(
            led, acceptance_id="A2", requested={"C-EQ": {"F-SOC": "0.01"}})
        types = {f["type"] for f in flags[0]["payload"]["findings"]}
        self.assertIn("COST_OVER_COVERAGE", types)
        with self.assertRaises(InvariantViolation):
            led.review_acceptance(
                acceptance_id="A2", reviewer_id="auditor-9", decision="APPROVE")
        # 被红线拦下后，没有任何放款发生，全局守恒仍然成立
        self.assertTrue(led.recompute()["ok"])


class IdempotencyTest(unittest.TestCase):
    def test_request_id_replay_returns_same_events(self):
        led = build_ledger()
        e1, f1 = submit_duplicate_device(led, acceptance_id="A9")
        e2, f2 = submit_duplicate_device(led, acceptance_id="A9")
        self.assertEqual(e1["event_id"], e2["event_id"])
        self.assertEqual(f1[0]["event_id"], f2[0]["event_id"])
        self.assertEqual(len(led.events()), 6)  # 申请+3锁定+提交+提示

    def test_request_id_replay_across_review_stages(self):
        led = build_ledger()
        submit_duplicate_device(led)
        r1 = split_and_accept(led, request_id="rv-x")
        r2 = split_and_accept(led, request_id="rv-x")
        self.assertEqual([e["event_id"] for e in r1],
                         [e["event_id"] for e in r2])

    def test_same_request_id_different_operation_rejected(self):
        led = build_ledger()
        led.submit_application("p2", "另一项目", request_id="dup-rid")
        with self.assertRaises(LedgerError):
            led.mark_condition_met(fund_id="F-BANK", condition_id="G1",
                                   request_id="dup-rid")

    def test_duplicate_business_keys_rejected_without_request_id(self):
        led = build_ledger()
        with self.assertRaises(DuplicateSubmission):
            led.submit_application(SUBJECT, "重复立项")
        with self.assertRaises(DuplicateSubmission):
            led.lock_fund(fund_id="F-BANK", subject_id=SUBJECT, funder_id="b",
                          fund_type="BANK_LOAN", amount="1", purpose="x",
                          cost_items=[{"cost_item_id": "C-EQ", "amount": "100"}])


class PersistenceAndCrashTest(unittest.TestCase):
    def _bootstrap_file(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "journal.jsonl"
        led = ProgressLedger(path)
        led.submit_application(SUBJECT, "展厅", request_id="app")
        led.lock_fund(
            fund_id="F-GOV", subject_id=SUBJECT, funder_id="gov",
            fund_type="FISCAL_SUBSIDY", amount="40", purpose="补贴",
            cost_items=[{"cost_item_id": "C-EQ", "name": "设备", "amount": "100"}],
            shares={"C-EQ": "40"}, request_id="lock")
        return path

    def test_reopen_replays_full_state(self):
        path = self._bootstrap_file()
        led = ProgressLedger(path)
        self.assertEqual(led.fund_position("F-GOV")["committed"], "40.00")
        led.submit_acceptance(
            acceptance_id="A1", subject_id=SUBJECT, submitted_by="u",
            milestone="m", requested={"C-EQ": {"F-GOV": "40"}}, request_id="sub")
        again = ProgressLedger(path)
        self.assertEqual(again.seq, led.seq)
        self.assertIn("A1", again.state["acceptances"])

    def test_torn_tail_line_is_discarded_on_open(self):
        path = self._bootstrap_file()
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"event_id": "evt-half", "kind": "TRANCHE_RELEASED"')  # 无换行
        led = ProgressLedger(path)
        self.assertEqual(led.seq, 2)
        # 干净重写后，撕裂内容不再残留
        self.assertNotIn("evt-half", path.read_text(encoding="utf-8"))

    def test_resume_submission_after_mid_command_crash(self):
        path = self._bootstrap_file()
        # 手工制造“提交事件已落盘、提示事件未落盘”的崩溃现场。
        submit_event = {
            "event_id": "evt-manual-sub", "kind": "ACCEPTANCE_SUBMITTED",
            "occurred_at": "2026-09-23T10:00:00+08:00", "subject_id": SUBJECT,
            "seq": 3, "request_id": "sub-crash", "stage": 0,
            "payload": {
                "acceptance_id": "A1", "submitted_by": "u", "milestone": "m",
                "requested": {"C-EQ": {"F-GOV": "100"}}, "receipt_ids": []},
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(submit_event, ensure_ascii=False) + "\n")
        led = ProgressLedger(path)
        event, flags = led.submit_acceptance(
            acceptance_id="A1", subject_id=SUBJECT, submitted_by="u",
            milestone="m", requested={"C-EQ": {"F-GOV": "100"}},
            request_id="sub-crash")
        self.assertEqual(event["event_id"], "evt-manual-sub")
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["kind"], "OVERLAP_FLAGGED")
        # 再次重试仍然幂等，不产生第二条提示
        _, flags2 = led.submit_acceptance(
            acceptance_id="A1", subject_id=SUBJECT, submitted_by="u",
            milestone="m", requested={"C-EQ": {"F-GOV": "100"}},
            request_id="sub-crash")
        self.assertEqual(flags[0]["event_id"], flags2[0]["event_id"])

    def test_resume_review_after_mid_command_crash(self):
        path = self._bootstrap_file()
        led = ProgressLedger(path)
        led.submit_acceptance(
            acceptance_id="A1", subject_id=SUBJECT, submitted_by="u",
            milestone="m", requested={"C-EQ": {"F-GOV": "40"}}, request_id="sub")
        review_event = {
            "event_id": "evt-manual-rev", "kind": "ACCEPTANCE_REVIEWED",
            "occurred_at": "2026-09-23T11:00:00+08:00", "subject_id": SUBJECT,
            "seq": led.seq + 1, "request_id": "rev-crash", "stage": 0,
            "payload": {
                "acceptance_id": "A1", "reviewer_id": "aud-1",
                "decision": "APPROVE", "reason": "",
                "resolved_flags": [], "flag_resolutions": {},
                "final_allocations": {"C-EQ": {"F-GOV": "40.00"}}},
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(review_event, ensure_ascii=False) + "\n")
        led = ProgressLedger(path)
        events = led.review_acceptance(
            acceptance_id="A1", reviewer_id="aud-1", decision="APPROVE",
            request_id="rev-crash")
        self.assertEqual([e["event_id"] for e in events][0], "evt-manual-rev")
        self.assertEqual(events[1]["kind"], "MILESTONE_ACCEPTED")
        self.assertEqual(led.acceptance_status("A1")["current_version"], 1)


class ViewsAndTraceabilityTest(unittest.TestCase):
    def setUp(self):
        self.led = build_ledger()
        submit_duplicate_device(self.led)
        split_and_accept(self.led)
        self.led.mark_condition_met(fund_id="F-BANK", condition_id="G1")
        self.led.release_tranche(fund_id="F-BANK", acceptance_id="A1",
                                 amount="60", receipt_id="BK-60")
        self.led.release_tranche(fund_id="F-GOV", acceptance_id="A1",
                                 amount="40", receipt_id="GV-40")

    def test_funder_sees_only_own_contract_and_masked_overlap_summary(self):
        view = self.led.funder_view("bank-001")
        self.assertEqual([c["fund_id"] for c in view["contracts"]], ["F-BANK"])
        self.assertEqual({r["receipt_id"] for r in view["releases"]}, {"BK-60"})
        self.assertEqual(view["recoveries"], [])
        summary = view["overlap_summaries"][0]
        cost = summary["costs"][0]
        self.assertIn("F-BANK", cost["my_requested"])
        self.assertTrue(all(o["fund_type"] == "FISCAL_SUBSIDY"
                            and "***" in o["party_ref"]
                            for o in cost["other_accepted"]))
        # 银行视图不出现财政出资方标识，也不出现财政合同号
        flat = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("gov-001", flat)
        self.assertNotIn("F-GOV", flat)
        # 但仍以脱敏的资金类型摘要告知“还有谁覆盖了同一成本项”
        cross = next(f for f in summary["findings"]
                     if f["type"] == "CROSS_FUND_DUPLICATE")
        refs = cross["fund_ids_in_submission"] + cross["other_fund_ids"] \
            + cross["pending_fund_ids"]
        self.assertTrue(any("FISCAL_SUBSIDY" in ref for ref in refs))
        self.assertIn("F-BANK", cross["fund_ids_in_submission"])
        # 对方资金额度不足属于对方内部信息，不进入银行视图；己方的保留
        insuff = [f for f in summary["findings"]
                  if f["type"] == "INSUFFICIENT_AVAILABILITY"]
        self.assertTrue(insuff)
        self.assertTrue(all(f["fund_id"] == "F-BANK" for f in insuff))

    def test_other_funder_without_stake_sees_no_summary(self):
        view = self.led.funder_view("soc-001")
        self.assertEqual(view["overlap_summaries"], [])
        self.assertEqual([c["fund_id"] for c in view["contracts"]], ["F-SOC"])

    def test_regulator_view_is_global_and_recomputable(self):
        reg = self.led.regulator_view()
        self.assertEqual({f["fund_id"] for f in reg["funds"]},
                         {"F-BANK", "F-GOV", "F-SOC"})
        self.assertTrue(reg["recomputation"]["ok"])
        # 监管从导出的事件序列独立重放，应得到同样的全局口径
        replayed = fold(self.led.events())
        rep_replayed = ProgressLedger()
        rep_replayed._state = replayed
        self.assertEqual(
            [(f["fund_id"], f["outstanding"]) for f in reg["funds"]],
            [(row["fund_id"], row["outstanding"])
             for row in rep_replayed.recompute()["funds"]])

    def test_trace_release_walks_cost_acceptance_review_chain(self):
        chain = self.led.trace_release("BK-60")
        self.assertEqual(chain["amount"], "60.00")
        self.assertEqual(chain["fund"]["fund_id"], "F-BANK")
        self.assertTrue(chain["fund"]["lock_event_id"])
        cost = chain["acceptance"]["costs"][0]
        self.assertEqual(cost["cost_item_id"], "C-EQ")
        self.assertEqual(cost["cost_amount"], "100.00")
        self.assertEqual(chain["acceptance"]["version_paid"], 1)
        self.assertTrue(chain["acceptance"]["version_event_id"])
        self.assertEqual(chain["review"]["decision"], "SPLIT")
        self.assertTrue(chain["review"]["review_event_id"])
        self.assertEqual(len(chain["overlap_flags"]), 1)
        self.assertTrue(chain["application_event_id"])
        with self.assertRaises(UnknownReference):
            self.led.trace_release("NOT-EXIST")

    def test_project_view_lists_every_receipt_with_trace(self):
        pv = self.led.project_view(SUBJECT)
        receipts = {r["receipt_id"] for r in pv["receipts"]}
        self.assertEqual(receipts, {"BK-60", "GV-40"})
        self.assertTrue(all(r["acceptance"]["costs"] for r in pv["receipts"]))

    def test_global_conservation_holds(self):
        report = self.led.recompute()
        self.assertTrue(report["ok"])
        totals = report["totals"]
        self.assertEqual(D(totals["committed"]),
                         D(totals["outstanding"]) + D(totals["available"]))
        for row in report["cost_items"]:
            self.assertLessEqual(D(row["accepted_coverage"]), D(row["cost_amount"]))


if __name__ == "__main__":
    unittest.main()
