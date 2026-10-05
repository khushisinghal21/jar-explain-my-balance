"""Find the cases where explaining isn't enough and a person must act.

Most "unexpected deduction" complaints are explainable (GST, the buy/sell gap, price). These aren't:
  duplicate_debit   two debits for the same mandate on the same day
  stuck_debit       money taken, no gold credited, past the refund SLA
  amount_mismatch   an auto-debit for a different amount than the mandate
  late_withdrawal   gold sold, cash not paid out, past the payout SLA
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .ledger import Debit, Ledger, Pricing, Withdrawal


@dataclass
class Issue:
    kind: str
    ref: str
    detail: dict

    @property
    def urgent(self) -> bool:
        return self.kind in {"duplicate_debit", "stuck_debit", "late_withdrawal"}


def recent_debits(lg: Ledger, now: datetime, days: int = 7) -> list[Debit]:
    return sorted((d for d in lg.debits if d.at >= now - timedelta(days=days)), key=lambda d: d.at, reverse=True)


def debit_issues(lg: Ledger, pricing: Pricing, now: datetime) -> list[Issue]:
    issues: list[Issue] = []
    by_day: dict[tuple, list[Debit]] = {}
    for d in lg.debits:
        if d.source in ("daily", "weekly") and d.status != "refunded":
            by_day.setdefault((d.source, d.at.date()), []).append(d)
    for (_, day), ds in by_day.items():
        if len(ds) > 1:
            extra = sorted(ds, key=lambda d: d.at)[1:]
            issues += [Issue("duplicate_debit", d.ref, {"date": day, "amount": d.amount}) for d in extra]

    sla = timedelta(hours=pricing.refund_sla_hours)
    for d in lg.debits:
        if d.status == "pending" and now - d.at > sla:
            issues.append(Issue("stuck_debit", d.ref, {"date": d.at.date(), "amount": d.amount,
                                                      "hours": int((now - d.at).total_seconds() // 3600)}))
    if lg.mandate:
        source, amount = lg.mandate
        for d in lg.debits:
            if d.source == source and d.amount != amount and d.status != "refunded":
                issues.append(Issue("amount_mismatch", d.ref, {"date": d.at.date(), "amount": d.amount,
                                                               "mandate": amount}))
    seen, out = set(), []
    for i in issues:  # a duplicate that is also pending is reported once, as the duplicate
        if i.ref not in seen:
            seen.add(i.ref)
            out.append(i)
    return out


def withdrawal_issues(lg: Ledger, pricing: Pricing, now: datetime) -> list[Issue]:
    sla = timedelta(hours=pricing.withdrawal_sla_hours)
    return [Issue("late_withdrawal", w.ref, {"date": w.at.date(), "amount": w.amount,
                                             "hours": int((now - w.at).total_seconds() // 3600)})
            for w in lg.withdrawals if w.status == "processing" and now - w.at > sla]


def open_withdrawals(lg: Ledger) -> list[Withdrawal]:
    return [w for w in lg.withdrawals if w.status == "processing"]
