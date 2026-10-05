"""Made-up users and gold prices for the demo. Every number is synthetic.

Each user mirrors a complaint pattern from public app reviews:
  meena   new saver: "I saved ₹500 but my balance shows less"
  asha    long-time daily saver: "why did my balance drop this week?" (gold fell)
  rohit   withdrew some gold: "I put in more than I got back"
  imran   charged twice yesterday + a debit with no gold for 4 days
  kavya   withdrawal stuck in processing for 3 days
"""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from .ledger import Debit, Ledger, Price, Pricing, Withdrawal, buy, money

TODAY = date(2026, 10, 5)
NOW = datetime.combine(TODAY, time(18, 0))
DAYS = 200


def prices(pricing: Pricing, seed: int = 11) -> dict[date, Price]:
    """A gold sell-price path ending with a ~3% fall in the last week, so 'why did it drop' is real."""
    rng = random.Random(seed)
    sell = 9800.0
    out = {}
    start = TODAY - timedelta(days=DAYS)
    for i in range(DAYS + 1):
        d = start + timedelta(days=i)
        drift = 0.0009 if i < DAYS - 7 else -0.0045
        sell *= 1 + drift + rng.gauss(0, 0.006)
        s = Decimal(str(round(sell, 2)))
        out[d] = Price(d, s, money(s * (1 + pricing.buy_sell_spread)))
    return out


def _daily(lg: Ledger, pr: Pricing, amount: int, days: int, source: str = "daily", every: int = 1) -> None:
    for i in range(days, 0, -every):
        at = datetime.combine(TODAY - timedelta(days=i), time(9, 0))
        ref = f"{lg.user_id}-d{i}"
        p = buy(lg, pr, f"{ref}-g", at, Decimal(amount))
        lg.purchases.append(p)
        lg.debits.append(Debit(ref, at, p.paid, source, "credited", p.ref))


def _withdraw(lg: Ledger, ref: str, at: datetime, grams: Decimal, status: str = "paid") -> None:
    rate = lg.price_on(at.date()).sell
    lg.withdrawals.append(Withdrawal(ref, at, grams, rate, money(grams * rate), status,
                                     at + timedelta(hours=6) if status == "paid" else None))


def users(pricing: Pricing | None = None) -> dict[str, Ledger]:
    pr = pricing or Pricing.load()
    px = prices(pr)
    out: dict[str, Ledger] = {}

    meena = Ledger("meena", "Meena", "en", ("daily", Decimal(100)), px)
    _daily(meena, pr, 100, 5)
    out["meena"] = meena

    asha = Ledger("asha", "Asha", "hinglish", ("daily", Decimal(50)), px)
    _daily(asha, pr, 50, 150)
    out["asha"] = asha

    rohit = Ledger("rohit", "Rohit", "hi", ("weekly", Decimal(500)), px)
    _daily(rohit, pr, 500, 182, "weekly", every=7)
    held = sum((p.grams for p in rohit.purchases if p.at.date() <= TODAY - timedelta(days=30)), Decimal(0))
    _withdraw(rohit, "rohit-w1", datetime.combine(TODAY - timedelta(days=30), time(12, 0)),
              (held / 2).quantize(Decimal("0.000001")))
    out["rohit"] = rohit

    imran = Ledger("imran", "Imran", "hinglish", ("daily", Decimal(30)), px)
    _daily(imran, pr, 30, 60)
    yday = datetime.combine(TODAY - timedelta(days=1), time(9, 0))
    imran.debits.append(Debit("imran-dup", yday + timedelta(minutes=2), Decimal(30), "daily", "pending"))
    imran.debits.append(Debit("imran-stuck", datetime.combine(TODAY - timedelta(days=4), time(9, 5)),
                              Decimal(200), "manual", "pending"))
    out["imran"] = imran

    kavya = Ledger("kavya", "Kavya", "en", ("weekly", Decimal(1000)), px)
    _daily(kavya, pr, 1000, 140, "weekly", every=7)
    _withdraw(kavya, "kavya-w1", datetime.combine(TODAY - timedelta(days=3), time(11, 0)),
              Decimal("0.5"), status="processing")
    out["kavya"] = kavya
    return out
