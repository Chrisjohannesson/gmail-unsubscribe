# Implementation Plan: Self-Hosted Gmail Unsubscribe Manager

## Current State Summary

| Component | Current Implementation |
|-----------|----------------------|
| **Entrypoint** | `uvicorn app:app` (manual command) |
| **Routes** | 12 endpoints (inbox, email, unsubscribe, sync APIs) |
| **Database** | SQLite `emails` table + FTS, no job/audit tables |
| **Unsubscribe Flow** | In-memory `unsub_status` dict, lost on restart |
| **UI** | 4 Jinja2 templates with Alpine.js |

---

## Milestone A: Single-Command Run

**Goal:** One command to start everything (dependencies, Chrome, server)

### Files to Add
| File | Purpose |
|------|---------|
| `Dockerfile` | Container with Python, Chrome, dependencies |
| `docker-compose.yml` | Orchestrate app + volume mounts |
| `Makefile` | Convenience commands (`make run`, `make dev`, `make sync`) |
| `scripts/entrypoint.sh` | Container startup script |
| `.dockerignore` | Exclude credentials, db, pycache |

### Files to Change
| File | Changes |
|------|---------|
| `app.py` | Add `HOST`/`PORT` env vars, health check endpoint |
| `requirements.txt` | Pin exact versions for reproducibility |

### API Endpoints to Add
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Liveness check for container orchestration |

### DB Changes
None

### UI Changes
None

### Checklist
- [ ] Create `Dockerfile` with Python 3.11 + Chrome headless
- [ ] Create `docker-compose.yml` with volume for `emails.db`
- [ ] Create `Makefile` with targets: `run`, `dev`, `build`, `clean`
- [ ] Add `/health` endpoint returning `{"status": "ok"}`
- [ ] Add env var support: `HOST`, `PORT`, `DB_PATH`
- [ ] Create `.dockerignore` excluding sensitive files
- [ ] Pin all versions in `requirements.txt`
- [ ] Test: `make run` starts everything from scratch

---

## Milestone B: DB-Backed Job System

**Goal:** Unsubscribe runs persist as "jobs" in the database, survive restarts

### Files to Add
| File | Purpose |
|------|---------|
| `models.py` | SQLAlchemy/dataclass models for Job, JobItem |
| `jobs.py` | Job lifecycle: create, start, update, complete |

### Files to Change
| File | Changes |
|------|---------|
| `database.py` | Add `jobs` and `job_items` tables, migration logic |
| `app.py` | Replace in-memory `unsub_status` with DB queries |
| `templates/unsubscribe.html` | Show job history, resume capability |

### API Endpoints to Add/Change
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/jobs` | GET | List all jobs (paginated) |
| `/api/jobs` | POST | Create new unsubscribe job |
| `/api/jobs/{job_id}` | GET | Get job details + items |
| `/api/jobs/{job_id}/retry` | POST | Retry failed items in a job |
| `/api/unsub-status` | GET | **Change:** Read from DB instead of memory |

### DB Tables to Add
```sql
jobs
├── id (TEXT PRIMARY KEY, UUID)
├── status (TEXT: pending/running/completed/failed)
├── created_at (TEXT, ISO timestamp)
├── started_at (TEXT, nullable)
├── completed_at (TEXT, nullable)
├── total_items (INTEGER)
├── completed_items (INTEGER)
├── successful_items (INTEGER)
├── failed_items (INTEGER)

job_items
├── id (INTEGER PRIMARY KEY)
├── job_id (TEXT, FK → jobs.id)
├── sender (TEXT)
├── sender_email (TEXT)
├── unsubscribe_url (TEXT)
├── unsubscribe_mailto (TEXT)
├── method_attempted (TEXT: one-click/browser/mailto)
├── status (TEXT: pending/running/success/failed)
├── error_message (TEXT, nullable)
├── attempted_at (TEXT, nullable)
├── retry_count (INTEGER, default 0)
```

### UI Changes
| Template | Changes |
|----------|---------|
| `unsubscribe.html` | Add "Job History" section showing past runs |
| `unsubscribe.html` | Add "Retry Failed" button per job |
| New: `job_detail.html` | Detailed view of single job with all items |

### Checklist
- [ ] Design `jobs` table schema
- [ ] Design `job_items` table schema
- [ ] Add migration in `init_db()` for new tables
- [ ] Create `jobs.py` with `create_job()`, `start_job()`, `update_item()`, `complete_job()`
- [ ] Refactor `run_mass_unsubscribe()` to write to DB
- [ ] Add `/api/jobs` endpoints (list, create, get, retry)
- [ ] Update `/api/unsub-status` to read from active job
- [ ] Add job history UI section
- [ ] Add retry functionality for failed items
- [ ] Test: Restart server mid-job, verify state persists

---

## Milestone C: Persisted Audit Trail

**Goal:** Full history of every unsubscribe attempt with timestamps, methods, outcomes

### Files to Add
| File | Purpose |
|------|---------|
| `audit.py` | Audit log helpers: `log_attempt()`, `get_history()` |

### Files to Change
| File | Changes |
|------|---------|
| `database.py` | Add `audit_log` table |
| `app.py` | Log every unsubscribe attempt to audit table |
| `jobs.py` | Call audit logger on each item completion |

### API Endpoints to Add
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/audit` | GET | Query audit log (filter by sender, date, outcome) |
| `/api/audit/export` | GET | Export audit log as CSV |
| `/api/senders/{email}/history` | GET | All attempts for a specific sender |

