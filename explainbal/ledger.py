"""The ledger and the arithmetic. No LLM anywhere in this file.

Why a balance changes comes down to four things, and they add up exactly:

    money you put in
  − GST on each purchase
  − the buy/sell gap (you buy gold at a higher rate than you can sell it at)
  ± gold price movement since you bought
  − cash you withdrew
  = what your gold is worth now

Every gram is tracked as a lot (one purchase). Withdrawals consume the oldest lots first. Each
lot's value is split into those parts, so the parts always sum to the balance to the paisa;
`Breakdown.check()` asserts it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

PAISA = Decimal("0.01")
CONFIG = Path(__file__).resolve().parent.parent / "config" / "pricing.yaml"


def money(x) -> Decimal:
    return Decimal(x).quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Pricing:
    gst_rate: Decimal
    buy_sell_spread: Decimal
    refund_sla_hours: int
    withdrawal_sla_hours: int

    @classmethod
    def load(cls, path: Path = CONFIG) -> "Pricing":
        d = yaml.safe_load(path.read_text())
        return cls(Decimal(str(d["gst_rate"])), Decimal(str(d["buy_sell_spread"])),
                   int(d["refund_sla_hours"]), int(d["withdrawal_sla_hours"]))


@dataclass(frozen=True)
class Price:
    day: date
    sell: Decimal   # ₹ per gram you get when selling
    buy: Decimal    # ₹ per gram you pay when buying, before GST


@dataclass
class Debit:
    ref: str
    at: datetime
    amount: Decimal
    source: str                     # "daily" | "weekly" | "manual"
    status: str                     # "credited" | "pending" | "refunded"
    purchase_ref: str | None = None


@dataclass
class Purchase:
    ref: str
    at: datetime
    paid: Decimal                   # total debited, GST included
    gst: Decimal
    grams: Decimal
    buy_rate: Decimal
    sell_rate: Decimal              # sell rate at the moment of purchase

    @property
    def spread(self) -> Decimal:
        """What the buy/sell gap cost on this purchase: gold value at buy rate minus at sell rate."""
        return money(self.paid - self.gst - self.grams * self.sell_rate)


@dataclass
class Withdrawal:
    ref: str
    at: datetime
    grams: Decimal
    sell_rate: Decimal
    amount: Decimal
    status: str                     # "paid" | "processing"
    paid_at: datetime | None = None


@dataclass
class Ledger:
    user_id: str
    name: str
    lang: str
    mandate: tuple[str, Decimal] | None
    prices: dict[date, Price]
    debits: list[Debit] = field(default_factory=list)
    purchases: list[Purchase] = field(default_factory=list)
    withdrawals: list[Withdrawal] = field(default_factory=list)

    def price_on(self, d: date) -> Price:
        if d in self.prices:
            return self.prices[d]
        earlier = [k for k in self.prices if k <= d]
        if not earlier:
            raise ValueError(f"no price on or before {d}")
        return self.prices[max(earlier)]


def buy(ledger: Ledger, pricing: Pricing, ref: str, at: datetime, paid: Decimal) -> Purchase:
    """How a purchase is priced: GST comes off the top, the rest buys gold at the buy rate."""
    p = ledger.price_on(at.date())
    paid = money(paid)
    gold_value = money(paid / (1 + pricing.gst_rate))
    gst = paid - gold_value
    grams = (gold_value / p.buy).quantize(Decimal("0.000001"))
    return Purchase(ref, at, paid, gst, grams, p.buy, p.sell)


# ── the breakdown ──────────────────────────────────────────────────────────────

@dataclass
class Breakdown:
    start: date | None              # None = since the first purchase
    end: date
    start_value: Decimal
    paid: Decimal
    gst: Decimal
    spread: Decimal
    price_move: Decimal
    withdrawn: Decimal
    end_value: Decimal
    grams_end: Decimal
    sell_start: Decimal | None
    sell_end: Decimal
    purchases: int
    exact_gap: Decimal = Decimal(0)   # identity error in exact, unrounded arithmetic (should be 0)
    rounding: Decimal = Decimal(0)    # paise moved into price_move so the rounded rows add up

    def rows(self) -> list[tuple[str, Decimal]]:
        rows = [("start_value", self.start_value)] if self.start else []
        return rows + [("paid", self.paid), ("gst", -self.gst), ("spread", -self.spread),
                       ("price_move", self.price_move), ("withdrawn", -self.withdrawn),
                       ("end_value", self.end_value)]

    def check(self) -> Decimal:
        """Gap between the displayed rows and the balance. Must be exactly 0.00."""
        total = self.start_value + self.paid - self.gst - self.spread + self.price_move - self.withdrawn
        return total - self.end_value


def _held(ledger: Ledger, until: datetime) -> dict[str, Decimal]:
    """Grams still held per purchase at `until`, withdrawals taken from the oldest purchase first."""
    held = {p.ref: p.grams for p in sorted(ledger.purchases, key=lambda p: p.at) if p.at <= until}
    for w in sorted(ledger.withdrawals, key=lambda w: w.at):
        if w.at > until:
            continue
        left = w.grams
        for ref in held:
            take = min(held[ref], left)
            held[ref] -= take
            left -= take
            if left <= 0:
                break
    return held


def _sales(ledger: Ledger, until: datetime) -> list[tuple[str, Decimal, Withdrawal]]:
    """Which purchase each withdrawn gram came from (FIFO): (purchase_ref, grams, withdrawal)."""
    remaining = {p.ref: p.grams for p in sorted(ledger.purchases, key=lambda p: p.at)}
    out = []
    for w in sorted(ledger.withdrawals, key=lambda w: w.at):
        if w.at > until:
            continue
        left = w.grams
        for ref in remaining:
            take = min(remaining[ref], left)
            if take > 0:
                out.append((ref, take, w))
                remaining[ref] -= take
                left -= take
            if left <= 0:
                break
    return out


def explain(ledger: Ledger, end: date, start: date | None = None) -> Breakdown:
    """Exact breakdown of the balance change from `start` (or the beginning) to the end of `end`."""
    end_t = datetime.combine(end, datetime.max.time())
    start_t = datetime.combine(start, datetime.max.time()) if start else None
    s_end = ledger.price_on(end).sell
    s_start = ledger.price_on(start).sell if start else None

    held_end = _held(ledger, end_t)
    held_start = _held(ledger, start_t) if start_t else {}
    sales = [(ref, g, w) for ref, g, w in _sales(ledger, end_t) if start_t is None or w.at > start_t]

    paid = gst = spread = move = withdrawn = Decimal(0)
    start_value = sum((g * s_start for g in held_start.values()), Decimal(0)) if start else Decimal(0)
    count = 0
    for p in ledger.purchases:
        if p.at > end_t:
            continue
        new = start_t is None or p.at > start_t
        base = p.sell_rate if new else s_start              # value each gram was carried at, at the start
        g_end = held_end.get(p.ref, Decimal(0))
        sold = [(g, w) for ref, g, w in sales if ref == p.ref]
        if new:
            paid += p.paid
            gst += p.gst
            spread += p.paid - p.gst - p.grams * p.sell_rate   # exact, unrounded
            count += 1
        move += g_end * (s_end - base) + sum((g * (w.sell_rate - base) for g, w in sold), Decimal(0))
        withdrawn += sum((g * w.sell_rate for g, w in sold), Decimal(0))

    end_exact = sum((g * s_end for g in held_end.values()), Decimal(0))
    # The identity must hold in exact arithmetic first. This is the real test of the accounting.
    exact_gap = start_value + paid - gst - spread + move - withdrawn - end_exact

    rows = [money(start_value), money(paid), money(gst), money(spread), money(withdrawn), money(end_exact)]
    sv, pd, gs, sp, wd, ev = rows
    move_shown = ev - sv - pd + gs + sp + wd          # rounded rows must add up to the paisa...
    rounding = move_shown - money(move)                # ...and this is how many paise that took
    return Breakdown(start, end, sv, pd, gs, sp, move_shown, wd, ev,
                     sum(held_end.values(), Decimal(0)), s_start, s_end, count, exact_gap, rounding)
