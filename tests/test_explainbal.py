from datetime import timedelta
from decimal import Decimal

import pytest

from explainbal.agent import Agent, allowed_numbers, detect_intent, detect_lang, inr, verify_numbers
from explainbal.evaluate import identity_stress
from explainbal.ledger import Pricing, buy, explain
from explainbal.synthetic import NOW, TODAY, users


@pytest.fixture(scope="module")
def ls():
    return users()


@pytest.fixture
def agent(ls):
    return Agent(users(), Pricing.load(), NOW)


# ── arithmetic ────────────────────────────────────────────────────────────────

def test_inr_uses_indian_grouping():
    assert inr(Decimal("1234567.5")) == "₹12,34,567.50"
    assert inr(Decimal("466.214")) == "₹466.21"


def test_purchase_splits_gst_then_buys_at_buy_rate(ls):
    pr = Pricing.load()
    lg = ls["meena"]
    p = buy(lg, pr, "x", NOW - timedelta(days=1), Decimal(103))
    assert p.gst == Decimal("3.00") and p.paid == Decimal("103.00")
    # grams are kept to 6 decimals: at ~₹13,600/g that is under 1 paisa of rounding per purchase
    assert abs(p.grams * p.buy_rate - Decimal("100")) < Decimal("0.01")
    assert p.spread > 0, "buying at the buy rate costs the gap to the sell rate"


def test_meena_breakdown_matches_the_complaint(ls):
    b = explain(ls["meena"], TODAY)
    assert (b.paid, b.end_value) == (Decimal("500.00"), Decimal("466.21"))
    assert b.paid - b.gst - b.spread + b.price_move == b.end_value


def test_rows_add_up_exactly_on_hundreds_of_random_periods():
    res = identity_stress(n=300)
    assert res["all_rows_add_up"]
    assert Decimal(res["max_exact_gap_or_rounding_paise"]) <= 1, "at most 1 paisa of rounding is ever absorbed"


def test_withdrawals_counted_once_and_exactly(ls):
    b = explain(ls["rohit"], TODAY)
    w = ls["rohit"].withdrawals[0]
    assert b.withdrawn == w.amount
    assert b.check() == 0 and b.exact_gap == 0


# ── language and intent ───────────────────────────────────────────────────────

@pytest.mark.parametrize("q,lang", [("Why did my balance drop?", "en"), ("Mera balance kam kyun hua?", "hinglish"),
                                    ("मेरा balance कम क्यों हुआ?", "hi")])
def test_language(q, lang):
    assert detect_lang(q) == lang


@pytest.mark.parametrize("q,intent", [("Kal paise do baar kat gaye", "deduction"),
                                      ("I withdrew yesterday, not received", "withdrawal"),
                                      ("Should I sell now?", "advice"),
                                      ("Maine 500 daale the, kam kyun hai", "invested_vs_value"),
                                      ("Why is my balance lower this week?", "balance_drop")])
def test_intent(q, intent):
    assert detect_intent(q) == intent


# ── verification ──────────────────────────────────────────────────────────────

def test_verify_numbers_rejects_invented_amounts():
    allowed = allowed_numbers({"value": Decimal("466.21"), "gst": Decimal("14.55")})
    assert verify_numbers("Worth ₹466.21, GST ₹14.55.", allowed)[0]
    assert verify_numbers("Worth about ₹466.", allowed)[0]          # rounding to the rupee is fine
    ok, _, bad = verify_numbers("You lost ₹40.00 to fees.", allowed)
    assert not ok and bad == ["₹40.00"]


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    def render(self, *_):
        return self.reply


def test_llm_reply_with_invented_number_falls_back_to_template(ls):
    a = Agent(users(), Pricing.load(), NOW, FakeLLM("Your gold is worth ₹480.00 today."))
    r = a.ask("meena", "I saved 500 but my balance shows less")
    assert r.renderer == "template" and r.verified
    assert "₹480.00" not in r.text and any("rejected" in n for n in r.notes)


def test_llm_reply_with_only_real_numbers_is_used(ls):
    a = Agent(users(), Pricing.load(), NOW,
              FakeLLM("Aapne ₹500.00 save kiye; ₹14.55 GST aur ₹11.84 rate ka fark gaya. Aaj value ₹466.21 hai."))
    r = a.ask("meena", "Maine 500 daale the, kam kyun hai")
    assert r.renderer == "llm" and r.verified


def test_broken_llm_never_breaks_answers(ls):
    class Down:
        def render(self, *_):
            raise TimeoutError

    r = Agent(users(), Pricing.load(), NOW, Down()).ask("asha", "Is hafte balance kam kyun hua?")
    assert r.renderer == "template" and r.verified and r.text


# ── escalation ────────────────────────────────────────────────────────────────

def test_duplicate_and_stuck_debits_escalate(agent):
    r = agent.ask("imran", "Kal paise do baar kat gaye!")
    kinds = {i["kind"] for i in r.ticket["issues"]}
    assert kinds == {"duplicate_debit", "stuck_debit"}
    assert r.ticket["balance_breakdown"], "the human gets the breakdown with the ticket"


def test_late_withdrawal_escalates_but_normal_debits_dont(agent):
    assert agent.ask("kavya", "I withdrew 3 days ago, not received").ticket is not None
    assert agent.ask("meena", "Why are there deductions?").ticket is None


def test_no_buy_sell_advice(agent):
    r = agent.ask("asha", "Should I sell my gold now?")
    assert r.intent == "advice" and "can't tell you whether to buy or sell" in r.text


# ── API ───────────────────────────────────────────────────────────────────────

def test_api():
    from fastapi.testclient import TestClient

    from explainbal.api import app

    c = TestClient(app)
    assert {u["id"] for u in c.get("/api/users").json()} >= {"meena", "imran"}
    body = c.post("/api/ask", json={"user_id": "meena", "question": "Why is my balance less than I saved?"}).json()
    assert body["verified"] and body["breakdown"][-1] == ["end_value", "466.21"]
    assert c.post("/api/ask", json={"user_id": "nobody", "question": "hi"}).status_code == 404
    assert "Explain" in c.get("/").text or "balance" in c.get("/").text
