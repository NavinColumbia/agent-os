#!/usr/bin/env python3
"""skills.py — the capability/skills registry. The org's catalog of what it can DO, mapped to roles,
tools, an ENGINE, and readiness. Roles query this so an agent KNOWS its capabilities without having to
invent tooling each time.

ENGINE — the heart of the model (see docs/adr/0006-claude-first-cognitive-substrate.md):
  claude  : Claude does it natively (reason/write/code/analyze/translate/plan/advise). No extra ML
            model needed — these REPLACE the old zoo of task-specific models. Ready by default;
            "ready" here = a governed role + a playbook, both of which ship in this OS.
  tool    : a deterministic local binary for things that aren't text (ffmpeg, imagemagick, blender,
            godot, playwright, piper, whisper, pandoc, qpdf, postgres). Ready when installed.
  device  : needs a physical target (Mac runner for iOS, adb/emulator for Android).
  gpu     : needs a local GPU (absent on WSL) — deferred until cloud.
  paid    : needs a paid API key / model — deferred until you provide one.

status: ready | needs_setup (free, installable on demand) | needs_key (gpu/paid, deferred).

    skills.py seed                # (re)load the catalog
    skills.py for-role <role>     # what a role can do
    skills.py status              # counts by engine + status
    skills.py find <substr>       # search
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

R = "ready"; SU = "needs_setup"; SK = "needs_key"
CL = "claude"; TO = "tool"; DV = "device"; GPU = "gpu"; PAID = "paid"

# name, category, description, roles, tools, status, engine
CATALOG = [
    # ── engineering / software ────────────────────────────────────────────────
    ("web-build", "engineering", "Build static/SPA/SSR web apps", ["frontend-engineer", "fullstack-engineer", "builder", "tech-lead"], ["vite", "ts"], R, CL),
    ("backend-api", "engineering", "Design/build REST/GraphQL/gRPC services + DB schemas", ["backend-engineer", "fullstack-engineer", "tech-lead"], ["python", "node", "postgres"], R, CL),
    ("system-design", "engineering", "Architecture, scaling, trade-off analysis, ADRs", ["software-architect", "staff-engineer", "tech-lead"], ["reasoning"], R, CL),
    ("code-review", "engineering", "Review diffs for bugs/security/style", ["reviewer", "staff-engineer", "tech-lead"], ["reasoning"], R, CL),
    ("refactor-migrate", "engineering", "Large-scale refactor + framework/version migration", ["backend-engineer", "frontend-engineer", "staff-engineer"], ["reasoning"], R, CL),
    ("debug-rca", "engineering", "Reproduce, root-cause, and fix defects", ["backend-engineer", "sdet", "incident-commander"], ["reasoning"], R, CL),
    ("ios-build-test", "mobile", "Build + simulate + screenshot iOS apps", ["mobile-engineer", "qa-security"], ["xcode", "simctl", "mac_runner"], R, DV),
    ("android-build-test", "mobile", "Build APKs + emulate + screenshot Android", ["mobile-engineer", "qa-security"], ["android-sdk", "adb", "emulator"], R, DV),
    ("embedded-firmware", "engineering", "Embedded/IoT firmware logic + protocols", ["embedded-engineer"], ["reasoning"], R, CL),
    ("smart-contracts", "engineering", "Write/audit blockchain smart contracts", ["blockchain-engineer", "security-appsec"], ["reasoning"], R, CL),
    # ── qa / reliability / security ───────────────────────────────────────────
    ("qa-visual-a11y-e2e", "qa", "Live screenshots + axe a11y + behavioral E2E + vision critique", ["qa-security", "sdet"], ["playwright", "axe", "vision"], R, TO),
    ("test-authoring", "qa", "Author unit/integration/property tests + coverage", ["sdet", "qa-security"], ["reasoning"], R, CL),
    ("load-perf-test", "qa", "Design + analyze load/perf tests", ["sdet", "devops-sre"], ["k6"], R, TO),
    ("threat-modeling", "security", "STRIDE threat models + security review", ["security-appsec", "security-redteam"], ["reasoning"], R, CL),
    ("pentest-advisory", "security", "Authorized pentest planning + findings triage (advisory)", ["security-redteam", "security-appsec"], ["reasoning"], R, CL),
    ("incident-response", "ops", "Drive incidents: triage, comms, postmortem", ["incident-commander", "devops-sre"], ["monitor"], R, CL),
    # ── infra / platform / ops ────────────────────────────────────────────────
    ("iac-provisioning", "infra", "Author IaC (Terraform/K8s/Compose) — apply gated", ["platform-infra", "devops-sre"], ["reasoning"], R, CL),
    ("ci-cd-pipelines", "infra", "Design CI/CD pipelines + release automation", ["devops-sre", "release-manager"], ["reasoning"], R, CL),
    ("observability", "ops", "Metrics/log/trace dashboards + SLOs", ["devops-sre", "incident-commander"], ["otel"], R, CL),
    ("monitoring-alerting", "ops", "Health checks + alerts", ["incident-commander", "devops-sre"], ["monitor"], R, TO),
    ("db-admin-tuning", "infra", "Schema/query tuning, backups, migrations", ["database-admin", "backend-engineer"], ["postgres"], R, CL),
    ("scheduling", "ops", "Recurring autonomous jobs", ["controller", "data-engineer"], ["scheduler"], R, TO),
    ("notifications", "ops", "Phone notify + 2-way human bridge", ["controller"], ["ntfy"], R, TO),
    ("secrets-vault", "security", "Scoped encrypted secrets", ["security-appsec", "platform-infra"], ["vault"], R, TO),
    # ── design / ux / content ─────────────────────────────────────────────────
    ("design-tokens", "design", "Design-as-code (DTCG tokens -> CSS)", ["design-ux", "design-systems"], ["style-dictionary"], R, TO),
    ("ux-design", "design", "Wireframes, IA, user flows, design critique", ["design-ux", "ux-researcher"], ["reasoning"], R, CL),
    ("ux-research", "design", "Synthesize interviews/surveys into insights", ["ux-researcher"], ["reasoning"], R, CL),
    ("image-edit-recolor", "media", "Edit/recolor/compose images, thumbnails", ["creative-artist", "graphic-designer", "design-ux"], ["imagemagick"], R, TO),
    ("brand-identity", "design", "Brand voice, naming, visual identity systems", ["brand-designer", "creative-artist", "marketing-growth"], ["reasoning"], R, CL),
    ("technical-writing", "content", "Docs, API refs, runbooks, tutorials", ["technical-writer"], ["reasoning"], R, CL),
    ("copywriting", "content", "Marketing/product/UX copy at any length", ["copywriter", "content-strategist", "marketing-growth"], ["reasoning"], R, CL),
    ("localization", "content", "Translate + localize across languages/locales", ["localization-translator", "content-strategist"], ["reasoning"], R, CL),
    # ── media / creative production ───────────────────────────────────────────
    ("ai-voiceover", "media", "Natural text-to-speech voiceover", ["voiceover-engineer", "video-producer"], ["piper"], R, TO),
    ("video-editing", "media", "Cut/concat/overlay/encode video", ["video-producer", "motion-designer"], ["ffmpeg"], R, TO),
    ("demo-video-production", "media", "Auto-assemble narrated product/demo/ad videos", ["video-producer", "marketing-growth"], ["ffmpeg", "piper"], R, TO),
    ("transcription", "media", "Speech-to-text from audio/video", ["analyst", "qa-security"], ["faster-whisper"], R, TO),
    ("document-generation", "content", "Render PDFs/slides/docs from content", ["technical-writer", "analyst"], ["pandoc"], R, TO),
    ("3d-modeling", "creative", "3D asset/scene creation + headless render", ["3d-artist", "creative-artist", "game-developer"], ["blender"], SU, TO),
    ("game-development", "creative", "2D/3D game build + headless export", ["game-developer"], ["godot"], SU, TO),
    ("image-generation", "creative", "Generate images/anime/manga (diffusion)", ["creative-artist", "graphic-designer"], ["image-gen-model"], SK, GPU),
    ("music-sound-gen", "media", "Music / SFX generation", ["creative-artist", "motion-designer"], ["audio-model"], SK, GPU),
    # ── product / program management ──────────────────────────────────────────
    ("product-strategy", "product", "Vision, roadmap, prioritization, PRDs", ["product-manager", "group-product-manager"], ["reasoning"], R, CL),
    ("user-stories-specs", "product", "Specs, acceptance criteria, story breakdown", ["product-manager", "business-analyst"], ["reasoning"], R, CL),
    ("project-management", "product", "Plan/track/coordinate delivery", ["project-manager", "scrum-master"], ["reasoning"], R, CL),
    ("market-competitive-research", "product", "Market sizing + competitive teardown", ["business-analyst", "research-growth", "product-manager"], ["web-research"], R, CL),
    # ── data / analytics / ai ─────────────────────────────────────────────────
    ("data-ingestion", "data", "Governed live ingestion from web/APIs/SNS", ["data-engineer"], ["connectors"], R, TO),
    ("data-pipelines", "data", "ELT/ETL modeling + transformations", ["data-engineer", "analytics-engineer"], ["reasoning"], R, CL),
    ("data-analysis", "data", "Explore/clean/analyze data, find signal", ["data-analyst", "data-scientist", "analyst"], ["python", "pandas"], R, CL),
    ("bi-dashboards", "data", "Metrics models + BI dashboards + narratives", ["bi-analyst", "analytics-engineer"], ["sql"], R, CL),
    ("experiment-tracking", "data", "Log/compare/best ML experiments", ["data-scientist", "ml-engineer"], ["experiments"], R, TO),
    ("agent-eval", "data", "Evaluate agents/models", ["qa-security", "ml-engineer"], ["inspect-ai"], R, TO),
    ("graph-memory", "data", "Knowledge-graph + vector memory", ["data-scientist", "librarian"], ["pgvector", "age"], R, TO),
    ("prompt-engineering", "ai", "Design/optimize prompts + agent workflows", ["ai-prompt-engineer", "ml-engineer"], ["reasoning"], R, CL),
    ("classic-ml-modeling", "ml", "Train classic ML (sklearn/xgboost) on CPU", ["ml-engineer", "data-scientist"], ["sklearn"], R, TO),
    ("deep-learning-train", "ml", "Train/fine-tune deep nets + recommenders", ["ml-engineer", "mlops-engineer"], ["gpu", "ml-frameworks"], SK, GPU),
    ("model-serving", "ml", "Package + serve models behind an API", ["mlops-engineer", "ml-engineer"], ["reasoning"], R, CL),
    # ── growth / marketing / sales / success ──────────────────────────────────
    ("marketing-growth", "growth", "Campaigns, content, SEO, ads, analytics", ["marketing-growth"], ["web-research", "ffmpeg", "piper"], R, CL),
    ("launch-kit-generation", "growth", "Auto-generate a product's go-to-market kit (landing page + Show HN/Product Hunt/tweet/SEO copy); publish stays human-gated", ["marketing-growth", "controller"], ["launch_kit", "factory"], R, CL),
    ("portfolio-analytics", "growth", "CEO portfolio view: per-product shipped/cost/revenue + platform MRR", ["finance-cost-controller", "controller"], ["portfolio", "billing"], R, TO),
    ("app-circuit-breaker", "finance", "Per-app spend cap + loss limit → auto-pause bleeding apps (pause page); resume needs approval", ["finance-cost-controller", "controller"], ["appguard"], R, TO),
    ("founder-digest", "growth", "Weekly digest to phone: portfolio state + prioritized next steps", ["controller", "marketing-growth"], ["digest", "ntfy"], R, TO),
    ("market-competitor-intel", "growth", "Competitor + feature-gap analysis + product recommendations (advisory)", ["research-growth", "marketing-growth"], ["intel", "web-research"], R, CL),
    ("seo-content", "growth", "SEO strategy, keyword + content optimization", ["seo-specialist", "content-marketer"], ["web-research"], R, CL),
    ("social-media", "growth", "Social content calendars + post drafting (publish gated)", ["social-media-manager", "community-manager"], ["reasoning"], R, CL),
    ("paid-ads", "growth", "Plan/draft paid campaigns (spend gated)", ["performance-ads", "marketing-growth"], ["reasoning"], R, CL),
    ("lifecycle-email", "growth", "Email/lifecycle flows + drip copy", ["email-lifecycle", "marketing-growth"], ["reasoning"], R, CL),
    ("pr-comms", "growth", "Press releases, messaging, comms (publish gated)", ["pr-comms", "marketing-growth"], ["reasoning"], R, CL),
    ("sales-enablement", "sales", "Decks, demos, proposals, RFP responses", ["sales-engineer", "account-executive"], ["reasoning"], R, CL),
    ("customer-support", "support", "Draft support replies, KB articles, triage", ["support-agent", "customer-success"], ["reasoning"], R, CL),
    ("community-management", "growth", "Community engagement + moderation playbooks", ["community-manager"], ["reasoning"], R, CL),
    ("partnerships", "growth", "Partner/BD outreach drafting + deal analysis", ["partnerships"], ["reasoning"], R, CL),
    # ── finance / legal / people / ops (advisory; real actions gated) ─────────
    ("financial-analysis", "finance", "Budgets, modeling, cost/unit-economics (advisory)", ["finance-cost-controller", "fp-and-a-analyst"], ["analysis"], R, CL),
    ("accounting-bookkeeping", "finance", "Categorize, reconcile, statements (advisory)", ["accountant-bookkeeper"], ["analysis"], R, CL),
    ("fpa-forecasting", "finance", "Forecasts, scenario + variance analysis (advisory)", ["fp-and-a-analyst", "finance-cost-controller"], ["analysis"], R, CL),
    ("tax-advisory", "finance", "Tax guidance by jurisdiction (advisory)", ["tax-advisor"], ["web-research"], R, CL),
    ("payroll", "finance", "Payroll calc + compliance checks (advisory)", ["payroll-specialist"], ["analysis"], R, CL),
    ("procurement", "ops", "Vendor evaluation, RFQs, contracts triage (advisory)", ["procurement"], ["web-research"], R, CL),
    ("legal-compliance-review", "legal", "Region-specific compliance/legal review (advisory)", ["legal-compliance-regional", "legal-compliance-checklist"], ["web-research", "checklists"], R, CL),
    ("contract-drafting", "legal", "Draft/redline contracts + NDAs (advisory)", ["contracts-counsel", "legal-compliance-regional"], ["reasoning"], R, CL),
    ("privacy-dpo", "legal", "GDPR/CCPA/DPA + privacy impact (advisory)", ["privacy-dpo", "security-appsec"], ["reasoning"], R, CL),
    ("ip-patent-advisory", "legal", "Patent/IP strategy + prior-art research (advisory)", ["ip-patent-advisor"], ["web-research"], R, CL),
    ("hr-recruiting", "people", "JD authoring, screening rubrics, sourcing (advisory)", ["hr-recruiter", "people-ops"], ["reasoning"], R, CL),
    ("people-ops", "people", "Policies, onboarding, performance frameworks (advisory)", ["people-ops"], ["reasoning"], R, CL),
    # ── industry-specialist advisory ──────────────────────────────────────────
    ("healthcare-compliance", "industry", "HIPAA + clinical/health domain advisory", ["healthcare-compliance"], ["web-research"], R, CL),
    ("fintech-compliance", "industry", "KYC/AML + financial-services compliance advisory", ["fintech-compliance", "legal-compliance-regional"], ["web-research"], R, CL),
    ("ecommerce-merchandising", "industry", "Catalog, pricing, conversion merchandising", ["ecommerce-merchandiser", "marketing-growth"], ["web-research"], R, CL),
    ("supply-chain-logistics", "industry", "Inventory, routing, logistics optimization (advisory)", ["logistics-supply-chain"], ["analysis"], R, CL),
    ("real-estate-advisory", "industry", "Property/market analysis (advisory)", ["real-estate-advisor"], ["web-research"], R, CL),
    ("sustainability-esg", "industry", "ESG/carbon reporting + strategy (advisory)", ["energy-sustainability"], ["web-research"], R, CL),
    ("game-economy-design", "industry", "Game systems, balancing, monetization design", ["gaming-economy-designer", "game-developer"], ["reasoning"], R, CL),
    # ── personal advisory (your own life ops) ─────────────────────────────────
    ("visa-travel-finance", "personal", "Personal visa/travel/finance research (advisory)", ["visa-travel-advisor", "personal-finance-advisor"], ["web-research"], R, CL),
    ("personal-finance", "personal", "Personal budgeting/investing research (advisory)", ["personal-finance-advisor"], ["web-research"], R, CL),
    ("career-coaching", "personal", "Resume, positioning, O-1/EB-1A evidence (advisory)", ["career-coach"], ["web-research"], R, CL),
    ("executive-assistant", "personal", "Scheduling, inbox triage, drafting, planning", ["executive-assistant"], ["reasoning"], R, CL),
    # ── governance / meta ─────────────────────────────────────────────────────
    ("orchestration", "governance", "Run products through SPEC→BUILD→QA→REVIEW→LAUNCH", ["controller"], ["controller"], R, TO),
    ("autonomous-app-factory", "governance", "Real role-agents build+test products end-to-end: sandboxed QA, test-driven re-flow, LAUNCH gated on green", ["controller", "tech-lead"], ["factory", "claude", "srt", "pytest"], R, TO),
    ("concurrent-app-fleet", "governance", "Build many products at once through the governed line (the app factory at scale)", ["controller"], ["factory", "claude"], R, TO),
    ("fleet-observability", "ops", "Live mission-control dashboard + watchdog paging on stalls/outages/SLA", ["controller", "incident-commander"], ["dashboard", "watchdog", "fleet", "ntfy"], R, TO),
    ("debug-tracing", "ops", "Persist + replay full per-run traces (every agent prompt/response + test output) for debugging", ["incident-commander", "qa-security", "controller"], ["trace", "dashboard"], R, TO),
    ("autonomous-self-healing", "ops", "Watchdog detects + responder auto-fixes safe incidents (restart/GC/snapshot); escalates judgement calls", ["incident-commander", "controller"], ["responder", "watchdog"], R, TO),
    ("chaos-resilience", "ops", "Fault-injection: kill daemons mid-run, assert autonomous recovery", ["incident-commander", "qa-security"], ["chaos", "watchdog"], R, TO),
    ("factory-benchmark", "qa", "Measure factory build success (pass@1/fixloop/latency) across std + hard (stateful API) tiers", ["qa-security", "controller"], ["eval_factory"], R, TO),
    ("backend-service-factory", "engineering", "Build+QA multi-module HTTP API services w/ SQLite persistence end-to-end", ["backend-engineer", "controller"], ["factory", "pytest"], R, TO),
    ("incident-rca", "ops", "Reasoning incident-commander diagnoses NOVEL failures + writes an RCA (escalate-with-analysis)", ["incident-commander"], ["incident", "claude"], R, CL),
    ("web-app-factory", "engineering", "Build + QA static web apps end-to-end through the governed line (real browser smoke test)", ["frontend-engineer", "builder", "controller"], ["factory", "playwright"], R, TO),
    ("saas-billing", "finance", "Tenant onboarding, plans, metered usage (derived), quotas, invoices, MRR", ["finance-cost-controller", "controller"], ["billing", "tenancy"], R, TO),
    ("resource-allocation", "governance", "Hire/route roles, manage the registry", ["resource-allocator"], ["allocator"], R, TO),
    ("ethics-safety-review", "governance", "Ethics/safety gate on plans + outputs", ["ethics-safety-reviewer", "audit-governance"], ["reasoning"], R, CL),
    ("web-research", "research", "Live web search + read + synthesize sources", ["research-growth", "analyst"], ["websearch", "webfetch"], R, CL),
    ("agent-directory", "governance", "Live presence/work registry: discover peers, direct brokered contact (no sockets), conflict detection", ["controller", "resource-allocator"], ["directory", "commfabric"], R, TO),
    ("agent-orchestration", "governance", "Reuse-vs-spawn routing, per-agent priority task queues, hire requests, uncovered-role escalation", ["controller", "resource-allocator"], ["orchestrate", "directory"], R, TO),
    ("os-query-plane", "governance", "Agent-queryable OS data plane: any agent can query app complexity/real-cost/status + raise alerts to the human", ["controller", "incident-commander", "analyst"], ["osq"], R, TO),
    ("build-economics", "finance", "Real per-stage/per-app token cost + tokens + time (from claude usage), feeding portfolio P&L + circuit-breaker", ["finance-cost-controller", "controller"], ["factory", "trace", "portfolio"], R, TO),
]


def seed():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("ALTER TABLE skills ADD COLUMN IF NOT EXISTS engine TEXT NOT NULL DEFAULT 'claude'")
        for name, cat, desc, roles, tools, status, engine in CATALOG:
            cur.execute("""INSERT INTO skills (name, category, description, roles, tools, status, engine)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (name) DO UPDATE SET category=EXCLUDED.category, description=EXCLUDED.description,
                             roles=EXCLUDED.roles, tools=EXCLUDED.tools, status=EXCLUDED.status, engine=EXCLUDED.engine""",
                        (name, cat, desc, roles, tools, status, engine))
        c.commit()
    return len(CATALOG)


def for_role(role):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT name, status, engine, tools FROM skills WHERE %s = ANY(roles) ORDER BY status, name", (role,))
        return cur.fetchall()


def find(sub):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT name, status, engine FROM skills
                       WHERE name ILIKE %s OR description ILIKE %s OR category ILIKE %s ORDER BY name""",
                    (f"%{sub}%", f"%{sub}%", f"%{sub}%"))
        return cur.fetchall()


