# ZAO Fractal Bot — Comprehensive Audit & Roadmap

**Date:** 2026-03-27
**Auditor:** Claude Opus 4.6
**Project:** `/Users/zaalpanthaki/Documents/fractalbotmarch2026`

---

## Table of Contents

1. [Best Practices Audit](#1-best-practices-audit)
2. [Dead Code Analysis](#2-dead-code-analysis)
3. [Strategic Roadmap & Next Steps](#3-strategic-roadmap--next-steps)

---

# 1. Best Practices Audit

**29 findings total:** 1 Critical, 5 High, 16 Medium, 7 Low

## 1.1 Security

### CRITICAL-001: Live Discord Bot Token in `.env` File
- **File:** `.env`, line 1
- **Severity:** Critical
- **Issue:** The `.env` file contains a live Discord bot token and client secret in plaintext. While `.env` is in `.gitignore`, if the machine is compromised or the file accidentally shared, the bot is fully compromised.
- **Fix:** Rotate the token via the Discord Developer Portal. Also rotate the Alchemy API key. Consider a secrets manager or OS keychain.

### HIGH-001: Hardcoded Webhook Secret Defaults
- **File:** `utils/web_integration.py`, lines 30-31
- **Severity:** High
- **Issue:** `WebIntegration.__init__` falls back to placeholder strings (`'https://your-app.vercel.app/api/webhook'` and `'your_webhook_secret'`) when env vars are missing. If env vars are unset in production, the bot attempts HTTP requests to a placeholder URL with a guessable bearer token.
- **Fix:** Raise an error or disable webhook integration when env vars are not set.

### MEDIUM-001: No Input Sanitization on User-Supplied Proposal Text
- **File:** `cogs/proposals.py`, lines 1162-1189
- **Severity:** Medium
- **Issue:** `/propose` passes `title` and `description` into Discord embeds without sanitization. Users could inject misleading markdown (fake `@everyone` pings, crafted URLs).
- **Fix:** Strip or escape `@everyone`, `@here`, and URL-masking markdown from user-supplied text.

### MEDIUM-002: Unvalidated External URL in /curate Command (SSRF Risk)
- **File:** `cogs/proposals.py`, lines 1201-1248
- **Severity:** Medium
- **Issue:** `/curate` accepts any URL and the bot fetches it server-side via `_scrape_og_tags()`. This enables SSRF — a user could pass internal network addresses.
- **Fix:** Validate `https://` scheme only, reject private/internal IP ranges, consider domain allow-list.

### MEDIUM-003: `html` Module Name Collision (Bug)
- **File:** `cogs/proposals.py`, lines 37, 244, 259
- **Severity:** Medium
- **Issue:** `import html` at line 37 is shadowed by `html = await resp.text()` at line 244. Then `html.unescape()` at line 259 calls `str.unescape()` which does not exist — this is a live runtime crash waiting to happen, currently hidden by the bare `except`.
- **Fix:** Rename the variable at line 244 to `page_html`.

## 1.2 Error Handling

### HIGH-002: Bare `except Exception` Swallowing Errors
- **File:** `cogs/proposals.py`, lines 260-261
- **Severity:** High
- **Issue:** `_scrape_og_tags` catches `Exception` and does `pass` — completely swallowing errors including the `html` shadowing bug above.
- **Fix:** Log the exception. Let the caller know scraping failed.

### MEDIUM-004: Broad Exception Handler in ENS Resolution
- **File:** `cogs/wallet.py`, lines 86-88
- **Severity:** Medium
- **Issue:** `resolve_ens` wraps two resolution strategies in a single `except Exception` block. If Strategy 1 fails for an unrelated reason, Strategy 2 never runs.
- **Fix:** Wrap each strategy in its own try/except.

### MEDIUM-005: Missing Error Handling for TOKEN=None
- **File:** `main.py`, lines 43, 227
- **Severity:** Medium
- **Issue:** `TOKEN = os.getenv('DISCORD_TOKEN')` can return `None`. `bot.start(TOKEN)` would then raise an opaque `LoginFailure`.
- **Fix:** Add: `if not TOKEN: raise RuntimeError("DISCORD_TOKEN environment variable is required")`

### LOW-001: Unhandled `json.JSONDecodeError` on Data File Load
- **Files:** `cogs/wallet.py:203`, `cogs/intro.py:88`, `cogs/history.py:48`, `cogs/proposals.py:292`, `cogs/hats.py:321`
- **Severity:** Low
- **Issue:** All `_load()` methods use `json.load(f)` without catching `JSONDecodeError`. Corrupted files crash the cog.
- **Fix:** Add try/except for `JSONDecodeError`, log warning, fall back to empty data.

## 1.3 Concurrency

### HIGH-003: Race Condition in Wallet Registry Writes
- **File:** `cogs/wallet.py`, lines 217-220, 268-272
- **Severity:** High
- **Issue:** `WalletRegistry.register()` and `add_name_mapping()` modify dicts and call `_save()` without locking. Two simultaneous `/register` calls can interleave and lose data. Same applies to all JSON-backed stores.
- **Fix:** Add an `asyncio.Lock` to each store for read-modify-write sequences.

### HIGH-004: Race Condition in FractalGroup Vote Processing
- **File:** `cogs/fractal/group.py`, lines 342-391
- **Severity:** High
- **Issue:** Two simultaneous vote button clicks could both read the same count, both conclude no winner, and miss a winner or double-advance. The `_groups_lock` exists but is never used.
- **Fix:** Acquire lock around `process_vote` + `check_for_winner`.

### MEDIUM-006: `_seen_interactions` Not Thread-Safe
- **File:** `main.py`, lines 81-102
- **Severity:** Medium
- **Issue:** Dedup cache is a plain `OrderedDict`. Safe in single-threaded asyncio, but fragile if the bot ever moves to multi-threaded sharding.
- **Fix:** Document single-threaded assumption or add a lock.

### MEDIUM-007: Shared Mutable State in PresentationTimer
- **File:** `cogs/timer.py`, lines 60-91
- **Severity:** Medium
- **Issue:** `reactions` and `raised_hands` modified from callbacks and countdown coroutine concurrently.
- **Fix:** Use asyncio.Lock or document cooperative-scheduling assumption.

## 1.4 Discord.py Best Practices

### MEDIUM-008: Non-Persistent Timer Views
- **File:** `cogs/timer.py`, line 589
- **Severity:** Medium
- **Issue:** `TimerControlView` uses `timeout=None` but is not re-registered on restart. Button clicks fail silently after bot restart.
- **Fix:** Accept timers as ephemeral (use finite timeout), or implement persistent view re-registration.

### MEDIUM-009: Voting Buttons Missing Group-Specific Prefix
- **File:** `cogs/fractal/views.py`, line 82
- **Severity:** Medium
- **Issue:** `custom_id=f"vote_{candidate.id}"` — no fractal-group prefix. Simultaneous groups sharing a member could route clicks incorrectly.
- **Fix:** Include thread ID: `f"vote_{thread.id}_{candidate.id}"`.

### MEDIUM-010: `add_name_mapping` Does Not Use Atomic Save
- **File:** `cogs/wallet.py`, lines 271-272
- **Severity:** Medium
- **Issue:** Only write path that uses `json.dump()` directly instead of `atomic_save()`. Crash during write corrupts the file.
- **Fix:** Use `atomic_save(NAMES_FILE, self._name_wallets)`.

### LOW-002: `check_voice_state` Minimum Hardcoded to 1
- **File:** `cogs/base.py`, line 130
- **Severity:** Low
- **Issue:** Check uses `< 1` but config defines `MIN_GROUP_MEMBERS = 2`. Config constant is never used.
- **Fix:** Use `MIN_GROUP_MEMBERS` from config.

### LOW-003: Duplicate Admin Check Pattern
- **Files:** Multiple cogs
- **Severity:** Low
- **Issue:** `BaseCog` and `WalletCog` both define `is_supreme_admin()`. Pattern repeated ~15 times.
- **Fix:** Create a `@is_admin` decorator.

## 1.5 Data Integrity

### HIGH-005: No Backup Strategy for JSON Data Files
- **File:** `data/` directory
- **Severity:** High
- **Issue:** All persistent state in flat JSON with no backup mechanism. Disk failure = total data loss.
- **Fix:** Periodic timestamped backups, or migrate to SQLite.

### MEDIUM-011: History IDs Fragile (Length-Based)
- **File:** `cogs/history.py`, line 76
- **Severity:** Medium
- **Issue:** `entry['id'] = len(self._data['fractals']) + 1` — deleting a record causes ID collisions.
- **Fix:** Track `next_id` as a separate counter (like ProposalStore).

### MEDIUM-012: `get_all()` Returns Direct Reference
- **File:** `cogs/history.py`, line 98
- **Severity:** Medium
- **Issue:** Returns `self._data['fractals']` directly. Callers can accidentally mutate internal state.
- **Fix:** Return `list(self._data['fractals'])`.

## 1.6 Code Quality

### MEDIUM-013: Duplicated Blockchain Query Logic
- **Files:** `cogs/guide.py:293-390`, `cogs/proposals.py:84-192`, `cogs/hats.py:117-134`
- **Severity:** Medium
- **Issue:** Three independent `_eth_call` implementations with slight variations. Contract addresses duplicated.
- **Fix:** Extract shared `utils/blockchain.py` module.

### MEDIUM-014: Magic Numbers Throughout
- **Severity:** Medium
- **Examples:** `200` dedup cache size, `7` days proposal expiry, `45` seconds overtime, `300` seconds cache TTL, `6` max group size.
- **Fix:** Named constants in `config/config.py`.

### LOW-004: Messy hashlib Imports in wallet.py
- **File:** `cogs/wallet.py`, line 99
- **Fix:** Import once at top level.

### LOW-005: `utils/logging.py` Is Unused
- **File:** `utils/logging.py`
- **Fix:** Delete or refactor `main.py` to use it.

### LOW-006: `_remaining_when_paused` Used Before Assignment
- **File:** `cogs/timer.py`, line 541
- **Fix:** Initialize `self._remaining_when_paused = 0` in `__init__`.

## 1.7 Configuration

### MEDIUM-015: Hardcoded Discord Channel/Role IDs
- **File:** `config/config.py`, lines 22, 77, 93, 97
- **Fix:** Load from environment variables with hardcoded defaults.

### MEDIUM-016: Hardcoded Contract Addresses in Multiple Files
- **Files:** `cogs/guide.py:36-41`, `cogs/proposals.py:70-73`, `cogs/hats.py:37-40`
- **Fix:** Centralize in `config/config.py`.

### LOW-007: Hardcoded Channel Name Lookups
- **File:** `cogs/fractal/cog.py`, lines 248-251
- **Fix:** Use configurable channel IDs from config.

---

# 2. Dead Code Analysis

**Findings:** 7 unused imports, 6 unused functions, 5 unused variables, 6 unused config constants, 6 duplicate patterns, 2 stale files, 1 bug

## 2.1 Unused Imports

| File | Line | Import | Recommendation |
|------|------|--------|----------------|
| `utils/logging.py` | 1 | `import logging` | **Remove entire file** — never imported |
| `cogs/fractal/group.py` | 31 | `quote` from urllib.parse | **Remove** — only `urlencode` is used |
| `cogs/fractal/views.py` | 21 | `Callable, Dict, List` from typing | **Remove** — unused, Python 3.12 doesn't need them |
| `cogs/fractal/cog.py` | 25 | `timezone` from datetime | **Remove** — never used in file |
| `cogs/fractal/cog.py` | 28 | `FractalGroup` from .group | **Remove** — only in docstrings, groups created in views.py |
| `cogs/timer.py` | 31 | `tasks` from discord.ext | **Remove** — no `@tasks.loop` in timer |
| `cogs/proposals.py` | 37 | `import html` | **Bug** — shadowed by local variable at line 243. Rename the variable. |

## 2.2 Unused Functions/Methods

| File | Line | Function | Why Unused | Recommendation |
|------|------|----------|------------|----------------|
| `utils/logging.py` | 3 | `setup_logging()` | Never imported. `main.py` configures logging directly. | **Remove entire file** |
| `cogs/fractal/cog.py` | 63 | `_get_next_group_name()` | Groups now use `FractalNameModal`. | **Remove** |
| `cogs/fractal/group.py` | 124 | `add_member()` | `admin_add_member` manipulates `group.members` directly. | **Remove** or refactor admin cmd to use it |
| `cogs/hats.py` | 228 | `_get_next_id()` | Never called. Also remove `SELECTOR_GET_NEXT_ID` at line 49. | **Remove** |
| `utils/web_integration.py` | 132 | `notify_fractal_paused()` | Never called by `admin_pause_fractal`. | **Remove** |
| `utils/web_integration.py` | 140 | `notify_fractal_resumed()` | Never called by `admin_resume_fractal`. | **Remove** |

## 2.3 Unused Variables/Attributes

| File | Line | Variable | Recommendation |
|------|------|----------|----------------|
| `cogs/fractal/cog.py` | 58 | `self._groups_lock` | Never acquired. **Remove** (or actually use it — see race condition HIGH-004). |
| `cogs/fractal/cog.py` | 61 | `self.admin_group` | Never mounted on command tree. **Remove**. |
| `cogs/fractal/views.py` | 297 | `self.custom_name` | Stored but never read. **Remove**. |
| `cogs/fractal/views.py` | 298 | `self.awaiting_modification` | Set to `True` but never checked. **Remove** or implement handler. |
| `cogs/hats.py` | 378 | `self._wearer_cache` | Initialized as `{}` but never populated or read. Also remove `WEARER_CACHE_TTL`. **Remove** (or actually use it — see Quick Wins). |

## 2.4 Unused Config Constants

| File | Line | Constant | Used? | Recommendation |
|------|------|----------|-------|----------------|
| `config/config.py` | 29 | `MAX_GROUP_MEMBERS` | No — hardcoded `6` used instead | **Refactor** code to use it |
| `config/config.py` | 30 | `MIN_GROUP_MEMBERS` | No — hardcoded `1` used instead | **Refactor** code to use it |
| `config/config.py` | 39 | `STARTING_LEVEL` | No — hardcoded `6` in group.py | **Refactor** code to use it |
| `config/config.py` | 40 | `ENDING_LEVEL` | No — hardcoded `1` in group.py | **Refactor** code to use it |
| `config/config.py` | 48-53 | `BUTTON_STYLES` | No — local styles defined in views | **Remove** |
| `config/config.py` | 69 | `THREAD_PREFIX` | No — thread names built inline | **Remove** |

## 2.5 Redundant/Duplicate Code

| Pattern | Locations | Recommendation |
|---------|-----------|----------------|
| `_eth_call` implementations | `proposals.py`, `guide.py`, `hats.py` (~150 lines duplicated) | **Extract** `utils/blockchain.py` |
| Contract address constants | `proposals.py:70-73`, `guide.py:36-41` | **Move** to `config/config.py` |
| Interaction dedup | `main.py:80-102` AND `cogs/base.py:24-65` (double-checked) | **Remove one layer** |
| `is_supreme_admin()` | `BaseCog` AND `WalletCog` (identical) | **Inherit** `WalletCog` from `BaseCog` |
| Proposal closing logic | `_catchup_expiry` AND `_expire_proposals` (near-identical) | **Extract** `_close_expired_proposal()` |
| Vote processing logic | `ProposalVoteView._handle_vote()` AND `GovernanceVoteView` callback | **Extract** `_process_vote()` |

## 2.6 Stale Files

| File | Issue | Recommendation |
|------|-------|----------------|
| `test_timer.py` | Standalone debug script, no assertions or test framework. Was for duplicate message bug (now fixed). | **Remove** |
| `utils/logging.py` | Never imported anywhere. | **Remove** |
| `data/history.json` entry #1 | Test data: `"Fractal 1test - Group 2test"` | **Clean up** |
| `data/proposals.json` #2-4 | Raw URLs as titles — early test data | **Clean up** |

---

# 3. Strategic Roadmap & Next Steps

## 3.1 Architecture Improvements

### P0: Migrate from JSON to SQLite (Medium effort)
All persistence is via flat JSON files loaded entirely into memory. Problems: no concurrent write safety, linear scans for queries, no transactions, unbounded file growth. Migrate to SQLite via `aiosqlite` — ACID transactions, indexed queries, concurrent reads, zero infrastructure. **Do NOT jump to Postgres** — the community size doesn't justify it.

### P1: Deduplicate Onchain RPC Logic (Small effort)
Three modules implement identical `_eth_call`. Each creates a new `aiohttp.ClientSession` per call (no connection pooling). Extract `utils/onchain.py` with a singleton session, generic `eth_call()`, and typed wrappers. Eliminates ~150 lines of duplication.

### P1: Formalize Cross-Cog Communication (Small effort)
Cogs communicate by attaching attributes to the bot instance (`bot.wallet_registry`, `bot.fractal_history`). Create a `BotState` dataclass with typed fields, making dependencies explicit.

### P2: Plugin System for Proposal Types (Medium effort)
Define a `ProposalType` base class with `build_embed()`, `create_vote_view()`, `validate()`. Each type becomes a self-contained class. Pays dividends when adding elections, bounties, etc.

## 3.2 Feature Gaps

### P0: Quorum Requirements for Proposals (Small effort)
Proposals close after 7 days regardless of participation. A 2-vote proposal passes with the same authority as a 25-vote one. Add configurable quorum thresholds (min voters or min % of registered wallets). Display quorum progress on embeds.

### P1: Delegation / Proxy Voting (Medium effort)
Members who can't attend should delegate their Respect weight. Standard DAO governance feature that increases participation rates.

### P1: Recurring Fractal Scheduling (Medium effort)
Every meeting requires manual `/randomize`. Add `/schedule_fractal` for weekly slots — auto-reminder 1hr before, auto-randomize if enough members are in the waiting room.

### P1: Proposal Discussion Summaries (Small effort)
When a proposal closes, auto-summarize thread discussion (top arguments, key participants). Currently just gets a "closed" label.

### P2: Member Reputation Dashboard (Small effort)
Richer `/profile` command showing reputation trajectory, consistency score, peer endorsements.

### P2: Notification Preferences (Medium effort)
Let members `/subscribe` to specific notification types (proposals only, fractals only, results only).

## 3.3 Reliability & Operations

### P0: Structured Logging with File Output (Small effort)
Logs go to stdout only. No persistence, no rotation. Fix `utils/logging.py` as the single config point, add `RotatingFileHandler` to `logs/bot.log`, add JSON formatter for aggregation.

### P0: Health Check Endpoint (Small effort)
No way to externally verify the bot is running. Add a tiny `aiohttp` server responding to `GET /health` with status, guild count, uptime. Enables UptimeRobot/Healthchecks.io monitoring.

### P1: Graceful Shutdown (Small effort)
No signal handling — `SIGTERM` kills mid-operation. Register handlers that set a shutdown flag, wait for in-progress ops, and call `bot.close()`.

### P1: Data Backup Automation (Small effort)
No automated backups. Add a `@tasks.loop(hours=24)` task copying `data/*.json` to timestamped backup directory. Keep 30 days.

### P1: Error Alerting (Small effort)
Errors logged but nobody notified. Add a Discord webhook posting critical errors to `#ops-alerts`.

### P2: Retry Logic for RPC Calls (Small effort)
All `_eth_call` functions fail silently on network errors. Add exponential backoff (1s, 2s, 4s) with max 3 retries.

## 3.4 Scaling Concerns

### What Breaks First: RPC Call Volume (P1, Medium effort)
Hats sync (every 10 min) calls `_is_wearer_of_hat` for every (member, hat) pair. At 100 members x 10 hats = 1,000 RPC calls/10min. Public Optimism RPC will rate-limit.

**Fix:** Use Multicall3 to batch all checks into ~5 calls. Share a single `aiohttp.ClientSession`. Use the existing (but unused!) `_wearer_cache`.

### What Breaks Second: JSON File Size (P2)
Addressed by SQLite migration. `proposals.json` with inline vote data will grow to several MB, making full-file rewrites on every vote slow.

### What Breaks Third: Discord Rate Limits (P2)
`/randomize` moves members one at a time (20 API calls). Add `asyncio.sleep(0.5)` between moves.

## 3.5 Web Dashboard Assessment

**Current:** Next.js 14 with Neon Postgres via Drizzle ORM, next-auth Discord provider, pages for dashboard/leaderboard/respect/guide/admin.

### P0: Two Data Sources Creating Inconsistency (Medium effort)
Web app reads `data/*.json` directly AND receives webhook events writing to Postgres. These diverge. On Vercel (serverless), JSON file reads fail entirely.

**Fix:** Choose one source of truth. Recommended: webhook bridge syncs all data to Postgres, web reads only Postgres.

### P1: Admin Panel Access Control (Small effort)
No role-based access control. Any authenticated Discord user can hit admin endpoints. Verify Supreme Admin role.

### P2: Real-Time Updates (Medium effort)
Dashboard requires manual refresh. Add Server-Sent Events or 10s polling for active fractals.

## 3.6 Onchain Integration Opportunities

### P1: Onchain Proposal Voting (Large effort)
Deploy a governance contract on Optimism recording votes onchain. Bot constructs transaction data and provides signing link. Gives cryptographic verifiability.

### P1: Automated Respect Distribution (Medium effort)
After fractal completes, bot currently generates a `submitBreakout` URL requiring manual click. Auto-submit after a confirmation delay.

### P2: Treasury Balance Display (Small effort)
`/treasury` command reading ZAO multisig/Safe balance on Optimism.

### P2: Token-Gated Channels (Medium effort)
Use Hats Protocol to gate Discord channel access. Infrastructure exists in hats sync loop.

### P2: Onchain Attendance NFTs (Medium effort)
Mint non-transferable NFT per fractal participation for reputation scoring.

## 3.7 Quick Wins (< 1 Day Each)

| Priority | Item | Effort | Impact |
|----------|------|--------|--------|
| P0 | Fix aiohttp session leak — every RPC call creates a new session | 30 min | Prevents socket exhaustion |
| P0 | Use existing `_wearer_cache` in hats.py — defined but never populated | 30 min | Cuts RPC calls dramatically |
| P0 | Fix html variable shadowing bug in proposals.py | 5 min | Fixes broken `/curate` enrichment |
| P0 | Fix/remove `utils/logging.py` | 15 min | Eliminates dead code confusion |
| P1 | Add `/about` command (version, uptime, links) | 30 min | User orientation |
| P1 | Paginate `/admin_wallets` output | 30 min | Usability for growing membership |
| P1 | Add input validation to `/propose` (max_length) | 30 min | Prevents embed overflow |
| P1 | Make RESPECT_POINTS configurable (env var or data/config.json) | 15 min | No-deploy config changes |
| P2 | Add `/randomize` dry-run mode | 45 min | Facilitator confidence |

## 3.8 Recommended Execution Order

**Week 1 — Quick wins and reliability foundation:**
Fix the P0 quick wins (session leak, wearer cache, html bug, logging). Add structured logging and health check. Set up data backups and error alerting.

**Week 2 — Architecture cleanup:**
Extract shared onchain RPC module. Formalize cross-cog state. Batch RPC calls with Multicall3. Remove dead code.

**Week 3 — Web app production readiness:**
Migrate web app to Postgres-only data source. Add admin access control. Add error boundaries.

**Week 4+ — Feature development:**
Add proposal quorum. Build recurring fractal scheduling. Explore onchain voting and automated Respect distribution.

---

## Appendix: Issue Summary

| Category | Critical | High | Medium | Low | Total |
|----------|----------|------|--------|-----|-------|
| Security | 1 | 1 | 3 | — | 5 |
| Error Handling | — | 1 | 2 | 1 | 4 |
| Concurrency | — | 2 | 2 | — | 4 |
| Discord.py | — | — | 3 | 2 | 5 |
| Data Integrity | — | 1 | 2 | — | 3 |
| Code Quality | — | — | 2 | 3 | 5 |
| Configuration | — | — | 2 | 1 | 3 |
| Dead Code | — | — | — | — | 33 items |
| **Total** | **1** | **5** | **16** | **7** | **29 + 33 dead code** |
