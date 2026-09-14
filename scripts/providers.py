#!/usr/bin/env python3
"""providers.py — Bring-Your-Own-Agent provider layer (the SaaS core).

One control plane, ANY agent. Users plug in whatever model/agent they want — Claude (full
governed agent via headless claude -p), or any OpenAI-compatible endpoint (DeepSeek, OpenAI,
Together, local Ollama, vLLM, ...) with their own base_url + API key. Every provider's work is
still governed identically by the capability manifest + sandbox + PDP + tamper-evident audit —
that uniform governance over heterogeneous third-party agents is the product.

Config (env or ~/projects/agent-os/.env.local):
    AOS_PROVIDER=claude|hermes|openai|deepseek|together|ollama|<name>
    AOS_PROVIDER_BASE_URL=...   AOS_PROVIDER_MODEL=...   AOS_PROVIDER_KEY=...

    providers.py info
Run with the agent-os venv python.
"""
import os
import sys
from pathlib import Path

import requests

from aoscfg import ENV

# Known OpenAI-compatible endpoints (users still bring their own key).
KNOWN = {
    "openai":   "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "together": "https://api.together.xyz/v1",
    "ollama":   "http://127.0.0.1:11434/v1",   # local, no key, privacy-first
    "groq":     "https://api.groq.com/openai/v1",
}


def _cfg():
    c = {}
    if ENV.exists():
        for ln in ENV.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); c[k.strip()] = v.strip()
    for k in ("AOS_PROVIDER", "AOS_PROVIDER_BASE_URL", "AOS_PROVIDER_MODEL", "AOS_PROVIDER_KEY"):
        if os.environ.get(k):
            c[k] = os.environ[k]
    return c


class ClaudeAgentProvider:
    """Full agentic provider — runs through factory.agent so governance/provider/budget/tracing apply."""
    kind = "agent"
    name = "claude"

    def run_agent(self, repo, prompt, timeout=240):
        import factory
        prev = getattr(factory._ctx, "engine", None)
        factory._ctx.engine = "claude"
        try:
            return factory.agent("builder", repo, prompt, timeout=timeout)
        finally:
            factory._ctx.engine = prev


class HermesAgentProvider:
    """Optional canary-gated Hermes specialist; factory retains all governance and fallback gates."""
    kind = "agent"
    name = "hermes"

    def run_agent(self, repo, prompt, timeout=240, role="researcher"):
        import factory
        prev = getattr(factory._ctx, "engine", None)
        factory._ctx.engine = "hermes"
        try:
            return factory.agent(role, repo, prompt, timeout=timeout, compact=True)
        finally:
            factory._ctx.engine = prev


class OpenAICompatProvider:
    """Any OpenAI-compatible chat endpoint (DeepSeek/OpenAI/Together/Ollama/vLLM/...)."""
    kind = "chat"

    def __init__(self, name, base_url, model, api_key=None):
        self.name, self.base_url, self.model, self.api_key = name, base_url.rstrip("/"), model, api_key

    def complete(self, prompt, system="You are a helpful coding agent.", timeout=120):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        r = requests.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def get_provider(cfg=None):
    cfg = cfg or _cfg()
    name = cfg.get("AOS_PROVIDER", "claude")
    if name == "claude":
        return ClaudeAgentProvider()
    if name == "hermes":
        return HermesAgentProvider()
    base = cfg.get("AOS_PROVIDER_BASE_URL") or KNOWN.get(name)
    if not base:
        raise ValueError(f"unknown provider '{name}' and no AOS_PROVIDER_BASE_URL set")
    model = cfg.get("AOS_PROVIDER_MODEL") or "default"
    return OpenAICompatProvider(name, base, model, cfg.get("AOS_PROVIDER_KEY"))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "info":
        p = get_provider()
        print(f"active provider: {p.name} (kind={p.kind})")
        print("known OpenAI-compatible providers:", ", ".join(KNOWN))