### DB Tables to Add
```sql
audit_log
├── id (INTEGER PRIMARY KEY)
├── timestamp (TEXT, ISO with timezone)
├── job_id (TEXT, FK → jobs.id, nullable for manual)
├── sender (TEXT)
├── sender_email (TEXT)
├── action (TEXT: unsubscribe_attempt/unsubscribe_success/unsubscribe_fail/manual_open)
├── method (TEXT: one-click/browser/mailto/manual)
├── url_used (TEXT)
├── http_status (INTEGER, nullable)
├── error_message (TEXT, nullable)
├── duration_ms (INTEGER)
├── retry_number (INTEGER)
```

### UI Changes
| Template | Changes |
|----------|---------|
| `unsubscribe.html` | Per-sender: show last attempt date, total attempts |
| New: `audit.html` | Searchable audit log viewer with filters |
| `base.html` | Add "Audit Log" nav link |

### Checklist
- [ ] Design `audit_log` table schema
- [ ] Add migration for audit table
- [ ] Create `audit.py` with `log_attempt()` function
- [ ] Instrument `bounded_one_click()` to log attempts
- [ ] Instrument `browser_unsubscribe_worker()` to log attempts
- [ ] Instrument `send_unsubscribe_email()` to log attempts
- [ ] Add `/api/audit` endpoint with date/sender filters
- [ ] Add `/api/audit/export` CSV endpoint
- [ ] Create `audit.html` template with search/filter
- [ ] Add attempt history to sender rows in unsubscribe view
- [ ] Test: Verify all attempts logged with correct timestamps

---

## Milestone D: Safety Controls + Strategy Ladder

**Goal:** Prevent accidental mass unsubscribes, enforce deterministic method order

### Concepts

**Strategy Ladder** (deterministic order, user-configurable):
```
Level 1: One-Click HTTP POST (safest, RFC 8058 compliant)
   ↓ fails
Level 2: mailto: Send unsubscribe email
   ↓ fails
Level 3: Browser automation (Selenium click)
   ↓ fails
Level 4: Manual queue (user opens in browser)
```

**Safety Controls:**
- Confirmation required for >10 senders
- Daily unsubscribe limit (default: 50/day)
- Cooldown period between attempts to same sender (default: 24h)
- Dry-run mode (simulate without executing)
- Blocklist/allowlist for senders

### Files to Add
| File | Purpose |
|------|---------|
| `config.py` | User-configurable settings (limits, cooldowns, strategy) |
| `strategy.py` | Strategy ladder logic, method selection |
| `safety.py` | Rate limiting, cooldowns, confirmations |

### Files to Change
| File | Changes |
|------|---------|
| `database.py` | Add `settings` and `sender_rules` tables |
| `app.py` | Integrate safety checks before job creation |
| `jobs.py` | Use strategy ladder for method selection |
| `templates/unsubscribe.html` | Add confirmation modal, dry-run toggle |

### API Endpoints to Add
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/settings` | GET | Get current settings |
| `/api/settings` | PUT | Update settings |
| `/api/senders/{email}/block` | POST | Add sender to blocklist |
| `/api/senders/{email}/unblock` | DELETE | Remove from blocklist |
| `/api/dry-run` | POST | Simulate unsubscribe (no actual requests) |

### DB Tables to Add
```sql
settings
├── key (TEXT PRIMARY KEY)
├── value (TEXT, JSON-encoded)
├── updated_at (TEXT)

-- Example keys:
-- daily_limit: 50
-- cooldown_hours: 24
-- strategy_order: ["one-click", "mailto", "browser", "manual"]
-- require_confirmation_threshold: 10
-- dry_run_default: false

