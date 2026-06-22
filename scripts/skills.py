#!/usr/bin/env python3
"""skills.py — the capability/skills registry. The org's catalog of what it can DO, mapped to roles
and tools, with readiness. Roles query this so an agent KNOWS its capabilities (and which need a
paid key / setup). seed() loads the catalog; for_role() lists a role's skills.

    skills.py seed       # (re)load the catalog
    skills.py for-role <role>
    skills.py status     # ready vs needs_setup/needs_key counts
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

# name, category, description, roles, tools, status
CATALOG = [
    # software / mobile / qa
    ("web-build", "engineering", "Build static/SPA web apps", ["builder", "tech-lead"], ["vite", "ts"], "ready"),
    ("ios-build-test", "mobile", "Build + simulate + screenshot iOS apps", ["builder", "qa-security"], ["xcode", "simctl", "mac_runner"], "ready"),
    ("android-build-test", "mobile", "Build APKs + emulate + screenshot Android", ["builder", "qa-security"], ["android-sdk", "adb", "emulator"], "ready"),
    ("qa-visual-a11y-e2e", "qa", "Live screenshots + axe a11y + behavioral E2E + vision critique", ["qa-security"], ["playwright", "axe", "vision"], "ready"),
    ("design-tokens", "design", "Design-as-code (DTCG tokens -> CSS)", ["design-ux"], ["style-dictionary"], "ready"),
    # media / creative
    ("ai-voiceover", "media", "Natural text-to-speech voiceover", ["voiceover-engineer", "video-producer"], ["piper"], "ready"),
    ("video-editing", "media", "Cut/concat/overlay/encode video", ["video-producer"], ["ffmpeg"], "ready"),
    ("demo-video-production", "media", "Auto-assemble narrated product/demo/ad videos", ["video-producer", "marketing-growth"], ["ffmpeg", "piper"], "ready"),
    ("image-edit-recolor", "media", "Edit/recolor/compose images, thumbnails", ["creative-artist", "design-ux"], ["imagemagick"], "ready"),
    ("transcription", "media", "Speech-to-text from audio/video", ["analyst", "qa-security"], ["faster-whisper"], "ready"),
    ("3d-modeling", "creative", "3D asset/scene creation", ["creative-artist", "game-developer"], ["blender"], "needs_setup"),
    ("anime-manga-creation", "creative", "Anime/manga art + worlds (image-gen models)", ["creative-artist"], ["image-gen-model"], "needs_key"),
    ("game-development", "creative", "2D/3D game build", ["game-developer"], ["godot"], "needs_setup"),
    ("music-sound-gen", "media", "Music / SFX generation", ["creative-artist"], ["audio-model"], "needs_key"),
    # data / ml
    ("data-ingestion", "data", "Governed live ingestion from web/APIs/SNS", ["data-engineer"], ["connectors"], "ready"),
    ("experiment-tracking", "data", "Log/compare/best ML experiments", ["data-scientist", "ml-engineer"], ["experiments"], "ready"),
    ("agent-eval", "data", "Evaluate agents/models", ["qa-security", "ml-engineer"], ["inspect-ai"], "ready"),
    ("graph-memory", "data", "Knowledge-graph + vector memory", ["data-scientist", "librarian"], ["pgvector", "age"], "ready"),
    ("recommendation-systems", "ml", "Train/serve recommenders", ["ml-engineer"], ["gpu", "ml-frameworks"], "needs_setup"),
    # professional / advisory (research+advise; real filings need human approval + maybe paid data)
    ("legal-compliance-review", "legal", "Region-specific compliance/legal review (advisory)", ["legal-compliance-regional"], ["web-research", "checklists"], "ready"),
    ("financial-analysis", "finance", "Budgets, modeling, cost analysis (advisory)", ["finance-cost-controller"], ["analysis"], "ready"),
    ("tax-advisory", "finance", "Tax guidance by jurisdiction (advisory)", ["tax-advisor"], ["web-research"], "ready"),
    ("visa-travel-finance", "personal", "Personal visa/travel/finance research (advisory)", ["visa-travel-advisor"], ["web-research"], "ready"),
    ("marketing-growth", "growth", "Campaigns, content, SEO, ads, analytics", ["marketing-growth"], ["web-research", "ffmpeg", "piper"], "ready"),
    # ops / comms
    ("notifications", "ops", "Phone notify + 2-way human bridge", ["controller"], ["ntfy"], "ready"),
    ("scheduling", "ops", "Recurring autonomous jobs", ["controller", "data-engineer"], ["scheduler"], "ready"),
    ("monitoring-alerting", "ops", "Health checks + alerts", ["incident-commander"], ["monitor"], "ready"),
    ("secrets-vault", "security", "Scoped encrypted secrets", ["security-appsec", "platform-infra"], ["vault"], "ready"),
]


def seed():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        for name, cat, desc, roles, tools, status in CATALOG:
            cur.execute("""INSERT INTO skills (name, category, description, roles, tools, status)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (name) DO UPDATE SET category=EXCLUDED.category, description=EXCLUDED.description,
                             roles=EXCLUDED.roles, tools=EXCLUDED.tools, status=EXCLUDED.status""",
                        (name, cat, desc, roles, tools, status))
        c.commit()
    return len(CATALOG)


def for_role(role):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT name, status, tools FROM skills WHERE %s = ANY(roles) ORDER BY name", (role,))
        return cur.fetchall()


def _main(a):
    if not a or a[0] == "seed":
        print(f"seeded {seed()} skills")
    elif a[0] == "for-role":
        seed()
        for name, status, tools in for_role(a[1]):
            print(f"  {name:24} [{status}]  tools={tools}")
    elif a[0] == "status":
        seed()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM skills GROUP BY status ORDER BY status")
            counts = dict(cur.fetchall())
            cur.execute("SELECT count(distinct category) FROM skills"); cats = cur.fetchone()[0]
        print(f"skills: {sum(counts.values())} across {cats} categories — {counts}")
    elif a[0] == "test":
        n = seed()
        ready = sum(1 for *_, s in [(x[0], x[5]) for x in CATALOG] if s == "ready")
        media = for_role("video-producer")
        ok = n >= 25 and any("demo-video" in m[0] for m in media)
        print(f"seeded {n} skills; video-producer has {len(media)} incl demo-video; ready≈{ready}")
        print("PASS: skills registry — roles know their capabilities ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
