# Adversarially-verified audit findings (2026-06-28)

53 distinct real findings.

## 1. [HIGH] api.py auth is fail-open when AOS_API_TOKEN is unset/empty


## 2. [?] Untrusted generated code can escape the srt sandbox by printing a magic string


## 3. [?] Adversarial verification silently drops the tenant's BYO key/engine (thread-local not inherited by pool workers)


## 4. [?] qualityloop 'high' bar advertises adversarial sign-off that never runs (rigor hard-pinned to 2)


## 5. [?] Crash-resume skips REVIEW and reports passed=True, bypassing the REQUEST-CHANGES launch gate


## 6. [?] verify:frontdoor /download/<product


## 7. [?] verify:orchestrate: the actual task


## 8. [?] loopcontroller.resume_stalled() — the durable crash-recovery guarantee — is never wired into the scheduler


## 9. [?] verify:loopcontroller: an exception


## 10. [?] verify:loopcontroller.resume_stalle


## 11. [?] verify:Budget→capability scaler (sc


## 12. [?] verify:Deleting (or disabling) a re


## 13. [?] waits.reply_by SLA deadline is never written by any code — all SLA-breach / overdue-wait detection is dead


## 14. [?] verify:tasksweep reclaims lease-exp


## 15. [?] verify:scheduler.tick runs subproce


## 16. [?] Metered usage has no billing-period window — quota permanently locks out paying tenants and invoices never reset


## 17. [?] GDPR erasure silently abandons earlier deletions on any per-table error yet reports success


## 18. [?] `suspended` tenant flag is read everywhere but written nowhere — no code can ever suspend a tenant


## 19. [?] verify:Enforcement hook fails OPEN


## 20. [?] verify:denied_paths / can_read_secr


## 21. [?] verify:approval_required_for is dec


## 22. [?] verify:verify_approval.py (Article


## 23. [?] verify:Cerbos PDP is not the actual


## 24. [?] Runtime kill-switch fails OPEN on any DB error — an operator HALT is silently ignored during a DB blip


## 25. [?] allocator reap reclaims LIVE leases — heartbeat_at is written once at claim and never refreshed


## 26. [?] verify:redact.scrub does NOT mask A


## 27. [?] /download/<product> serves any tenant's full source zip with NO authentication and NO ownership check


## 28. [?] Cockpit 'Work queue' counts are global, not tenant-scoped (cross-tenant leak + wrong numbers)


## 29. [?] Public status page never reports a service outage — health booleans silently bypass the outage check


## 30. [?] verify:connectors.ingest enforces t


## 31. [?] verify:versions.rollback() permanen


## 32. [?] verify:enforce_manifest PreToolUse


## 33. [?] factory grants tools from AOS_AGENT_TOOLS env, never from the manifest tools[]/denied_tools allowlist


## 34. [?] allowed_paths is never enforced as a positive write-allowlist anywhere


## 35. [?] verify:can_deploy / can_read_secret


## 36. [?] verify:verify_approval.py (hash-pin


## 37. [?] verify:gate_check (stage-gate artif


## 38. [?] Cerbos PDP decision is enforced only in the demo controller.py, not on any path that ships real products


## 39. [?] verify:Boolean capability flags (ca


## 40. [?] approval_required_for is enforced nowhere — gated actions (deploy/spend/secrets/auth/...) are never actually gated


## 41. [?] verify:denied_paths / secret protec


## 42. [?] verify:Every product repo is hard-w


## 43. [?] verify:Deploy/force-push/spawn deny


## 44. [?] enforce_manifest.py fails OPEN on missing pyyaml, unparseable input, or absent manifest


## 45. [?] verify:Lifecycle stage-gates: advan


## 46. [?] gate_check only tests file existence and the controller writes its own stub artifacts — gates self-satisfy


## 47. [?] budget.allow_spend (the runtime cost governor) is never called by the controller it documents


## 48. [?] Closed-loop controller's QA/quality gate is non-enforcing: a failed build still reaches DELIVER and is reported \


## 49. [?] The only real UX gate (console_e2e browser click-through) SKIPs silently and SKIP is counted as PASS


## 50. [?] Scalable verification (static-security + adversarial) is OFF by default in the factory's LAUNCH path


## 51. [?] audit.reseal() re-signs arbitrary content with the live HMAC key, fully defeating the hash-chain tamper-evidence


## 52. [?] Vault secrets are not tenant-scoped — a tenant's BYO model API key is readable by any other tenant's build agent


## 53. [?] verify:HTTP API: empty AOS_API_TOKE