sender_rules
├── id (INTEGER PRIMARY KEY)
├── sender_email (TEXT UNIQUE)
├── rule_type (TEXT: block/allow/skip)
├── reason (TEXT, nullable)
├── created_at (TEXT)
```

### UI Changes
| Template | Changes |
|----------|---------|
| `unsubscribe.html` | Add confirmation modal for large batches |
| `unsubscribe.html` | Add dry-run checkbox |
| `unsubscribe.html` | Show blocked senders grayed out |
| `unsubscribe.html` | Display cooldown status per sender |
| New: `settings.html` | Settings page for limits, strategy order |

### Strategy Ladder Implementation
```python
# strategy.py
STRATEGY_ORDER = ["one-click", "mailto", "browser", "manual"]

async def execute_unsubscribe(item: dict, settings: dict) -> Result:
    """Execute unsubscribe using strategy ladder."""
    for method in settings.get("strategy_order", STRATEGY_ORDER):
        if not item_supports_method(item, method):
            continue

        result = await attempt_method(item, method)
        log_attempt(item, method, result)

        if result.success:
            return result

        if result.should_stop:  # e.g., 4xx error = don't retry
            break

    return Result(success=False, method="exhausted", message="All methods failed")
```

### Safety Check Flow
```python
# safety.py
async def pre_flight_check(items: list, settings: dict) -> SafetyResult:
    """Run all safety checks before job creation."""

    # 1. Check daily limit
    today_count = await get_today_unsubscribe_count()
    if today_count + len(items) > settings["daily_limit"]:
        return SafetyResult(blocked=True, reason=f"Daily limit ({settings['daily_limit']}) exceeded")

    # 2. Filter blocked senders
    blocked = await get_blocked_senders([i["sender_email"] for i in items])
    items = [i for i in items if i["sender_email"] not in blocked]

    # 3. Filter cooldown (attempted within N hours)
    on_cooldown = await get_senders_on_cooldown(items, settings["cooldown_hours"])
    items = [i for i in items if i["sender_email"] not in on_cooldown]

    # 4. Require confirmation if above threshold
    needs_confirmation = len(items) > settings["require_confirmation_threshold"]

    return SafetyResult(
        blocked=False,
        items=items,
        skipped_blocked=len(blocked),
        skipped_cooldown=len(on_cooldown),
        needs_confirmation=needs_confirmation
    )
```

### Checklist
- [ ] Create `config.py` with default settings
- [ ] Create `settings` table with key-value store
- [ ] Create `sender_rules` table for block/allow lists
- [ ] Create `strategy.py` with ladder logic
- [ ] Create `safety.py` with rate limiting, cooldown checks
- [ ] Add `/api/settings` GET/PUT endpoints
- [ ] Add `/api/senders/{email}/block` endpoint
- [ ] Add `/api/dry-run` endpoint
- [ ] Integrate safety checks in `process_unsubscribe()`
- [ ] Add confirmation modal to UI (shows: count, blocked, cooldown)
- [ ] Add dry-run toggle to UI
- [ ] Add settings page with strategy order drag-drop
- [ ] Add cooldown indicator per sender row
- [ ] Test: Hit daily limit, verify blocking
- [ ] Test: Attempt same sender twice within cooldown
- [ ] Test: Dry-run mode logs but doesn't execute

---

## Execution Order

```
Milestone A (Foundation)     ← Start here
     ↓
Milestone B (Job System)     ← Core persistence
     ↓
Milestone C (Audit Trail)    ← Observability
     ↓
Milestone D (Safety)         ← Production-ready
```

**Estimated Effort:**
- Milestone A: 2-3 hours
- Milestone B: 4-6 hours
- Milestone C: 3-4 hours
- Milestone D: 5-7 hours

---

## Quick Reference: All New Tables

```sql
-- Milestone B
CREATE TABLE jobs (...);
CREATE TABLE job_items (...);

-- Milestone C
CREATE TABLE audit_log (...);

-- Milestone D
CREATE TABLE settings (...);
CREATE TABLE sender_rules (...);
```

## Quick Reference: All New Endpoints

| Milestone | Endpoint | Method |
|-----------|----------|--------|
| A | `/health` | GET |
| B | `/api/jobs` | GET, POST |
| B | `/api/jobs/{id}` | GET |
| B | `/api/jobs/{id}/retry` | POST |
| C | `/api/audit` | GET |
| C | `/api/audit/export` | GET |
| C | `/api/senders/{email}/history` | GET |
| D | `/api/settings` | GET, PUT |
| D | `/api/senders/{email}/block` | POST, DELETE |
| D | `/api/dry-run` | POST |

## Quick Reference: All New Files

| Milestone | File |
|-----------|------|
| A | `Dockerfile`, `docker-compose.yml`, `Makefile`, `scripts/entrypoint.sh` |
| B | `models.py`, `jobs.py` |
| C | `audit.py` |
| D | `config.py`, `strategy.py`, `safety.py`, `templates/settings.html` |
