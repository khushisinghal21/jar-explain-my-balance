"""Answer "why did my balance change?" in the user's language, with numbers that can't be made up.

    question ─► language + intent ─► engine (ledger.py, checks.py) ─► facts
                                                                       │
                     template renderer (always available) ◄────────────┤
                     LLM renderer (optional) ──► verify_numbers ──► pass? use it : fall back to template
                                                                       │
                             real problem found (checks.py)? ──► ticket for a human, with the breakdown

The LLM never computes anything. It may only rephrase facts, and every ₹ amount, gram figure and
percentage in its reply must match a fact, or the reply is thrown away.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from .checks import Issue, debit_issues, open_withdrawals, recent_debits, withdrawal_issues
from .ledger import Breakdown, Ledger, Pricing, explain, money

# ── formatting ─────────────────────────────────────────────────────────────────


def inr(x: Decimal) -> str:
    """₹1,23,456.78 (Indian digit grouping)."""
    x = money(x)
    sign = "-" if x < 0 else ""
    whole, frac = f"{abs(x):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        whole = f"{head},{tail}"
    return f"{sign}₹{whole}.{frac}"


def grams(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.0001'))} g"


MONTHS_HI = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]


def day(d, lang: str) -> str:
    return f"{d.day} {MONTHS_HI[d.month - 1]}" if lang == "hi" else d.strftime("%-d %b")


# ── language and intent ────────────────────────────────────────────────────────

_HINGLISH = re.compile(r"\b(kyu|kyun|kyon|mera|meri|mere|kam|hua|ho gaya|gaya|paisa|paise|kata|kat|nahi|"
                       r"nikala|nikal|daale|dale|jama|bech|hafte|kal|aaj|mahine|balance kam|kitna)\b", re.I)


def detect_lang(text: str) -> str:
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"
    return "hinglish" if _HINGLISH.search(text) else "en"


INTENTS = [  # checked in order; first match wins
    ("advice", r"should i (sell|buy|invest)|bech (du|doon|dena)|bechna chahiye|kharid(u|oon|na chahiye)|"
               r"बेच(ूँ|ना चाहिए| दूँ)|खरीद(ूँ|ना चाहिए)|good time to"),
    ("withdrawal", r"withdr[ae]w|payout|nikal|निकाल|(not|n'?t) (been )?received|nahi mila|नहीं मिले|"
                   r"bank (me|mein) nahi aaya"),
    ("deduction", r"deduct|debit|kat (gaya|gaye|raha)|kaat|kata|कट|twice|double|do baar|दो बार|extra charge|"
                  r"autopay|auto pay"),
    ("invested_vs_value", r"(put in|invested|saved|deposit|daale|dale|jama|डाले|जमा|बचाए).*(less|kam|कम|only|sirf)|"
                          r"(less|kam|कम) .*(than|se) .*(put in|invested|saved|daale|jama|डाले|जमा)|"
                          r"less than (i|what i)|got back less|kam mila|कम मिला"),
    ("balance_drop", r"drop|fell|fall|less|lower|lost|loss|down|decreas|reduc|kam|gir|ghat|कम|गिर|घट"),
]
PERIODS = [(1, r"yesterday|today|kal|aaj|कल|आज"), (30, r"month|mahine|महीने"), (7, r"week|hafte|हफ्ते|हफ़्ते")]


def detect_intent(text: str) -> str:
    t = text.lower()
    for name, pat in INTENTS:
        if re.search(pat, t):
            return name
    return "unknown"


def detect_days(text: str) -> int:
    t = text.lower()
    for days, pat in PERIODS:
        if re.search(pat, t):
            return days
    return 7


# ── answer ─────────────────────────────────────────────────────────────────────


@dataclass
class Answer:
    text: str
    lang: str
    intent: str
    renderer: str
    verified: bool
    numbers_checked: int
    breakdown: list[tuple[str, str]] = field(default_factory=list)
    ticket: dict | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _facts_since_start(b: Breakdown) -> dict:
    return {"paid": b.paid, "gst": b.gst, "spread": b.spread, "price_move": b.price_move,
            "price_move_abs": abs(b.price_move), "withdrawn": b.withdrawn, "value": b.end_value,
            "grams": b.grams_end, "sell_today": b.sell_end}


def _facts_period(b: Breakdown, days: int) -> dict:
    pct = ((b.sell_end - b.sell_start) / b.sell_start * 100).quantize(Decimal("0.1"))
    return {**_facts_since_start(b), "days": days, "start_value": b.start_value, "sell_start": b.sell_start,
            "sell_pct": pct, "sell_pct_abs": abs(pct), "change": b.end_value - b.start_value,
            "change_abs": abs(b.end_value - b.start_value)}


# ── templates (the renderer that can't be wrong) ───────────────────────────────

def _since_start_text(f: dict, lang: str) -> str:
    fell = f["price_move"] < 0
    m = inr(f["price_move_abs"])
    eq = (f"{inr(f['paid'])} − {inr(f['gst'])} − {inr(f['spread'])} {'−' if fell else '+'} {m}"
          + (f" − {inr(f['withdrawn'])}" if f["withdrawn"] else "") + f" = {inr(f['value'])}")
    if lang == "hi":
        lines = [f"आपने अब तक {inr(f['paid'])} बचाए हैं, और आज आपके gold की value {inr(f['value'])} है।",
                 f"{inr(f['gst'])} खरीद पर GST में गए।",
                 f"{inr(f['spread'])} buy और sell rate का फ़र्क है: gold थोड़े ज़्यादा rate पर खरीदा जाता है और "
                 f"थोड़े कम rate पर बिकता है, इसलिए खरीदते ही उसकी value थोड़ी कम हो जाती है।",
                 f"खरीदने के बाद gold का दाम {'गिरा' if fell else 'बढ़ा'}, इससे आपकी savings {m} "
                 f"{'कम' if fell else 'ज़्यादा'} हुई।"]
        if f["withdrawn"]:
            lines.append(f"आपने {inr(f['withdrawn'])} cash withdraw भी किए हैं।")
        lines.append(f"पूरा हिसाब: {eq}")
    elif lang == "hinglish":
        lines = [f"Aapne ab tak {inr(f['paid'])} save kiye hain, aur aaj aapke gold ki value {inr(f['value'])} hai.",
                 f"{inr(f['gst'])} purchases par GST mein gaye.",
                 f"{inr(f['spread'])} buy aur sell rate ka fark hai: gold thode zyada rate par kharida jaata hai aur "
                 f"thode kam rate par bikta hai, isliye kharidte hi uski value thodi kam ho jaati hai.",
                 f"Kharidne ke baad gold ka daam {'gira' if fell else 'badha'}, isse aapki savings {m} "
                 f"{'kam' if fell else 'zyada'} hui."]
        if f["withdrawn"]:
            lines.append(f"Aapne {inr(f['withdrawn'])} cash withdraw bhi kiye hain.")
        lines.append(f"Poora hisaab: {eq}")
    else:
        lines = [f"You've saved {inr(f['paid'])} so far, and your gold is worth {inr(f['value'])} today.",
                 f"{inr(f['gst'])} went to GST on your purchases.",
                 f"{inr(f['spread'])} is the buy/sell gap: gold is bought at a slightly higher rate than it can be "
                 f"sold at, so it's worth a little less the moment you buy it.",
                 f"Gold prices {'fell' if fell else 'rose'} after you bought, which "
                 f"{'lowered' if fell else 'raised'} your savings by {m}."]
        if f["withdrawn"]:
            lines.append(f"You've also withdrawn {inr(f['withdrawn'])} in cash.")
        lines.append(f"It adds up exactly: {eq}")
    return "\n".join(lines)


def _period_text(f: dict, lang: str) -> str:
    fell = f["sell_pct"] < 0
    down = f["change"] < 0
    pm, ch = inr(f["price_move_abs"]), inr(f["change_abs"])
    if lang == "hi":
        lines = [f"पिछले {f['days']} दिन में आपके gold की value {inr(f['start_value'])} से {inr(f['value'])} हुई।",
                 f"मुख्य वजह gold का दाम है: sell rate {inr(f['sell_start'])} से {inr(f['sell_today'])} प्रति ग्राम हुआ "
                 f"({f['sell_pct_abs']}% {'गिरा' if fell else 'बढ़ा'}), इससे आपकी savings {pm} "
                 f"{'कम' if f['price_move'] < 0 else 'ज़्यादा'} हुई।"]
        if f["paid"]:
            lines.append(f"इस दौरान आपने {inr(f['paid'])} और बचाए ({inr(f['gst'])} GST, {inr(f['spread'])} "
                         f"buy/sell rate का फ़र्क)।")
        if f["withdrawn"]:
            lines.append(f"आपने {inr(f['withdrawn'])} withdraw भी किए।")
        lines.append(f"आपका gold कहीं नहीं गया: आपके पास {grams(f['grams'])} gold है, सिर्फ़ उसका दाम बदला है।")
    elif lang == "hinglish":
        lines = [f"Pichhle {f['days']} din mein aapke gold ki value {inr(f['start_value'])} se {inr(f['value'])} hui.",
                 f"Main wajah gold ka daam hai: sell rate {inr(f['sell_start'])} se {inr(f['sell_today'])} per gram hua "
                 f"({f['sell_pct_abs']}% {'gira' if fell else 'badha'}), isse aapki savings {pm} "
                 f"{'kam' if f['price_move'] < 0 else 'zyada'} hui."]
        if f["paid"]:
            lines.append(f"Is dauran aapne {inr(f['paid'])} aur save kiye ({inr(f['gst'])} GST, {inr(f['spread'])} "
                         f"buy/sell rate ka fark).")
        if f["withdrawn"]:
            lines.append(f"Aapne {inr(f['withdrawn'])} withdraw bhi kiye.")
        lines.append(f"Aapka gold kahin nahi gaya: aapke paas {grams(f['grams'])} gold hai, sirf uska daam badla hai.")
    else:
        lines = [f"In the last {f['days']} days your gold went from {inr(f['start_value'])} to {inr(f['value'])}"
                 f" ({'down' if down else 'up'} {ch}).",
                 f"The main reason is the gold price: the sell rate moved from {inr(f['sell_start'])} to "
                 f"{inr(f['sell_today'])} per gram ({'down' if fell else 'up'} {f['sell_pct_abs']}%), which "
                 f"{'lowered' if f['price_move'] < 0 else 'raised'} your savings by {pm}."]
        if f["paid"]:
            lines.append(f"You also saved {inr(f['paid'])} in this period ({inr(f['gst'])} GST, "
                         f"{inr(f['spread'])} buy/sell gap).")
        if f["withdrawn"]:
            lines.append(f"You also withdrew {inr(f['withdrawn'])}.")
        lines.append(f"Your gold hasn't gone anywhere: you still hold {grams(f['grams'])}. Only its price changed.")
    return "\n".join(lines)


STATUS = {
    "en": {"credited": "gold added", "pending": "no gold yet", "refunded": "refunded"},
    "hinglish": {"credited": "gold add hua", "pending": "gold abhi nahi aaya", "refunded": "refund hua"},
    "hi": {"credited": "gold जुड़ा", "pending": "gold अभी नहीं आया", "refunded": "refund हुआ"},
}
ISSUE_TEXT = {
    "en": {"duplicate_debit": "a second {amount} debit on {date}",
           "stuck_debit": "{amount} taken on {date} with no gold for {hours} hours",
           "amount_mismatch": "a {amount} auto-debit on {date} that doesn't match your {mandate} plan",
           "late_withdrawal": "your {amount} withdrawal from {date}, still not paid after {hours} hours"},
    "hinglish": {"duplicate_debit": "{date} ko {amount} ka doosra debit",
                 "stuck_debit": "{date} ko {amount} kata, {hours} ghante se gold nahi aaya",
                 "amount_mismatch": "{date} ko {amount} ka auto-debit, jo aapke {mandate} plan se alag hai",
                 "late_withdrawal": "{date} ka {amount} withdrawal, {hours} ghante baad bhi nahi mila"},
    "hi": {"duplicate_debit": "{date} को {amount} का दूसरा debit",
           "stuck_debit": "{date} को {amount} कटे, {hours} घंटे से gold नहीं आया",
           "amount_mismatch": "{date} को {amount} का auto-debit, जो आपके {mandate} plan से अलग है",
           "late_withdrawal": "{date} का {amount} withdrawal, {hours} घंटे बाद भी नहीं मिला"},
}
ESCALATED = {"en": "This isn't something I can explain away, so I've raised it with the team (ticket {t}): ",
             "hinglish": "Yeh explain karne wali baat nahi hai, maine team ko bhej diya hai (ticket {t}): ",
             "hi": "यह समझाने वाली बात नहीं है, मैंने team को भेज दिया है (ticket {t}): "}


def _issue_line(i: Issue, lang: str) -> str:
    d = {k: (inr(v) if isinstance(v, Decimal) else day(v, lang) if hasattr(v, "month") else v)
         for k, v in i.detail.items()}
    return ISSUE_TEXT[lang][i.kind].format(**d)


def _deduction_text(lg: Ledger, debits, issues, lang: str, ticket: str | None) -> str:
    st = STATUS[lang]
    head = {"en": "Your debits in the last 7 days:", "hinglish": "Pichhle 7 din ke debits:",
            "hi": "पिछले 7 दिन के debits:"}[lang]
    lines = [head] + [f"• {inr(d.amount)}, {day(d.at, lang)}: {st[d.status]}" for d in debits[:6]]
    if len(debits) > 6:
        lines.append({"en": f"…and {len(debits) - 6} more.", "hinglish": f"…aur {len(debits) - 6}.",
                      "hi": f"…और {len(debits) - 6}।"}[lang])
    if issues:
        lines.append(ESCALATED[lang].format(t=ticket) + "; ".join(_issue_line(i, lang) for i in issues) + ".")
    else:
        lines.append({"en": "Every debit became gold. Each one includes GST, so the gold added is worth a little "
                            "less than the amount debited.",
                      "hinglish": "Har debit ka gold add hua hai. Har debit mein GST shaamil hai, isliye add hue gold "
                                  "ki value debit se thodi kam hoti hai.",
                      "hi": "हर debit का gold जुड़ा है। हर debit में GST शामिल है, इसलिए जुड़े gold की value debit "
                            "से थोड़ी कम होती है।"}[lang])
    return "\n".join(lines)


def _withdrawal_text(open_ws, issues, lang: str, pricing: Pricing, ticket: str | None) -> str:
    if issues:
        return ESCALATED[lang].format(t=ticket) + "; ".join(_issue_line(i, lang) for i in issues) + "."
    if open_ws:
        w = open_ws[0]
        return {"en": f"Your {inr(w.amount)} withdrawal from {day(w.at, 'en')} is being processed. Payouts usually "
                      f"arrive within {pricing.withdrawal_sla_hours} hours.",
                "hinglish": f"Aapka {inr(w.amount)} ka withdrawal ({day(w.at, 'en')}) process ho raha hai. "
                            f"Usually {pricing.withdrawal_sla_hours} ghante mein paise aa jaate hain.",
                "hi": f"आपका {inr(w.amount)} का withdrawal ({day(w.at, 'hi')}) process हो रहा है। आम तौर पर "
                      f"{pricing.withdrawal_sla_hours} घंटे में पैसे आ जाते हैं।"}[lang]
    return {"en": "You have no withdrawals in progress.", "hinglish": "Aapka koi withdrawal process mein nahi hai.",
            "hi": "आपका कोई withdrawal process में नहीं है।"}[lang]


def _advice_text(f: dict, lang: str) -> str:
    return {"en": f"I can't tell you whether to buy or sell; that depends on your goals. What I can tell you: you hold "
                  f"{grams(f['grams'])}, worth {inr(f['value'])} at today's sell rate of {inr(f['sell_today'])} per gram.",
            "hinglish": f"Kharidna ya bechna aapke goals par depend karta hai, main iski salah nahi de sakti. Itna bata "
                        f"sakti hoon: aapke paas {grams(f['grams'])} gold hai, aaj ke sell rate "
                        f"{inr(f['sell_today'])} per gram par uski value {inr(f['value'])} hai.",
            "hi": f"खरीदना या बेचना आपके goals पर निर्भर करता है, मैं इसकी सलाह नहीं दे सकती। इतना बता सकती हूँ: "
                  f"आपके पास {grams(f['grams'])} gold है, आज के sell rate {inr(f['sell_today'])} प्रति ग्राम पर उसकी "
                  f"value {inr(f['value'])} है।"}[lang]


UNKNOWN = {"en": "I can explain why your balance changed, your recent debits, or a withdrawal. Try: "
                 "\"Why did my balance drop this week?\"",
           "hinglish": "Main bata sakti hoon ki balance kyun badla, recent debits, ya withdrawal ka status. Poochiye: "
                       "\"Is hafte mera balance kam kyun hua?\"",
           "hi": "मैं बता सकती हूँ कि balance क्यों बदला, हाल के debits, या withdrawal का status। पूछिए: "
                 "\"इस हफ़्ते मेरा balance कम क्यों हुआ?\""}


# ── number verification ───────────────────────────────────────────────────────

_MONEY_RE = re.compile(r"(-?)\s?₹\s?(-?[\d,]+(?:\.\d+)?)")
_GRAM_RE = re.compile(r"(\d+\.\d+)\s?(?:g\b|gm\b|grams?\b|ग्राम)")
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?%")


def allowed_numbers(facts: dict, extra: list[Decimal] = ()) -> dict[str, set[Decimal]]:
    vals = [v for v in facts.values() if isinstance(v, Decimal)] + list(extra)
    m = set()
    for v in vals:
        m |= {money(abs(v)), abs(v).quantize(Decimal("1"))}
    return {"money": m, "grams": {abs(v).quantize(Decimal("0.0001")) for v in vals},
            "pct": {abs(v).quantize(Decimal("0.1")) for v in vals}}


def verify_numbers(text: str, allowed: dict[str, set[Decimal]]) -> tuple[bool, int, list[str]]:
    """Every ₹ amount, gram figure and % in `text` must be one of the facts. Returns (ok, checked, bad)."""
    bad, n = [], 0
    for _, num in _MONEY_RE.findall(text):
        n += 1
        v = abs(Decimal(num.replace(",", "")))
        if money(v) not in allowed["money"] and v.quantize(Decimal("1")) not in allowed["money"]:
            bad.append(f"₹{num}")
    for num in _GRAM_RE.findall(text):
        n += 1
        if Decimal(num).quantize(Decimal("0.0001")) not in allowed["grams"]:
            bad.append(f"{num} g")
    for num in _PCT_RE.findall(text):
        n += 1
        if Decimal(num).quantize(Decimal("0.1")) not in allowed["pct"]:
            bad.append(f"{num}%")
    return not bad, n, bad


# ── optional LLM renderer ──────────────────────────────────────────────────────

LLM_SYSTEM = """You explain a gold-savings balance to an Indian app user, warmly and briefly (3-5 short lines).
Reply in the user's language: "hi" = Hindi in Devanagari, "hinglish" = romanised Hindi, "en" = English.
Use ONLY the numbers in FACTS, written exactly as given (₹ with paise). Do not compute, round, convert or
add any number. Never give buy/sell advice. If FACTS has an "issues" list, say the team has been told and
give the ticket id."""


class LLMRenderer:
    """OpenAI-compatible chat endpoint (OpenAI, Groq, Gemini's OpenAI endpoint, local servers)."""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model

    @classmethod
    def from_env(cls) -> "LLMRenderer | None":
        key = os.getenv("LLM_API_KEY")
        if not key:
            return None
        return cls(os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"), key, os.getenv("LLM_MODEL", "gpt-4o-mini"))

    def render(self, question: str, lang: str, facts_text: dict) -> str:
        r = httpx.post(f"{self.base_url}/chat/completions", timeout=30,
                       headers={"Authorization": f"Bearer {self.api_key}"},
                       json={"model": self.model, "temperature": 0.2, "messages": [
                           {"role": "system", "content": LLM_SYSTEM},
                           {"role": "user", "content": f"LANG: {lang}\nQUESTION: {question}\n"
                                                       f"FACTS: {json.dumps(facts_text, ensure_ascii=False)}"}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


# ── the agent ──────────────────────────────────────────────────────────────────


class Agent:
    def __init__(self, ledgers: dict[str, Ledger], pricing: Pricing, now: datetime, llm: LLMRenderer | None = None):
        self.ledgers, self.pricing, self.now, self.llm = ledgers, pricing, now, llm
        self.tickets: list[dict] = []

    def _ticket(self, lg: Ledger, issues: list[Issue], b: Breakdown) -> str:
        tid = "JX-" + uuid.uuid5(uuid.NAMESPACE_URL, lg.user_id + ",".join(i.ref for i in issues)).hex[:6].upper()
        if all(t["id"] != tid for t in self.tickets):
            self.tickets.append({"id": tid, "user": lg.user_id, "created": self.now.isoformat(),
                                 "issues": [{"kind": i.kind, "ref": i.ref,
                                             **{k: str(v) for k, v in i.detail.items()}} for i in issues],
                                 "balance_breakdown": [(k, str(v)) for k, v in b.rows()]})
        return tid

    def ask(self, user_id: str, question: str) -> Answer:
        lg = self.ledgers[user_id]
        lang, intent = detect_lang(question), detect_intent(question)
        today = self.now.date()
        whole = explain(lg, today)
        facts: dict = _facts_since_start(whole)
        rows, issues, ticket = whole.rows(), [], None

        if intent == "balance_drop":
            days = detect_days(question)
            b = explain(lg, today, today - timedelta(days=days))
            facts, rows = _facts_period(b, days), b.rows()
            text = _period_text(facts, lang)
        elif intent == "invested_vs_value":
            text = _since_start_text(facts, lang)
        elif intent == "deduction":
            issues = debit_issues(lg, self.pricing, self.now)
            ticket = self._ticket(lg, issues, whole) if issues else None
            debits = recent_debits(lg, self.now)
            facts = {**facts, **{f"debit_{i}": d.amount for i, d in enumerate(debits)},
                     **{f"issue_{i}": i_.detail.get("amount") for i, i_ in enumerate(issues)}}
            text = _deduction_text(lg, debits, issues, lang, ticket)
        elif intent == "withdrawal":
            issues = withdrawal_issues(lg, self.pricing, self.now)
            ticket = self._ticket(lg, issues, whole) if issues else None
            open_ws = open_withdrawals(lg)
            facts = {**facts, **{f"withdrawal_{i}": w.amount for i, w in enumerate(open_ws)}}
            text = _withdrawal_text(open_ws, issues, lang, self.pricing, ticket)
        elif intent == "advice":
            text = _advice_text(facts, lang)
        else:
            text = UNKNOWN[lang]

        allowed = allowed_numbers(facts, extra=[self.pricing.gst_rate * 100])
        renderer, notes = "template", []
        if self.llm and intent != "unknown":
            try:
                facts_text = {k: (inr(v) if isinstance(v, Decimal) and k not in ("grams", "sell_pct", "sell_pct_abs")
                                  else str(v)) for k, v in facts.items()}
                if issues:
                    facts_text["issues"] = [_issue_line(i, lang) for i in issues]
                    facts_text["ticket"] = ticket
                draft = self.llm.render(question, lang, facts_text)
                ok, _, bad = verify_numbers(draft, allowed)
                if ok:
                    text, renderer = draft, "llm"
                else:
                    notes.append(f"LLM reply rejected, numbers not in the facts: {', '.join(bad)}")
            except Exception as e:  # the template answer is always there to fall back on
                notes.append(f"LLM unavailable ({type(e).__name__}); used template")
        ok, n, bad = verify_numbers(text, allowed)
        if not ok:  # should be impossible for templates; surfaced loudly if it ever happens
            notes.append(f"UNVERIFIED NUMBERS: {bad}")
        tk = next((t for t in self.tickets if t["id"] == ticket), None)
        return Answer(text, lang, intent, renderer, ok, n, [(k, str(v)) for k, v in rows], tk, notes)
