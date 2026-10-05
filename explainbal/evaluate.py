"""Evaluate the agent on a fixed set of questions with known answers (synthetic users).

Checks: intent and language detection, escalation decisions, and that every number in every
reply is verified. Also stress-tests the accounting identity on many random periods.
"""
from __future__ import annotations

import json
import random
from datetime import timedelta
from pathlib import Path

from .agent import Agent
from .ledger import Pricing, explain
from .synthetic import NOW, TODAY, users

CASES = [  # (user, question, intent, lang, should_escalate)
    ("meena", "I saved ₹500 but my balance shows less. Why?", "invested_vs_value", "en", False),
    ("meena", "Why is my balance less than what I put in?", "invested_vs_value", "en", False),
    ("meena", "Why did my balance drop this week?", "balance_drop", "en", False),
    ("meena", "Maine 500 daale the, balance kam kyun hai?", "invested_vs_value", "hinglish", False),
    ("asha", "Is hafte mera balance kam kyun hua?", "balance_drop", "hinglish", False),
    ("asha", "Kal se balance gir gaya", "balance_drop", "hinglish", False),
    ("asha", "Kya abhi gold bech du?", "advice", "hinglish", False),
    ("asha", "Should I sell my gold now?", "advice", "en", False),
    ("asha", "इस हफ़्ते मेरा balance कम क्यों हुआ?", "balance_drop", "hi", False),
    ("rohit", "मैंने जितने पैसे डाले उससे कम क्यों मिला?", "invested_vs_value", "hi", False),
    ("rohit", "इस महीने मेरा balance कम क्यों हुआ?", "balance_drop", "hi", False),
    ("rohit", "Did my last withdrawal go through?", "withdrawal", "en", False),
    ("imran", "Kal paise do baar kat gaye!", "deduction", "hinglish", True),
    ("imran", "Mere paise kat gaye par gold nahi aaya", "deduction", "hinglish", True),
    ("imran", "Why was I debited twice?", "deduction", "en", True),
    ("imran", "मेरे पैसे दो बार कटे", "deduction", "hi", True),
    ("meena", "Why are there deductions from my account?", "deduction", "en", False),
    ("asha", "Autopay se kitna kata is hafte?", "deduction", "hinglish", False),
    ("kavya", "I withdrew 3 days ago and still haven't received the money", "withdrawal", "en", True),
    ("kavya", "Mera withdrawal kab aayega? Paise nahi mile", "withdrawal", "hinglish", True),
    ("kavya", "Why is my balance lower this week?", "balance_drop", "en", False),
    ("kavya", "मैंने पैसे निकाले थे, अभी तक नहीं मिले", "withdrawal", "hi", True),
    ("meena", "hello", "unknown", "en", False),
    ("asha", "Gold ka rate kya hai aaj?", "unknown", "hinglish", False),
]


def identity_stress(n: int = 300, seed: int = 3) -> dict:
    rng, ls, worst = random.Random(seed), users(), 0
    for _ in range(n):
        lg = rng.choice(list(ls.values()))
        end = TODAY - timedelta(days=rng.randint(0, 60))
        start = end - timedelta(days=rng.randint(1, 120)) if rng.random() < 0.7 else None
        b = explain(lg, end, start)
        assert b.check() == 0, (lg.user_id, start, end)
        worst = max(worst, abs(b.exact_gap), abs(b.rounding) * 100)  # rounding in paise
    return {"periods_checked": n, "all_rows_add_up": True, "max_exact_gap_or_rounding_paise": str(worst)}


def run() -> dict:
    agent = Agent(users(), Pricing.load(), NOW)
    rows, ok_intent, ok_lang, verified = [], 0, 0, 0
    tp = fp = fn = 0
    for uid, q, intent, lang, esc in CASES:
        a = agent.ask(uid, q)
        got_esc = a.ticket is not None
        ok_intent += a.intent == intent
        ok_lang += a.lang == lang
        verified += a.verified
        tp += got_esc and esc
        fp += got_esc and not esc
        fn += esc and not got_esc
        rows.append({"user": uid, "q": q, "intent": a.intent, "want": intent, "lang": a.lang, "want_lang": lang,
                     "escalated": got_esc, "want_escalated": esc, "verified": a.verified})
    n = len(CASES)
    return {"questions": n, "intent_accuracy": round(ok_intent / n, 3), "language_accuracy": round(ok_lang / n, 3),
            "escalation": {"precision": round(tp / (tp + fp), 3) if tp + fp else None,
                           "recall": round(tp / (tp + fn), 3) if tp + fn else None, "fp": fp, "fn": fn},
            "replies_fully_verified": f"{verified}/{n}", "identity": identity_stress(),
            "misses": [r for r in rows if r["intent"] != r["want"] or r["lang"] != r["want_lang"]
                       or r["escalated"] != r["want_escalated"]]}


if __name__ == "__main__":
    res = run()
    out = Path(__file__).resolve().parent.parent / "reports" / "evaluation.json"
    out.write_text(json.dumps(res, indent=2, ensure_ascii=False))
    print(json.dumps({k: v for k, v in res.items() if k != "misses"}, indent=2, ensure_ascii=False))
    for m in res["misses"]:
        print("MISS", m)
