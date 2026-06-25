#!/usr/bin/env python3
"""templatesview.py — the TEMPLATES / BLUEPRINTS / starter-app gallery (Area 14).

The factory's "app store": a curated set of well-described starter blueprints a user
can build from. Each entry carries a ready-to-use *build charter* — a concrete, factory-
ready prompt describing the API, behaviours and edge-cases — so picking a template and
clicking "build" hands the factory a charter it can act on immediately. Static, no DB,
no web server: this is the data/logic module that the gallery view renders.

    templatesview.py selftest
    templatesview.py json
Run with the agent-os venv python.
"""
import json as _json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

_ENV = Path.home() / "projects" / "agent-os" / ".env.local"
_DB = next((l.split("=", 1)[1].strip() for l in _ENV.read_text().splitlines()
            if l.strip().startswith("DATABASE_URL=")), None) if _ENV.exists() else None

KINDS = {"lib", "web", "service"}

# A curated gallery of starter blueprints. The charter is the product's value: it must be
# concrete enough to hand straight to the factory and get a coherent first build.
GALLERY = [
    {
        "slug": "saas-rest-api",
        "name": "SaaS REST API",
        "kind": "service",
        "category": "Backend",
        "blurb": "A multi-tenant JSON REST API skeleton with resources, pagination and health checks.",
        "charter": (
            "Build a multi-tenant JSON REST API service exposing CRUD endpoints for a 'projects' resource "
            "scoped per tenant, with cursor-based pagination (limit/cursor query params, opaque base64 cursor) "
            "and consistent error envelopes {error:{code,message}}. Every request must carry a tenant id (header "
            "X-Tenant-Id) and rejects with 400 when missing; reads are isolated so one tenant never sees another's "
            "rows. Include GET /healthz returning {status:'ok'} and structured request logging. Handle edge cases: "
            "invalid cursor -> 400, unknown resource id -> 404, oversized page limit clamped to 100."
        ),
    },
    {
        "slug": "url-shortener",
        "name": "URL Shortener",
        "kind": "service",
        "category": "Backend",
        "blurb": "Shorten long URLs to compact codes, redirect, and count clicks.",
        "charter": (
            "Build a URL shortener service: POST /shorten {url} returns a short code (base62, 7 chars) and the full "
            "short link; GET /<code> issues a 301 redirect to the original URL and increments a click counter. "
            "Validate input URLs (must be http/https, reject javascript: and malformed URLs with 400), and return "
            "the existing code if the same URL is submitted twice (idempotent). Handle edge cases: unknown code -> 404, "
            "expired links (optional TTL) -> 410, and guard against open-redirect abuse. Expose GET /<code>/stats for "
            "click counts."
        ),
    },
    {
        "slug": "markdown-notes",
        "name": "Markdown Notes App",
        "kind": "web",
        "category": "Productivity",
        "blurb": "A web app to write, render and search markdown notes with live preview.",
        "charter": (
            "Build a markdown notes web app where a user can create, edit, delete and list notes, each with a title and "
            "markdown body, rendered to sanitized HTML with a live side-by-side preview. Provide full-text search across "
            "note titles and bodies, and autosave drafts. Sanitize rendered HTML to strip scripts and on* attributes "
            "(no XSS), support fenced code blocks and links that open safely (rel=noopener). Handle edge cases: empty "
            "title defaults to 'Untitled', deleting a note asks for confirmation, and search with no matches shows an "
            "empty state."
        ),
    },
    {
        "slug": "stripe-saas-starter",
        "name": "Stripe-Billed SaaS Starter",
        "kind": "service",
        "category": "Backend",
        "blurb": "A SaaS backend with subscription plans, checkout and Stripe webhook handling.",
        "charter": (
            "Build a SaaS billing backend integrating Stripe: define plans (free/pro/team), POST /checkout creates a "
            "Stripe Checkout session for the chosen plan and returns its URL, and a /webhooks/stripe endpoint verifies "
            "the Stripe signature and updates the local subscription state on checkout.session.completed, "
            "customer.subscription.updated and .deleted. Gate a sample /pro-feature endpoint behind an active paid "
            "subscription (402 when unpaid). Handle edge cases: invalid/forged webhook signature -> 400, idempotent "
            "webhook processing (ignore duplicate event ids), and graceful downgrade to free on cancellation."
        ),
    },
    {
        "slug": "csv-to-json-cli",
        "name": "CSV → JSON CLI",
        "kind": "lib",
        "category": "Data",
        "blurb": "A library + CLI that converts CSV files to typed JSON with header inference.",
        "charter": (
            "Build a CSV-to-JSON conversion library with a thin CLI wrapper: read a CSV (configurable delimiter, with or "
            "without a header row) and emit an array of JSON objects keyed by header, inferring numeric and boolean types "
            "while leaving ambiguous values as strings. Support reading from a file path or stdin and writing to a path or "
            "stdout, plus a --pretty flag. Handle edge cases: quoted fields containing the delimiter and newlines, ragged "
            "rows (pad/truncate to header width with a warning), empty input -> [], and a BOM at file start (strip it)."
        ),
    },
    {
        "slug": "invoice-generator",
        "name": "Invoice Generator",
        "kind": "lib",
        "category": "Data",
        "blurb": "A library that builds invoices from line items and renders totals, tax and PDF/HTML.",
        "charter": (
            "Build an invoice-generation library: given seller/buyer details and a list of line items {description, qty, "
            "unit_price}, compute per-line subtotals, an overall subtotal, configurable tax (percentage or flat), optional "
            "discount, and a grand total, then render the invoice to HTML (and optionally PDF). Use integer-cents money math "
            "to avoid floating-point rounding errors and assign a sequential invoice number. Handle edge cases: zero line "
            "items -> error, negative qty/price -> validation error, rounding of tax to the nearest cent, and "
            "multi-currency display formatting."
        ),
    },
    {
        "slug": "kanban-board",
        "name": "Kanban Board",
        "kind": "web",
        "category": "Productivity",
        "blurb": "A drag-and-drop kanban board with columns, cards and persistence.",
        "charter": (
            "Build a kanban board web app with configurable columns (e.g. Todo / Doing / Done) and cards that can be "
            "created, edited, deleted and dragged between or reordered within columns, persisting board state so a reload "
            "restores positions. Each card has a title, optional description and a colour label; columns show a live card "
            "count. Handle edge cases: dropping a card outside any column returns it to origin, deleting a column with "
            "cards asks for confirmation, optimistic UI updates that roll back on a failed save, and an empty board "
            "showing a 'create your first column' prompt."
        ),
    },
    {
        "slug": "webhook-relay",
        "name": "Webhook Relay",
        "kind": "service",
        "category": "Backend",
        "blurb": "Receive inbound webhooks and fan them out to subscribers with retries.",
        "charter": (
            "Build a webhook relay service: POST /ingest/<source> accepts an inbound webhook, stores it, and asynchronously "
            "delivers it to all registered subscriber URLs for that source via signed POSTs (HMAC-SHA256 over the body in an "
            "X-Signature header). Deliveries retry with exponential backoff on non-2xx responses up to N attempts, then move "
            "to a dead-letter list visible via GET /deadletter. Provide endpoints to register/unregister subscriber URLs. "
            "Handle edge cases: duplicate inbound events deduped by id, per-subscriber delivery timeout, and replay "
            "protection so a redelivered event keeps the same signature."
        ),
    },
    {
        "slug": "password-strength-meter",
        "name": "Password Strength Meter",
        "kind": "lib",
        "category": "Security",
        "blurb": "A library that scores password strength and returns actionable feedback.",
        "charter": (
            "Build a password-strength library: given a candidate password, return a score 0-4 and structured feedback "
            "(warnings + suggestions) based on length, character-class diversity, detection of common passwords and obvious "
            "patterns (sequences, repeats, keyboard walks, dates). Expose a single pure function with no I/O and an optional "
            "minimum-policy checker. Handle edge cases: empty string -> score 0 with a clear message, very long passphrases "
            "scored on entropy not just rules, and unicode input counted by code points. Do not log or transmit the input "
            "password anywhere."
        ),
    },
    {
        "slug": "landing-page",
        "name": "Marketing Landing Page",
        "kind": "web",
        "category": "Marketing",
        "blurb": "A responsive marketing landing page with hero, features and a capture form.",
        "charter": (
            "Build a responsive marketing landing page with a hero section (headline, subhead, primary CTA), a features grid, "
            "a testimonials strip, a pricing teaser and an email capture form that POSTs to a /subscribe endpoint and shows "
            "inline success/error states. The page must be mobile-first, accessible (semantic landmarks, labelled form "
            "fields, sufficient contrast) and fast (no render-blocking heavy assets). Handle edge cases: invalid email -> "
            "inline validation before submit, duplicate signup -> friendly message, and a fallback when JavaScript is "
            "disabled so the form still posts."
        ),
    },
    {
        "slug": "rest-crud-auth",
        "name": "REST CRUD + Auth",
        "kind": "service",
        "category": "Backend",
        "blurb": "A CRUD API with user signup, login, JWT auth and per-user ownership.",
        "charter": (
            "Build a REST CRUD service with authentication: POST /signup and POST /login (passwords hashed with bcrypt/argon2, "
            "never stored plaintext) issue a short-lived JWT access token; protected CRUD endpoints for an 'items' resource "
            "require a valid Bearer token and scope rows to the authenticated owner. Include token expiry and a refresh "
            "mechanism. Handle edge cases: duplicate email signup -> 409, wrong credentials -> 401 (same message for unknown "
            "user vs bad password to avoid enumeration), expired/invalid token -> 401, and a user cannot read or mutate "
            "another user's items -> 403/404."
        ),
    },
    {
        "slug": "rate-limiter",
        "name": "Rate Limiter",
        "kind": "lib",
        "category": "Security",
        "blurb": "A reusable rate-limiting library with token-bucket and sliding-window strategies.",
        "charter": (
            "Build a rate-limiting library exposing a check(key) -> {allowed, remaining, reset_at} interface, with pluggable "
            "strategies: token bucket (configurable capacity + refill rate) and sliding-window counter. Support an in-memory "
            "store by default and a swappable backend interface (e.g. Redis) for distributed use. Be safe under concurrency "
            "(atomic increment/refill). Handle edge cases: first-ever request for a key, clock skew / monotonic time for "
            "windows, burst exactly at capacity boundary (inclusive), and a disabled/limitless mode that always allows."
        ),
    },
]


