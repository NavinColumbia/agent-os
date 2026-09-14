# Private Investment Research and Controlled Trading System

**Status:** Research-backed product requirements document
**Owner:** Single private user
**Date:** 2026-08-25
**Product class:** Personal research, portfolio decision support, and staged trading automation
**Capital goal:** Grow an initial $20,000 toward a $200,000 stretch milestone in 3–5 years, then toward $500,000, without withdrawals
**Current authorization:** Research and design only. No brokerage connection, order placement, or application build has been authorized by this document.

## 1. Executive decision

Build a private, evidence-first investment committee—not an autonomous gambling bot.

The first useful product should:

1. Protect cash required for food, rent, health, taxes, and near-term obligations.
2. Continuously collect point-in-time market, filing, macro, corporate-event, and news data.
3. Generate cited research and explicit bull/bear theses.
4. Test strategy hypotheses without look-ahead, survivorship, or revision bias.
5. Compare every strategy with simple passive benchmarks after fees, slippage, and taxes.
6. Run signals in shadow and paper modes before any limited live experiment.
7. Require the human to approve the exact live order.
8. Learn from outcomes through governed, versioned experiments—never by silently changing live behavior.
9. Explain what it is doing, why it is slow, what decision it made, and what will happen next.

Do not begin with options, margin, short selling, high-frequency trading, or automatic live execution. Those multiply failure modes before the system has demonstrated any edge.

## 2. Economic reality and goal model

### 2.1 The target is a goal, not a service-level guarantee

$20,000 to $500,000 is a 25× increase. Approximate time to 25× under perfectly smooth annual compounding, with no withdrawals, taxes, fees, or losses:

| Annual return | Approximate time to 25× |
|---:|---:|
| 8% | 42 years |
| 10% | 34 years |
| 15% | 23 years |
| 20% | 18 years |
| 30% | 12 years |
| 50% | 8 years |
| 100% | 5 years |

Real returns are uneven. Higher targeted returns normally require higher drawdown and ruin risk. The system shall not convert a desired deadline into leverage, concentration, options exposure, or fabricated confidence.

The nearer stretch milestone is $20,000 to $200,000:

| Deadline | Multiple | Required annualized return without added capital |
|---:|---:|---:|
| 3 years | 10× | About 115.4%/year |
| 5 years | 10× | About 58.5%/year |

Those return requirements are extraordinarily aggressive. They cannot simultaneously be treated as consistent, low-risk, and deadline-certain. The goal engine shall model the probability of reaching $200,000 and the probability of drawdown/ruin under distributions and historical regimes; it shall never present a smooth CAGR path as a forecast.

The controller should seek every legitimate way to improve the probability—better research, multiple uncorrelated strategies, cost control, tax awareness, disciplined sizing, and future capital contributions—but it may not make the 3–5 year deadline a reason to bypass promotion gates. A separate non-market income track is the safest way to add capital and shorten the horizon without forcing investment risk.

The earlier idea of producing $4,000 every month from $20,000 would require a 20% monthly return merely to preserve the starting balance. That is a 240% simple annual payout rate and about 792% annualized if 20% monthly gains were compounded. It is not a credible consistency requirement.

FINRA explicitly warns that day trading can be extremely risky and says not to fund it with money required for living expenses. The private system must enforce that boundary, even when the owner is under financial pressure:

