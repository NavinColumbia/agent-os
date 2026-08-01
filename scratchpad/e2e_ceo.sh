#!/bin/bash
cd /home/swami/projects/agent-os
export AOS_ALLOW_MISSING_CONTROL_PLANE=1
export AOS_DISPATCH_PARK=1
export AOS_CEO_TENANT=demo
# high, patient QA (exhaustive like a real user); hierarchical multi-dev build
export AOS_BUDGET_USD=1000
export AOS_MAX_AGENTS=30
export AOS_MAX_DEPTH=3
export AOS_FLEET_WORKERS=6
export AOS_QA_PARALLEL=4
export AOS_QA_MAX_STORIES=0
export AOS_STORY_SATURATE_MAX=50
export AOS_QA_MAX_STEPS=400
export AOS_QA_STALL_STEPS=100
export AOS_QA_MAX_ROUNDS=12
export AOS_QA_DECIDE_LIGHT=1
PROMPT="A personal finance tracker web app for individuals, with several interdependent parts: (1) an accounts view (checking/savings/credit) with balances; (2) transactions with add/edit/delete, category, date, amount, notes; (3) a budgets system (monthly limit per category with progress vs actual); (4) a dashboard with spend-by-category charts, income-vs-expense, and budget alerts; (5) a data layer persisting everything to localStorage with CSV/JSON import-export. Clean modern responsive UI, keyboard-friendly, mobile+desktop. It must WORK end to end: adding transactions updates balances and budgets, charts reflect real data, and everything survives reload."
exec .venv/bin/python scripts/ceo_run.py "$PROMPT" --tenant demo --org 1