def gallery():
    """Return the full list of starter blueprints."""
    return GALLERY


def get(slug):
    """Return one template by slug, or {'error':'unknown'} if not found."""
    for t in GALLERY:
        if t["slug"] == slug:
            return t
    return {"error": "unknown"}


def charter_for(slug):
    """Return the factory-ready charter string for a slug, or '' if unknown."""
    t = get(slug)
    return t["charter"] if "charter" in t else ""


def categories():
    """Return the sorted unique list of categories present in the gallery."""
    return sorted({t["category"] for t in GALLERY})


def _selftest():
    g = gallery()
    ok_count = len(g) >= 10
    ok_charters = all(isinstance(t.get("charter"), str) and t["charter"].strip() for t in g)
    ok_kinds = all(t.get("kind") in KINDS for t in g)
    ok_slugs = len({t["slug"] for t in g}) == len(g)
    known = g[0]["slug"]
    ok_get = get(known).get("slug") == known
    ok_unknown = get("does-not-exist") == {"error": "unknown"}
    ok_charter_for = bool(charter_for(known)) and charter_for("does-not-exist") == ""
    ok_cats = len(categories()) >= 1 and categories() == sorted(set(categories()))

    ok = all([ok_count, ok_charters, ok_kinds, ok_slugs, ok_get, ok_unknown, ok_charter_for, ok_cats])
    print(f"entries={len(g)} (>=10:{ok_count}) charters-nonempty={ok_charters} kinds-valid={ok_kinds} "
          f"unique-slugs={ok_slugs}")
    print(f"get-known={ok_get} get-unknown={ok_unknown} charter_for={ok_charter_for} "
          f"categories={categories()}")
    try:
        audit.append(actor="templatesview", action="SelfTest", resource="gallery",
                     decision="pass" if ok else "fail", payload={"entries": len(g)})
    except Exception:
        pass
    print("PASS: templates gallery (curated blueprints + factory-ready charters) ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json":
        print(_json.dumps({"categories": categories(), "templates": gallery()}, indent=2))
    else:
        sys.exit("usage: templatesview.py selftest|json")


if __name__ == "__main__":
    _main(sys.argv[1:])