- [FINRA Rule 2270 day-trading risk disclosure](https://www.finra.org/rules-guidance/rulebooks/finra-rules/2270)
- [Investor.gov emergency savings guidance](https://www.investor.gov/introduction-investing/investing-basics/save-and-invest/save-rainy-day)

### 2.2 Two capital buckets

The system shall maintain two logically and operationally separate buckets:

| Bucket | Purpose | Trading access |
|---|---|---|
| Survival reserve | Essential expenses, known bills, emergency cash, and tax obligations | Never |
| Risk capital | Capital the owner can lose without losing food or shelter | Eligible only after promotion gates |

Before enabling live trading, onboarding must record essential monthly burn, near-term obligations, debts, income, country, tax residence, account restrictions, and an owner-defined reserve. A safe default is six months of essential expenses plus obligations due within 90 days. If the protected reserve consumes the full $20,000, the permitted live allocation is $0; research and paper trading continue.

### 2.3 What success means

The system succeeds when it improves decision quality and produces reproducible evidence. It does not succeed merely because a backtest or a single month is profitable.

Primary success measures:

- Survival reserve remains untouched.
- No unauthorized order is submitted.
- Portfolio loss remains within owner-approved limits.
- Every recommendation is traceable to time-valid evidence.
- Strategies beat relevant passive and cash benchmarks after realistic costs over sufficient out-of-sample evidence.
- A strategy that loses its edge is detected, reduced, paused, and investigated.
- The owner receives concise, proactive communication rather than having to interrogate the system.

## 3. Product goals and non-goals

### 3.1 Goals

- Create a daily personal investment-committee brief.
- Produce an on-demand company or ETF dossier with primary-source citations.
- Maintain a versioned thesis, disconfirming evidence, catalysts, valuation ranges, and invalidation conditions.
- Discover and test multiple strategy families using a common point-in-time research engine.
- Maintain an honest paper portfolio and reconcile simulated fills.
- Propose portfolio changes with risk and opportunity-cost analysis.
- Support a controlled path from research to shadow, paper, and limited live operation.
- Reuse Agent OS orchestration, memory, approvals, audit, incident, and proactive-communication capabilities.
- Improve research procedures and models from validated evidence.

### 3.2 Non-goals

- Guaranteed income, guaranteed returns, or a guaranteed date for reaching $500,000.
- Fully autonomous use of live money in the initial releases.
- High-frequency or latency-arbitrage trading.
- Competing with colocated professional market makers.
- Unlicensed redistribution of market data, analyst research, transcripts, or news.
- Providing investment advice or managing accounts for other people.
- Letting an LLM perform portfolio arithmetic, accounting, execution checks, or risk enforcement.
- Treating model confidence, sentiment, or a persuasive narrative as proof.

## 4. Operating principles

1. **Primary sources first.** Filings, official releases, and government data outrank summaries and social posts.
2. **Point-in-time truth.** The backtester may only see data available at the simulated decision time.
3. **Deterministic money path.** Accounting, features, sizing, constraints, orders, and reconciliation are deterministic code.
4. **Agents interpret; controls decide.** Agents may research and propose. A deterministic policy engine and the human authorize.
5. **No silent assumptions.** Missing data, stale data, revisions, and low coverage remain visible.
6. **Hypothesis before test.** Register the idea before measuring it to reduce backtest mining.
7. **Benchmarks are competitors.** Active complexity must outperform passive alternatives after all costs.
8. **Risk can veto.** The risk service can reduce or block exposure and can never relax a limit.
9. **Learning is governed.** A lesson becomes a candidate, then passes replay and holdout evaluation before promotion.
10. **Long work is durable.** Work checkpoints and resumes. Observation thresholds trigger diagnosis, not arbitrary cancellation.
11. **No action because of urgency alone.** Personal financial pressure cannot bypass the evidence or approval gates.

S&P's 2025 scorecard found 79% of active U.S. large-cap funds underperformed the S&P 500; its persistence scorecard also found sustained outperformance was rare. The system must assume that simple benchmarks are difficult to beat:

- [SPIVA U.S. Year-End 2025](https://www.spglobal.com/spdji/en/spiva/article/spiva-us/)
- [U.S. Persistence Scorecard Year-End 2025](https://www.spglobal.com/spdji/en/spiva/article/us-persistence-scorecard/)

## 5. Target user and core jobs

There is one user: the owner of the account and the system.

The owner needs to:

- Understand the current portfolio, exposures, risks, and likely upcoming catalysts.
- Ask, “What deserves research today?” and get a prioritized, cited answer.
- Ask for a company dossier and see facts, interpretations, uncertainties, and opposing cases separated.
- Compare buying, holding, trimming, selling, or doing nothing.
- See why an alert matters and how much of the apparent move has already occurred.
- Inspect every decision, data call, model call, calculation, and state transition.
- Know when a run is making progress, stalled, waiting on a provider, or degraded.
- Approve or reject exact paper/live proposals.
- Review whether an apparent edge remains real after costs and new evidence.

## 6. Scope by release

### Release 0 — Private research cockpit

- No broker credentials.
- Daily and on-demand research.
- Watchlists, thesis ledger, primary-source filing and macro ingestion.
- EOD/delayed prices, fundamentals, corporate actions, and basic news.
- Deterministic portfolio and goal simulator.
- Strategy registry and notebook/research environment.

### Release 1 — Honest strategy lab

- Point-in-time event-driven backtesting.
- Dynamic universe including inactive and delisted securities where data permits.
- Walk-forward and untouched holdout evaluation.
- Cost, spread, slippage, liquidity, taxes, and corporate-action modeling.
- Shadow signals and benchmark comparison.

### Release 2 — Paper investment committee

- Alpaca paper adapter first; Interactive Brokers can be added later.
- Broker-like order lifecycle, partial fills, rejects, cancellations, and reconciliation.
- Daily risk, incident, performance, and attribution briefs.
- No live-trading credential available to the runtime.

### Release 3 — Limited live, human approved

- Separate live brokerage credential and account/subaccount.
- Small capped allocation.
- Exact-order approval with expiration.
- Risk veto, kill switch, reconciliation, and immutable audit.
- No margin, options, or shorting.

### Release 4 — Conditional expansion

Only after long enough live evidence:

- Increased allocation.
- Selected event-driven/news strategies.
- Additional brokers or asset classes.
- Options, shorting, or leverage remain separate high-risk proposals, not automatic scope.

## 7. System context and architecture

    Private web/mobile cockpit
              |
              v
    Investment controller / CIO
       |          |           |
       v          v           v
    Research   Strategy     Portfolio and
    committee  laboratory   deterministic risk
       |          |           |
       +----------+-----------+
                  |
          Decision proposal
                  |
          Human approval gate
                  |
          Broker adapter boundary

    Shared foundations:
    data ingestion -> immutable raw lake -> normalized point-in-time store
    evidence ledger -> audit log -> memory/skill candidates
    telemetry -> health diagnosis -> incidents -> proactive communication

### 7.1 Component boundaries

| Component | Responsibility |
|---|---|
| Private cockpit | Briefs, dossiers, thesis review, strategy evidence, portfolio, approvals, and trace explorer |
| Investment controller | Decompose requests, dispatch specialists, synthesize, escalate uncertainty |
| Data plane | Acquire, timestamp, validate, normalize, version, and serve licensed data |
| Research committee | Fundamental, macro, event, market, bull, bear, and forensic analysis |
| Strategy laboratory | Feature computation, backtests, walk-forward evaluation, experiment registry |
| Portfolio engine | Holdings, cash, tax lots, exposure, optimization, attribution |
| Risk engine | Pre-trade and continuous constraints; block/reduce only |
| Execution adapter | Paper first; later submit only approved and unexpired live orders |
| Evidence and audit | Provenance for facts, decisions, model prompts, calculations, and orders |
| Learning service | Convert incidents/outcomes into staged lessons, skills, and model challengers |
| Operations service | Health, progress, rate limits, retries, costs, incidents, and recovery |

## 8. Agent organization

The system should use specialist agents where language interpretation adds value. It should not use agents to replace deterministic computations.

| Role | Output | Authority |
|---|---|---|
| CIO/controller | Prioritized questions, synthesis, trade-off memo | Can request research; cannot authorize money |
| Data steward | Coverage, lineage, staleness, anomalies, licensing status | Can quarantine data |
| Universe/screener analyst | Candidate set and deterministic screening explanation | Cannot promote a strategy |
| Fundamental analyst | Business quality, unit economics, valuation inputs | Research only |
| Forensic analyst | Accounting, dilution, governance, related-party and disclosure red flags | Research only |
| Macro/regime analyst | Rates, inflation, liquidity, growth, sector sensitivity | Research only |
| Event/news analyst | Catalyst extraction, novelty, source quality, affected entities | Research only |
| Market/technical analyst | Trend, volatility, liquidity, positioning, market response | Research only |
| Bull advocate | Strongest evidence-based positive case | Research only |
| Bear/red-team | Failure modes and disconfirming evidence | Can demand unresolved-risk disclosure |
| Quant researcher | Registered hypothesis, feature and test specification | Cannot alter live strategy |
| Evidence judge | Citation, timestamp, contradiction, and reproducibility checks | Can reject a memo |
| Risk officer | Exposure and loss constraints | Can veto; cannot loosen |
| Portfolio constructor | Deterministic candidate allocation and alternatives | Proposes only |
| Execution planner | Exact order plan and expected costs | Proposes only |
| Post-trade reviewer | Attribution and implementation shortfall | Can create learning candidates |
| Learning librarian | Deduplicate, stage, test, and version lessons/skills | Cannot write live behavior directly |
| Operations coordinator | Progress, stalls, provider health, ETA, recovery | Can retry/fail over; not trade |

### 8.1 Structured research contract

Every research output shall contain:

- Question and decision relevance.
- As-of time and data cutoff.
- Facts, each with source, source type, publication time, received time, and excerpt/hash.
- Calculations with code/version identifiers.
- Interpretation separated from facts.
- Base, bull, and bear cases.
- Confidence with a calibrated meaning, not decorative percentages.
- Missing or stale data.
- Disconfirming evidence.
- Thesis invalidation conditions.
- Catalysts and expected time horizon.
- Risks, including liquidity and gap risk.
- Recommended next research action.
- Expiration/review date.

Any uncited numeric market or company fact is invalid.

## 9. Data-source research and current cost

Prices below were checked on 2026-08-25 and may change. The implementation must keep provider entitlements and current terms in configuration rather than assuming permanent access.

### 9.1 Free and primary sources

| Source | Data | Cost/access | Use |
|---|---|---|---|
| [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | Submission history and XBRL from 10-K, 10-Q, 8-K, 20-F, 6-K and related filings | Free; no key for public data APIs; observe SEC fair-access policy | Source-of-truth filings, facts, insider and holdings events |
| [FRED and ALFRED](https://fred.stlouisfed.org/docs/api/fred/) | Macro series and historical vintages | Free account/API key; attribution and terms apply | Macro features; ALFRED prevents revision look-ahead |
| [BLS Public Data API](https://www.bls.gov/developers/api_faqs.htm) | Labor, CPI, PPI, productivity and other series | Free; v2 registration; 500 daily queries and 50 series/query | Direct economic releases |
| [U.S. Treasury rate feed](https://home.treasury.gov/treasury-daily-interest-rate-xml-feed) | Nominal and real yield curves, bills, long-term rates | Free XML feed | Rates, curve, discount and regime features |
| [EIA Open Data](https://www.eia.gov/opendata/documentation.php) | Energy supply, demand, inventories and prices | Free API key | Energy-sector and macro research |
| [CFTC Commitments of Traders](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm) | Weekly futures/options positioning categories | Public API/downloads | Slow positioning context; not a timing oracle |
| [FINRA Reg SHO volume](https://developer.finra.org/docs/api-explorer/query_api-equity-reg_sho_daily_short_sale_volume) | FINRA-reported daily short-sale volume | Public API | Context only; not total short interest or a direct bearish-position measure |
| [ClinicalTrials.gov API](https://clinicaltrials.gov/data-about-studies/learn-about-api) | Trial status and results; generally weekday daily refresh | Free public API | Optional biotech event research |
| [openFDA](https://open.fda.gov/apis/authentication/) | Drug/device events, labels, enforcement and other FDA datasets | Free key; 120,000 requests/day with key | Optional biotech/regulatory research |
| Company investor-relations sites | Earnings releases, presentations, event notices | Public, source-specific terms | Primary corporate communications |

SEC ingestion must obey its published fair-access limits and identify the client. The collector shall remain below 10 requests/second across the deployment, cache responses, and prefer bulk files for backfills.

### 9.2 Market, fundamental, and news vendors

| Provider | Current individual offering | Strength | Important limitation |
|---|---|---|---|
| [Alpaca Market Data](https://docs.alpaca.markets/us/docs/about-market-data-api) | Basic $0: IEX real time, 30 websocket symbols, history since 2016, 200 calls/min. Algo Trader Plus $99/month: all U.S. exchanges, unrestricted recent history, 10,000 calls/min, broader streaming and OPRA options feed. | Simplest paper/execution pairing | Basic IEX is not the full consolidated market |
| [Massive Stocks](https://massive.com/pricing?product=stocks) | Basic $0; Starter $29/month; Developer $79/month; Advanced $199/month. History rises from 2 to 20+ years; Advanced adds real-time quotes, financials, and ratios. | Clean REST/websocket/flat-file stack, corporate actions, inactive tickers | Individual-use license; plan history and entitlements differ |
| [Tiingo](https://www.tiingo.com/about/pricing) | Starter $0; Power $30/month or $300/year. Paid plan lists 30+ years of prices, 10,000 requests/hour, 100,000/day, 40 GB/month; news has three months queryable plus future data. | Low-cost long daily history and news | Personal/internal use only; fundamental API is a separate add-on |
| [Intrinio](https://intrinio.com/pricing) | Individual $150/month | Normalized U.S. fundamentals, EOD/history, options, and real-time derived feeds | Personal only; premium datasets may still require higher tiers |
| [Alpha Vantage](https://www.alphavantage.co/premium/) | Free standard access is 25 calls/day; premium prices are selected in its interactive purchase form | Broad prototype endpoints | Free quota is too small for serious broad ingestion; real-time entitlements separate |
| [Massive + Benzinga News](https://massive.com/docs/rest/partners/overview) | Benzinga real-time news expansion $99/month | Structured, timestamped real-time financial news with full-text fields where entitled | Separate licensed expansion; do not assume redistribution or training rights |
| [GDELT DOC 2.0](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) | Free global multilingual news search | Global topic/geopolitical context | Not finance-grade low-latency execution data; rolling/search constraints |

Do not scrape paywalled analyst research, transcripts, social platforms, or copyrighted news in violation of their terms. For unlicensed news, store the URL, timestamps, normalized entities, derived facts, and content hashes where allowed—not a shadow archive of full articles.

### 9.3 Brokerage choices

**Recommended first adapter: Alpaca paper.**

- Free paper environment and straightforward API.
- Basic data is sufficient for early paper workflows.
- U.S.-listed securities are generally commission-free in self-directed API accounts, but regulatory and other fees can apply.
- Paper results omit or simplify market impact, queue position, latency slippage, some fees, and other live behavior. Alpaca explicitly warns that paper is not a substitute for live experience:
  - [Alpaca paper trading](https://docs.alpaca.markets/us/docs/paper-trading)
  - [Alpaca commissions and clearing fees](https://alpaca.markets/support/commission-clearing-fees)

**Later adapter: Interactive Brokers.**

- Broader asset and international-market coverage.
- Requires a fully opened/funded live account for full paper/API operation.
- Market-data subscriptions are tied to usernames and entitlements.
- Web API has pacing limits and one active brokerage session per username.
- Paper execution behavior can differ from live:
  - [IBKR Trading Web API](https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-trading/)
  - [IBKR paper-trading limitations](https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/limitations/paper-trading)

### 9.4 Recommended cost tiers

These are planning estimates, not vendor quotes for future dates.

| Tier | Stack | Data cost/month | When to use |
|---|---|---:|---|
| Lean research | Tiingo Power + SEC/FRED/BLS/Treasury + Alpaca Basic paper | About $30 | Daily/EOD research and initial hypotheses |
| Recommended research | Tiingo Power + Massive Developer + official sources + Alpaca Basic | About $109 | Longer history, second-source validation, better market/reference coverage |
| Real-time research | Massive Advanced + Benzinga News + official sources + Alpaca Basic | About $298 | Only after a delayed-data event strategy passes promotion gates |
| Alternative normalized | Intrinio Individual + official sources + Alpaca Basic | About $150 | If normalization saves more engineering time than broader vendor coverage |

Additional planning ranges:

- Private local infrastructure: near $0 incremental if existing hardware and backups suffice.
- Small private cloud deployment: roughly $20–$100/month for compute, database, object storage, backups, and monitoring.
- Model usage: roughly $50–$500+/month depending on universe and document volume. This must be metered per workflow.
- Brokerage, exchange, regulatory, transfer, borrowing, and tax costs are separate.

Agents must not read every document with the largest model. Use deterministic ingestion, change detection, relevance ranking, and smaller extraction passes; reserve stronger models for a small, decision-relevant candidate set.

## 10. Data architecture

### 10.1 Recommended storage

- **Immutable raw store:** compressed JSON/XML/HTML/PDF where licensed, plus Parquet for bulk market data. Every object gets a checksum, source, entitlement, ingestion run, schema version, and timestamps.
- **PostgreSQL:** entities, security master, documents, facts, theses, experiments, decisions, approvals, orders, positions, tax lots, agent state, incidents, and audit relationships.
- **Partitioned time-series tables or TimescaleDB:** bars, quotes, features, signals, and portfolio snapshots.
- **DuckDB over Parquet:** fast local factor research and backtests without duplicating every dataset into serving tables.
- **Vector retrieval:** filings, releases, research notes, and prior theses only. It is an index, never the source of truth.
- **Object backup:** encrypted off-host snapshots with tested restore.

### 10.2 Required time semantics

Every observation/document shall preserve, where applicable:

- Event time: when the underlying event occurred.
- Period start/end: what reporting period the value describes.
- Published time: when the source released it.
- Provider time: vendor timestamp.
- Received time: when this system first obtained it.
- Effective time: earliest time a strategy is allowed to use it.
- Revision time and superseded version.
- Exchange and source timezone plus normalized UTC.

The backtester reads by effective time, never by the latest version known today.

### 10.3 Security master requirements

- Stable internal instrument identifier.
- Symbol history, exchange, CIK, FIGI where licensed, share class, currency, asset type.
- Active/inactive periods and delisting.
- Mergers, spin-offs, bankruptcy, acquisition, symbol reuse, and share-class changes.
- Point-in-time index/universe membership.
- Split, reverse split, dividend, distribution, and other corporate actions.
- Raw and adjusted price series with the adjustment version and method.

### 10.4 Core domain records

- Provider, entitlement, and license record.
- Ingestion run, request, response, retry, and anomaly.
- Instrument and corporate action.
- Price bar, quote, trade, and market calendar.
- Filing/document, document version, extracted fact, and evidence link.
- Macro series, vintage, release, and revision.
- News item, update/removal, entity mapping, novelty, and source quality.
- Feature definition, version, input lineage, and computed value.
- Strategy hypothesis, experiment, parameter set, holdout, result, and promotion state.
- Thesis, catalyst, invalidation, valuation scenario, and review.
- Portfolio, cash, holding, tax lot, exposure, and benchmark.
- Signal, recommendation, approval, order intent, broker order, fill, and reconciliation.
- Agent run, decision, message, checkpoint, cost, and incident.
- Lesson candidate, replay result, skill/model version, approval, and rollback.

## 11. Ingestion and data-quality pipeline

### 11.1 Pipeline stages

1. Schedule/poll or consume a licensed stream.
2. Record request intent, provider, endpoint, entitlement, and expected freshness.
3. Rate-limit and deduplicate.
4. Save immutable raw payload before transformation.
5. Validate transport, schema, record counts, chronology, and checksums.
6. Normalize into internal schemas.
7. Resolve entities using time-valid mappings.
8. Compare critical values with another source where available.
9. Compute staleness and quality scores.
10. Publish a data-ready event.
11. Quarantine conflicting or structurally broken data.
12. Reprocess from raw payload after parser/schema changes.

### 11.2 Required quality checks

- Missing intervals, duplicates, negative/zero prices, crossed quotes, impossible volumes.
- Out-of-order sequence and stream gaps.
- Exchange calendar, daylight-saving, halt, and extended-hours handling.
- Corporate-action discontinuities.
- Filing amendments and restatements.
- Unit/currency/XBRL taxonomy consistency.
- News edits/removals and duplicate syndication.
- Macro preliminary/revised vintages.
- Provider disagreement.
- Unexplained universe membership changes.
- Stale reference/fundamental data.
- Schema drift and silent field deletion.

Critical data failures block dependent research and orders. They do not degrade silently.

## 12. Research and strategy laboratory

### 12.1 Research lifecycle

1. Write the economic or behavioral hypothesis.
2. Identify why an edge might exist and why it might persist.
3. Define universe, decision horizon, data available at decision time, feature, signal, portfolio construction, exit, and risk.
4. Register the hypothesis and reserve an untouched holdout.
5. Implement deterministic feature and strategy code.
6. Run unit, invariance, and time-leak tests.
7. Run in-sample exploration within a declared budget.
8. Run walk-forward/out-of-sample evaluation.
9. Stress costs, lags, missing data, parameter changes, and adverse regimes.
10. Have a separate evidence judge reproduce the result.
11. Compare against passive and simpler strategy baselines.
12. Reject, revise as a new experiment, or promote to shadow.

Each trial consumes a multiple-testing budget. The system shall log every attempted parameter/configuration, including failures, to prevent cherry-picking.

Backtests must address look-ahead and survivorship bias. QuantConnect's research guide provides useful examples, and backtest-overfitting research shows that impressive simulated performance can be found after trying surprisingly few alternatives:

- [QuantConnect research guide](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/research-guide)
- [Bailey et al., Effects of Backtest Overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2308659)

### 12.2 Initial strategy families

Run several independent families in parallel, but promote only those with a defensible mechanism.

1. **Long-horizon quality/value:** profitability, balance-sheet strength, cash conversion, reinvestment, valuation, and governance.
2. **Cross-sectional momentum:** medium-term relative strength with volatility, liquidity, sector, and crash-risk controls.
3. **Trend/regime:** diversified ETF trend and risk scaling; simpler and more robust than single-stock prediction.
4. **Earnings and filing events:** surprise, guidance, revisions, 8-K/10-Q changes, and post-event drift.
5. **News/event response:** novelty and primary-source confirmation combined with actual price/volume response.
6. **Mean reversion/pairs:** only with stability, borrow, transaction-cost, and structural-break tests.
7. **Macro allocation:** rates, inflation, growth, credit, energy, and liquidity regimes using release vintages.
8. **Insider/ownership context:** Forms 3/4/5, 13D/G, and 13F as contextual evidence, not blind copy trades.
9. **Risk overlays:** volatility targeting, exposure caps, trend filters, drawdown response, and hedging research.
10. **Sector event modules:** biotech trials/FDA, energy inventories/CFTC, and other domain-specific sources.

Deferred:

- Options/volatility strategies.
- Short portfolios.
- Leveraged strategies.
- Intraday market making or HFT.
- Social-media-driven execution.

### 12.3 Backtest realism

Model:

- Survivorship and delistings.
- Point-in-time universe membership.
- Filing/release availability rather than reporting-period end.
- Corporate actions and dividends.
- Bid/ask spread and adverse selection.
- Commission, regulatory, borrow, and data costs.
- Slippage, market impact, latency, partial fills, and rejects.
- ADV participation and capacity.
- Trading halts and gap moves.
- Tax lots, short/long-term tax treatment inputs, and wash-sale flags for owner/CPA review.
- Cash yield and benchmark dividends.

Run perturbation tests: delay every signal, worsen every fill, raise costs, remove best trades, vary parameters, corrupt a small portion of data, and test crisis periods.

### 12.4 Evaluation metrics

Return alone is insufficient. Record:

- CAGR and time-weighted return.
- Volatility, downside deviation, Sharpe, Sortino, and Calmar.
- Maximum drawdown, time under water, and recovery time.
- Alpha, beta, tracking error, and information ratio against declared benchmarks.
- Hit rate, payoff ratio, profit factor, skew, and tail loss.
- Turnover, spread paid, slippage, implementation shortfall, and capacity.
- Gross/net/sector/factor exposure and concentration.
- VaR/CVaR as descriptive tools plus explicit historical stress losses.
- Performance before and after all modeled costs and estimated taxes.
- Stability across regimes, subperiods, symbols, parameters, and delayed execution.
- Deflated/probabilistic Sharpe or equivalent multiple-testing adjustment.

## 13. Promotion gates

Promotion is a state machine with immutable evidence:

    idea -> registered -> research -> reproduced -> shadow
         -> paper -> limited_live -> scaled
                    \-> paused/rejected/retired

### 13.1 Research to shadow

Required:

- Registered causal/economic hypothesis.
- No known time leak or survivorship defect.
- Independent reproduction.
- Positive out-of-sample result after stressed costs.
- Improvement over a simpler baseline that is large enough to matter.
- Acceptable drawdown and tail behavior.
- Parameter and subperiod stability.
- Data license permits intended use.

### 13.2 Shadow to paper

Required:

- Live-arriving data produces the same features as replay.
- Signals arrive before their intended decision deadline.
- No unresolved critical data gaps.
- At least 30 trading days of stable shadow operation.
- Operational recovery, stale-data blocking, and alerting pass drills.

### 13.3 Paper to limited live

Required:

- At least 60 trading days and enough independent decisions to evaluate the strategy; target 100 events/trades for higher-frequency strategies.
- Low-frequency strategies require a longer calendar window rather than pretending a few decisions prove consistency.
- Net paper results remain acceptable under worse-than-observed fills.
- Broker reconciliation has no unresolved mismatch.
- Kill switch, credential isolation, approval expiry, and incident drills pass.
- The owner still has a protected survival reserve.
- The evidence judge and risk officer both approve; either can veto.

Paper trading is an operational and behavioral test, not proof of live profitability.

### 13.4 Limited live to scaled

Required:

- Start with a small explicit allocation, not the full account.
- Minimum six months of live evidence and sufficient independent observations.
- Live implementation shortfall is within the stress assumptions.
- Drawdown, incident, and behavioral compliance remain within policy.
- Strategy continues to beat the declared alternative after costs.
- Scaling analysis shows capacity and concentration remain acceptable.
- Human approves each increase.

Claims of “consistent performance” require evidence across substantially different market regimes; a single profitable quarter cannot qualify.

## 14. Portfolio and risk requirements

### 14.1 Default initial live policy

These are conservative starting defaults to be reviewed with the owner before implementation:

- Cash account behavior; no borrowing or leverage.
- Long U.S. stocks and diversified ETFs only.
- No options and no shorts.
- First live strategy allocation capped at the lesser of 5% of risk capital or an owner-approved fixed amount.
- Estimated loss at the initial stop/invalidation level no greater than 0.25% of risk capital per position.
- Single-stock market value capped at 5% of risk capital; diversified ETF cap can be higher by policy.
- Sector exposure cap 20%.
- New single stocks require a minimum liquidity threshold and position size must be negligible relative to ADV.
- Portfolio daily-loss warning at 1%; new-order halt at 2%.
- Strategy pause at 5% drawdown from its live high watermark.
- Portfolio hard halt at 10% drawdown from its live high watermark pending human review.
- Stale, incomplete, conflicting, or unavailable critical data blocks new exposure.
- Market orders are prohibited for illiquid securities.
- New event exposure near earnings or binary events is prohibited unless the strategy was explicitly tested for it.

These are limits, not promises that losses cannot exceed them. Gaps, halts, outages, and liquidity can cause larger realized losses.

### 14.2 Approval contract

Every live proposal must show:

- Strategy and version.
- Symbol/instrument identity.
- Side.
- Quantity and notional.
- Order type and limit/stop prices.
- Time in force and approval expiration.
- Current quote timestamp and source.
- Estimated spread, slippage, fees, and tax-lot impact.
- Pre- and post-trade exposures.
- Thesis and signal evidence.
- Invalidation and exit plan.
- Worst modeled loss and relevant stress scenarios.
- Alternatives: do nothing, smaller size, different instrument, or delayed decision.

The user approval is bound to this exact payload hash. Any material change requires a new approval.

### 14.3 Kill and recovery controls

- Global manual kill switch.
- Automated new-order halt on stale data, reconciliation failure, broker instability, repeated rejects, risk breach, or audit failure.
- Cancel open orders where safe after a halt; never blindly liquidate without policy.
- Broker is source of truth for actual orders/fills/positions; internal ledger must reconcile.
- Restart is fail-closed: recover state and reconcile before accepting new orders.
- Risk service runs independently from the research controller.

## 15. News and event-driven design

News processing should be a funnel:

1. Ingest licensed headline/event and source timestamps.
2. Deduplicate syndication and updates.
3. Map entities using the time-valid security master.
4. Classify source quality and whether the item is a primary source.
5. Detect novelty relative to known filings/news.
6. Extract only verifiable event facts.
7. Measure contemporaneous price, volume, spread, and volatility response.
8. Route only significant, novel events to stronger agents.
9. Produce a cited alert with ambiguity and already-realized move.
10. Apply strategy-specific deterministic rules and risk.

Do not let an LLM infer a trade directly from sentiment. News sentiment is a feature candidate, not an authorization.

Latency tiers:

- Primary filing/official-event alert: target under two minutes from system receipt.
- Licensed real-time news triage: target under five seconds for deterministic routing and under 30 seconds for a cited interpretation.
- Interactive fresh dossier: first useful result under 60 seconds, with deeper sections streaming as completed.
- EOD brief: complete after official data validation, not at a fixed time if critical inputs are missing.

The product is not intended to win a sub-second race. If a strategy's edge disappears when delayed by tens of seconds or minutes, retail infrastructure is likely the wrong venue.

## 16. Governed self-learning

Hermes Agent's relevant design pattern is durable memory plus reusable procedural skills that can be created and revised, with optional write approval:

- [Hermes skills and skill-write approvals](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)
- [Nous Research Hermes Agent repository](https://github.com/NousResearch/hermes-agent)

Adopt the pattern, not uncontrolled self-modification.

### 16.1 Learning loop

1. Capture an episode: thesis, data, decision, action, result, costs, regime, and incidents.
2. Produce attribution: signal, sizing, execution, market, luck, and process contributions.
3. Generate a candidate lesson or procedure change.
4. Check for contradiction and duplicate lessons.
5. Replay the candidate against historical incidents and untouched data.
6. Run it as a challenger in shadow.
7. Require evidence-judge approval for research procedures and human approval for any money-path behavior.
8. Version and sign the promoted artifact.
9. Monitor post-promotion behavior.
10. Roll back automatically on regression.

### 16.2 What may learn

- Source reliability and entity-resolution rules.
- Research checklists and retrieval procedures.
- Which documents/questions are useful for specific sectors.
- Extraction prompts and model routing.
- Data anomaly detection.
- Execution-cost estimates from live/paper fills.
- Forecast calibration.
- Strategy parameters only through declared offline experiments.

### 16.3 What may not self-modify

- Survival-reserve boundary.
- Credential permissions.
- Approval requirements.
- Risk limits or kill-switch behavior.
- Audit retention.
- Live strategy code or model weights.
- Broker order payload after approval.

### 16.4 Prevent false learning

- Preserve losing and rejected experiments.
- Separate process quality from outcome luck.
- Use counterfactual and benchmark attribution.
- Weight lessons by sample size, regime coverage, and recency.
- Expire or review lessons as markets and providers change.
- Require causally plausible explanations.
- Never treat one trade or one month as a reusable edge.

## 17. Agent OS integration

Build this as a private vertical on existing Agent OS foundations, not a separate uncontrolled agent runtime.

| Existing capability | Investment-system use |
|---|---|
| Work contracts and objective portfolio | $20k-to-$500k objective, milestones, capital constraints, strategy research backlog |
| Controller and hierarchical actors | CIO and specialist research committee |
| Durable actor state/checkpoints | Long research/backfill/backtest resume |
| Conversations/event fabric | Findings, blockers, questions, corrections, and escalation |
| Company memory and role lessons | Private thesis context and validated research lessons |
| Skill/procedure system | Versioned sector/research/data-recovery procedures |
| Approvals inbox | Exact live-order and risk-policy approvals |
| Governance and kill switch | Capability, credential, data, and money boundaries |
| Audit chain | Evidence, decision, approval, and order provenance |
| Findings/incidents | Data, model, strategy, execution, and infrastructure failures |
| Cost metering | Per-provider, model, strategy, and workflow economics |
| Chief-of-staff/proactive notifications | Daily brief, decision queue, risk and stalled-work updates |

The current memory design already distinguishes working memory, company facts, and role lessons, but its own design notes that the experiential writer and stronger provenance/promotion path need completion. The investment vertical should consume the governed version, not write unreviewed market beliefs into global shared memory.

### 17.1 Direct Hermes integration decision

Hermes Agent is already installed on the development machine at version 0.16.0. Do not reimplement its mature profile, memory, skill, scheduling, and research loop merely to claim native ownership. Run a bounded direct-integration evaluation as an early build task.

Create a dedicated **investment-research** Hermes profile with:

- A Docker terminal backend and no host secrets forwarded.
- No broker, banking, email, messaging, or live Agent OS write credentials.
- Read-only access to approved research snapshots and a narrow Agent OS research MCP.
- Memory write approval enabled.
- Skill write approval enabled.
- No unattended skill installation from public registries.
- No autonomous gateway/cron activation until threat tests pass.
- An output-only research inbox that Agent OS validates before importing.

Agent OS remains the system of record and sole controller for data entitlements, strategy versions, risk, approvals, orders, audit, and incidents. Hermes may act as a research and procedure-learning specialist. It cannot be on the money path.

Evaluate Hermes and the native Agent OS research fleet on the same benchmark set:

- Primary-source retrieval and citation accuracy.
- Numeric/factual extraction accuracy.
- Contradiction and missing-evidence detection.
- Cross-session recall.
- Quality of proposed reusable procedures.
- Prompt-injection resistance.
- Reproducibility.
- Latency and model cost.
- Unsafe or unauthorized tool attempts.

Keep Hermes for the tasks where it materially wins. Do not force a single framework to perform every role. After the read-only evaluation passes, its strongest validated skills can remain executed by Hermes or be promoted through Agent OS governance without copying implementation blindly.

## 18. Long-running work, health, and communication

### 18.1 No arbitrary hard timeout

A maximum wall-clock duration is not a completion definition. Runs shall use:

- Durable stage checkpoints.
- Idempotent jobs and resumable cursors.
- Heartbeats plus evidence of meaningful progress.
- Per-stage expected ranges learned from history.
- Stagnation thresholds that trigger diagnosis.
- Provider and model circuit breakers.
- Bounded retries with backoff and a declared fallback.
- Explicit market/event expiration separate from runtime timeout.

A run may be genuinely long because a source is slow, a backfill is large, a market event has not occurred, or a low-frequency strategy needs observations. It must remain inspectable and recoverable.

### 18.2 Required trace for every external call

- Correlation, run, stage, and agent identifiers.
- Provider/model/tool and endpoint/operation.
- Request time, queue delay, duration, retry count, status, and rate-limit headers.
- Input/output token counts and cost for model calls.
- Bytes and record count for data calls.
- Cache hit/miss and freshness.
- Sanitized request parameters and response hash.
- Decision made because of the result.
- Next state transition.

Secrets and licensed content must be redacted from traces.

### 18.3 Stagnation diagnosis

The operations coordinator shall distinguish:

- Actively computing.
- Waiting on external provider.
- Rate limited.
- Waiting on market time/event.
- Waiting on a child agent.
- Encoding/indexing evidence.
- Retrying the same failure.
- Deadlocked or orphaned.
- Waiting for human approval.

When progress is slower than expected, it should inspect the trace and decide: continue, reduce scope safely, switch provider/model, split the job, add a worker, retry from checkpoint, quarantine data, or escalate. It must not repeatedly say “still running” without the causal stage and new evidence.

### 18.4 Proactive status message contract

Each material update states:

- Goal.
- Current stage.
- Completed artifacts/counts.
- Work in progress.
- Last meaningful progress time.
- Cause of delay or uncertainty.
- Decision taken and why.
- Next checkpoint.
- Current ETA range if estimable.
- Cost so far.
- Whether human action is actually required.

## 19. Security, privacy, and licensing

- Single-user authentication with phishing-resistant MFA where supported.
- Private network/VPN access initially; no public internet exposure by default.
- Broker credentials in a secrets manager, never in prompts, logs, memory, or source files.
- Separate read-only, paper, and live credentials.
- Runtime capability tokens scoped by environment and action.
- Live order service isolated from browsing, document parsing, and general-purpose shell tools.
- Treat all filings, news, websites, PDFs, emails, and social content as untrusted input capable of prompt injection.
- Sanitize and structurally extract documents before agent use.
- Allowlist outbound domains per connector.
- Encrypt data at rest and in transit.
- Signed, append-only approval/audit records with retention and tested restore.
- Software dependency and skill supply-chain scanning.
- Data entitlement enforcement at query and export boundaries.
- Private-use licenses must not leak into a later public product.
- Public/commercial use, advice to others, or account management requires a separate legal and licensing review.

## 20. User experience requirements

### 20.1 Home cockpit

- Net worth buckets and protected reserve.
- Portfolio value, cash, benchmark-relative performance, and drawdown.
- Current exposures and risk-limit consumption.
- Decisions awaiting approval.
- Thesis changes and new disconfirming evidence.
- Upcoming events.
- Data/provider/system health.
- Active research runs with exact stage and progress.
- Daily “what changed / why it matters / what to do” brief.

### 20.2 Company dossier

- Business and security identity.
- Latest and historical primary documents.
- Financial quality and changes.
- Valuation scenarios with editable assumptions.
- Ownership/insider activity context.
- News/catalyst timeline.
- Market/liquidity/volatility context.
- Bull, base, and bear cases.
- Contradictions and missing evidence.
- Thesis history and invalidation.
- Current portfolio relevance.

### 20.3 Strategy lab

- Registered hypothesis and mechanism.
- Experiment history including failures.
- Data coverage and bias checklist.
- In/out-of-sample and walk-forward results.
- Cost and stress controls.
- Benchmarks and simpler alternatives.
- Promotion state and gate evidence.
- Reproducible code/data/model versions.

### 20.4 Decision/approval screen

- Exact proposed action.
- Evidence and freshness.
- Portfolio/risk impact.
- Costs and alternatives.
- Red-team objection.
- Approve, reject, request more evidence, reduce size, or defer.
- Approval expiry and payload hash.

### 20.5 Trace explorer

- Timeline across data, agent, deterministic calculation, approval, broker, and reconciliation.
- Drill into sanitized calls, evidence, decisions, and state transitions.
- Critical path and time/cost breakdown.
- “Why is this taking long?” diagnosis.

## 21. Functional requirements

### Data

- FR-D01: Register each provider, dataset, entitlement, permitted use, retention, and cost.
- FR-D02: Preserve immutable raw responses and reprocess them.
- FR-D03: Maintain a point-in-time security master and corporate actions.
- FR-D04: Preserve published/received/effective/revision timestamps.
- FR-D05: Detect gaps, conflicts, staleness, and schema drift.
- FR-D06: Block dependent decisions when critical data is invalid.
- FR-D07: Support reproducible dataset snapshots.
- FR-D08: Track every fact to an evidence object and payload hash.

### Research

- FR-R01: Produce cited dossiers and daily briefs.
- FR-R02: Separate facts, calculations, and interpretations.
- FR-R03: Require bull, bear, missing-data, and invalidation sections.
- FR-R04: Version theses and notify on material changes.
- FR-R05: Calibrate confidence against historical outcomes.
- FR-R06: Deduplicate and score event novelty.

### Strategy

- FR-S01: Register hypothesis and holdout before testing.
- FR-S02: Use time-valid data in an event-driven simulator.
- FR-S03: Model realistic costs, fills, and constraints.
- FR-S04: Track all attempted experiments and parameters.
- FR-S05: Run walk-forward, regime, perturbation, and stress tests.
- FR-S06: Independently reproduce promotion candidates.
- FR-S07: Compare with passive, cash, and simpler baselines.
- FR-S08: Maintain immutable promotion evidence.

### Portfolio/risk

- FR-P01: Separate survival reserve and risk capital.
- FR-P02: Reconcile cash, positions, orders, fills, and tax lots.
- FR-P03: Compute exposures and risk deterministically.
- FR-P04: Enforce pre-trade and continuous hard limits.
- FR-P05: Provide risk veto and kill switch independent of agents.
- FR-P06: Attribute returns to market, factor, signal, sizing, and execution.
- FR-P07: Produce goal scenarios without implying guaranteed returns.
- FR-P08: Show the probability and risk tradeoff for the $200,000 3-year and 5-year stretch milestones.
- FR-P09: Model future contributions and non-market income as separate levers rather than assuming returns must close the full gap.

### Execution

- FR-E01: Paper-only credentials through Release 2.
- FR-E02: Bind live approval to the exact order-intent hash.
- FR-E03: Expire approvals and reprice/reapprove material changes.
- FR-E04: Handle partial fills, rejects, cancel/replace, halts, and disconnects.
- FR-E05: Reconcile broker state before new action after restart.
- FR-E06: Keep live order service isolated and least-privileged.

### Learning

- FR-L01: Capture complete decision/outcome episodes.
- FR-L02: Produce candidate lessons with attribution and evidence.
- FR-L03: Replay-test and shadow-test candidates.
- FR-L04: Version, sign, approve, monitor, and roll back promoted artifacts.
- FR-L05: Prevent self-modification of money-path controls.
- FR-L06: Expire or revisit lessons when context changes.

### Operations and communication

- FR-O01: Checkpoint and resume every long-running stage.
- FR-O02: Record per-call latency, status, counts, cost, and decision effect.
- FR-O03: Diagnose stagnation rather than use blind hard timeouts.
- FR-O04: Provide proactive status using the required contract.
- FR-O05: Create incidents for repeated failure, drift, reconciliation, and risk breaches.
- FR-O06: Meter provider/model/infrastructure spend and forecast budget.
- FR-O07: Recover from process and host restart without duplicate orders.

## 22. Non-functional requirements

- **Correctness:** financial arithmetic uses fixed-point decimal; invariants cover cash and double-entry ledger consistency.
- **Reproducibility:** any recommendation/backtest can be recreated from versioned data, code, configuration, and model artifacts.
- **Availability:** research may degrade by provider; execution and risk fail closed.
- **Recovery:** target recovery point under five minutes for transactional state and under 24 hours for reproducible bulk caches; exact values finalized during infrastructure design.
- **Performance:** cached cockpit under two seconds; fresh dossier begins under 60 seconds; deterministic risk check under 100 ms.
- **Durability:** no acknowledged order, approval, thesis, evidence, or audit write is lost after process restart.
- **Privacy:** private data never trains external models unless the owner explicitly enables a provider whose terms permit it.
- **Explainability:** every recommendation exposes sources, calculations, assumptions, counterarguments, and version.
- **Observability:** 100% of provider, model, strategy, risk, approval, and broker transitions share a trace identifier.
- **Cost control:** hard monthly vendor/model budget plus per-workflow forecast; exceeding research budget pauses new discretionary work, never risk monitoring.
- **Security:** least privilege, strong secret isolation, prompt-injection defense, dependency scanning, encrypted backup, and restore drills.
- **Testability:** deterministic replay for ingestion, signals, risk, and execution state machines.

## 23. Testing and assurance

### 23.1 Test layers

- Unit and property tests for financial calculations.
- Contract tests against provider schemas and fixtures.
- Golden tests for filings/news extraction.
- Time-travel tests proving no future data access.
- Dataset invariants and cross-provider reconciliation.
- Backtest reproducibility tests.
- Broker sandbox/paper lifecycle tests.
- Failure injection for rate limits, stale data, disconnects, duplicates, out-of-order events, and partial fills.
- Prompt-injection and secret-exfiltration tests.
- Approval binding and replay-attack tests.
- Crash/restart and exactly-once order-intent tests.
- Restore drills.
- Model evaluation sets for citation, extraction, calibration, and contradiction detection.

### 23.2 Money-path assurance

No live release unless tests prove:

- An agent cannot call the broker directly.
- Unapproved, expired, changed, or replayed intents fail.
- Risk veto cannot be bypassed by an agent or approval.
- Restart cannot duplicate an order.
- Stale/conflicting data prevents new exposure.
- Internal state reconciles with broker state.
- Kill switch works during each order state.
- A malicious document cannot alter policy or expose a secret.

## 24. Delivery plan and time

Engineering and market evidence are different clocks. Software can be built in weeks; consistency cannot be proven without time and observations.

| Phase | Deliverable | Engineering estimate | Evidence clock |
|---|---|---:|---:|
| 0 | Final financial boundary, jurisdiction, data contracts, threat model, isolated Hermes evaluation | 2–4 days | None |
| 1 | Data foundation, cockpit, dossiers, daily brief, thesis ledger | 2–3 weeks | Data-quality soak begins |
| 2 | Strategy registry, event-driven backtester, evaluation/reproduction | 2–3 weeks | Historical/walk-forward |
| 3 | Shadow signals, Alpaca paper, reconciliation, risk, incident/trace UI | 1–2 weeks | Minimum 60 trading days |
| 4 | Limited-live adapter and approval boundary | 1–2 weeks after gates | Six+ months before scaling |
| 5 | Conditional real-time news or additional strategy families | Evidence-driven | Strategy-specific |

Initial private research utility can arrive in roughly three weeks. A reasonably complete paper system is roughly six to ten engineering weeks. The minimum paper clock is about three calendar months and may need to be longer. No honest design can promise whether the $200,000 stretch milestone or $500,000 long-term goal will be reached by a particular date.

## 25. Recommended first build

Build a daily/EOD “private investment committee” using:

- Tiingo Power for long price history.
- Massive Basic initially for reference/cross-check, upgrading to Developer only when backtests need it.
- SEC EDGAR for filings and XBRL.
- FRED/ALFRED, BLS, and Treasury for macro and point-in-time vintages.
- Alpaca Basic paper for execution simulation.
- PostgreSQL + immutable local object storage + Parquet/DuckDB.
- Existing Agent OS controller, durable actors, approvals, audit, memory, incidents, and proactive communication.

Start with:

1. Diversified ETF trend/regime research.
2. Long-horizon quality/value research.
3. Medium-term cross-sectional momentum.
4. Earnings/filing event research without latency-sensitive execution.

These produce useful research with delayed/EOD data and avoid spending $268 more per month before real-time data has a demonstrated use. Add Massive Advanced and Benzinga News only if shadow results show that timeliness creates incremental after-cost value.

## 26. Acceptance criteria for the private paper product

The product is ready for sustained paper use when:

- The owner sees the survival reserve and risk capital separately.
- At least ten years of selected market history and all required reference/corporate-action data pass quality checks, or the UI clearly scopes strategies to the shorter valid period.
- A filing, macro release, price, and news item can each be traced from normalized value to immutable source.
- Dossiers include citations, competing cases, staleness, and missing evidence.
- At least three strategy families use the same reproducible event-driven test engine.
- Holdout and experiment registries prevent silent retesting/cherry-picking.
- Shadow and paper signals share identical strategy code.
- Paper orders, fills, positions, and cash reconcile.
- Risk limits, stale-data halt, and kill switch pass failure drills.
- Runs survive restart and show causal progress/stall diagnostics.
- Every recommendation and proposed order is reproducible.
- The system reports performance versus passive/cash alternatives after modeled costs.
- No component possesses a live-trading credential.

## 27. Conditions required before live money

Live money remains blocked until:

- The owner supplies jurisdiction, tax residence, broker choice, essential monthly burn, obligations, and protected-reserve amount.
- The chosen strategy passes all promotion gates.
- Paper and shadow evidence meet time and observation minimums.
- The live account and credential are isolated.
- Approval, risk, reconciliation, incident, backup, and recovery controls pass.
- A limited-live allocation and loss budget are explicitly approved.
- The owner acknowledges that the allocation can be lost and that the $500,000 goal is not guaranteed.

## 28. Key risks and mitigations

| Risk | Mitigation |
|---|---|
| Financial desperation drives excessive risk | Hard survival reserve, no urgency override, human-visible alternatives |
| Backtest overfitting | Hypothesis registry, holdout, attempt ledger, walk-forward, multiple-testing adjustment |
| Look-ahead/revision bias | Effective timestamps, ALFRED vintages, event-driven replay |
| Survivorship bias | Inactive/delisted instruments and time-valid universes |
| LLM hallucination | Source-required structured output and deterministic calculations |
| False self-learning | Candidate/replay/shadow/approval promotion pipeline |
| News prompt injection | Untrusted-content isolation, structural extraction, no money tools in research runtime |
| Provider outage/schema drift | Raw storage, contract monitors, fallbacks, quarantine, fail-closed |
| Paper/live mismatch | Conservative fills, stress tests, micro allocation, live reconciliation |
| Strategy decay/regime change | Challenger monitoring, drift alerts, exposure reduction and pause |
| Credential/order compromise | Separate service, least privilege, payload-bound approval, kill switch |
| Data licensing breach | Entitlement registry and export enforcement |
| Agent stalls/cost spirals | Durable progress telemetry, causal diagnosis, budget controls, bounded retries |
| Taxes erase apparent edge | Tax-lot ledger, after-tax scenario, CPA review |

## 29. Decisions to finalize immediately before implementation

These do not block the PRD:

- Country, tax residence, and brokerage eligibility.
- Essential monthly expenses and protected reserve.
- Whether all $20,000 is currently liquid and unencumbered.
- Existing brokerage and positions.
- Investment restrictions, debts, near-term obligations, and tax lots.
- Maximum acceptable temporary and permanent loss.
- Target horizon and whether future contributions are possible.
- Initial universe: U.S. ETFs only, U.S. liquid stocks, or both.
- Local-only versus private cloud deployment.
- Initial monthly data/model budget.

## 30. Final product rule

The system's job is not to agree with the owner's desired return. Its job is to keep the owner solvent, find and test real opportunities, reject false edges, communicate clearly, and compound only when evidence justifies risk.
