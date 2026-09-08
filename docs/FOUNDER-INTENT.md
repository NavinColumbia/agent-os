# Founder Intent

This file captures the founder's original project intent in plain language so future agents do not
reduce the product to a generic agent workflow builder, dashboard, or coding assistant.

## Core Dream

The user should feel like the CEO of one or more real companies where the employees are AI agents.
The agents should behave like competent human workers: they take high-level direction, organize work,
communicate with the CEO when needed, coordinate with each other, hire or request more help, recover
from problems, and keep moving until the business outcome is complete.

The product is not primarily a frontend, a chat app, or a static workflow tool. The frontend is just
one surface over the real system: a governed operating system for AI companies.

## Operating Model

- The CEO gives broad goals, such as creating a new product, researching a market, launching a
  business, fixing an issue, or coordinating an external process.
- A coordinator should turn that intent into an organization, workstreams, roles, tasks, approvals,
  and completion criteria.
- Agents should map naturally to company roles: personal assistant, chief of staff, CTO, CFO,
  product manager, marketer, researcher, designer, engineer, QA, data engineer, operator, support,
  or any other role the mission requires.
- Supervisors should be able to request or create additional agents when the work demands more
  parallelism or specialized expertise, while preserving governance and auditability.
- Agents should communicate with each other like coworkers: handoffs, clarifying questions, status
  updates, disagreements, escalation, delegation, and requests for help.
- Agents should communicate with humans like employees: brief, useful, timely, and only when human
  judgment, approval, external action, or missing information is required.

## Human-In-The-Loop

Humans are part of the workforce when needed. An agent should be able to ask a human for a decision,
approval, credential, physical action, delivery confirmation, or other external input, then wait
durably until the human replies.

Waiting for a human should not lose state. The system should remember what it was doing, why it asked,
who it asked, what answer it needs, and how to resume after the answer arrives.

## Proactive Communication

Agents should not disappear silently. Long-running work should produce periodic updates. If work is
blocked, broken, slow, restarted, retried, reassigned, or fixed, the CEO should be told at the right
level of detail.

The product should feel like managing an excellent human org, not watching a background job spinner.

## Resilience

The system should expect failures: crashed processes, stuck agents, provider outages, rate limits,
bad outputs, broken builds, silent stalls, missed messages, and partial completions.

Failures should be detected, recovered from when possible, escalated when necessary, and explained to
the CEO. Work should be durable across restarts and should not rely on a single in-memory process.

## Product Bar

The goal is an end-to-end AI company operating system capable of handling complex real-world work:
research, planning, product management, software development, QA, deployment, marketing, operations,
finance, support, and external human coordination.

Every future design decision should be judged against this question:

> Would this make the user feel like a real CEO with an elite AI workforce, or merely like a user of
> another agent tool?

## Ultimate Scale Of Ambition

Agent OS should become an organization generator: a system capable of founding and operating organizations
that themselves create world-class products and companies. It must not be architecturally limited to coding
small applications or following a fixed linear workflow. A sufficiently resourced mission may require many
divisions, thousands of specialist agents, human leaders, proprietary tools and data, simulations, regulated
controls, vendors, physical-world work, and years of iteration.

Examples such as creating a top quantitative-trading organization or a globally competitive game studio set
the scope and quality ambition. They are not guaranteed outcomes and cannot safely be treated as one-click,
unsupervised tasks. The platform must instead build the accountable multi-team institution required for the
mission, surface hard prerequisites and uncertainty, respect law and human authority, measure real outcomes,
and adapt until the objective succeeds or the CEO receives an honest evidence-backed reason it cannot.
