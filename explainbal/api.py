"""HTTP API + demo page.

    uvicorn explainbal.api:app --reload      →  http://localhost:8000
Set LLM_API_KEY (+ LLM_BASE_URL, LLM_MODEL) to let an LLM phrase answers; numbers are still verified.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .agent import Agent, LLMRenderer
from .ledger import Pricing
from .synthetic import NOW, users

STATIC = Path(__file__).resolve().parent / "static"
SAMPLES = {
    "meena": ["I saved ₹500 but my balance shows less. Why?", "Why did my balance drop this week?"],
    "asha": ["Is hafte mera balance kam kyun hua?", "Kya abhi gold bech du?"],
    "rohit": ["मैंने जितने पैसे डाले उससे कम क्यों मिला?", "इस महीने मेरा balance कम क्यों हुआ?"],
    "imran": ["Kal paise do baar kat gaye!", "Mere paise kat gaye par gold nahi aaya"],
    "kavya": ["I withdrew 3 days ago and still haven't received the money", "Why is my balance lower this week?"],
}

app = FastAPI(title="Explain My Balance")
agent = Agent(users(), Pricing.load(), NOW, LLMRenderer.from_env())


class Ask(BaseModel):
    user_id: str
    question: str


@app.get("/api/users")
def list_users():
    return [{"id": u.user_id, "name": u.name, "lang": u.lang, "samples": SAMPLES.get(u.user_id, [])}
            for u in agent.ledgers.values()]


@app.post("/api/ask")
def ask(body: Ask):
    if body.user_id not in agent.ledgers:
        raise HTTPException(404, "Unknown user")
    if not body.question.strip():
        raise HTTPException(422, "Ask a question first")
    return agent.ask(body.user_id, body.question).to_dict()


@app.get("/api/tickets")
def tickets():
    return agent.tickets


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