def _main(a):
    if not a or a[0] == "seed":
        print(f"seeded {seed()} skills")
    elif a[0] == "for-role":
        seed()
        rows = for_role(a[1])
        for name, status, engine, tools in rows:
            print(f"  {name:28} [{engine:6}/{status:11}]  tools={tools}")
        print(f"  ── {len(rows)} skills for role '{a[1]}'")
    elif a[0] == "find":
        seed()
        for name, status, engine in find(a[1]):
            print(f"  {name:28} [{engine}/{status}]")
    elif a[0] == "status":
        seed()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT engine, count(*) FROM skills GROUP BY engine ORDER BY engine")
            by_engine = dict(cur.fetchall())
            cur.execute("SELECT status, count(*) FROM skills GROUP BY status ORDER BY status")
            by_status = dict(cur.fetchall())
            cur.execute("SELECT count(distinct category), count(*) FROM skills"); cats, total = cur.fetchone()
        print(f"skills: {total} across {cats} categories")
        print(f"  by engine: {by_engine}")
        print(f"  by status: {by_status}")
    elif a[0] == "test":
        n = seed()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM skills WHERE engine='claude'"); claude_native = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM skills WHERE status='ready'"); ready = cur.fetchone()[0]
        media = for_role("video-producer")
        ok = n >= 80 and claude_native >= 40 and ready >= 70 and any("demo-video" in m[0] for m in media)
        print(f"seeded {n} skills; {claude_native} are Claude-native; {ready} ready now")
        print("PASS: skills registry — broad org pre-built, Claude-first ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
