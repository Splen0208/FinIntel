# Architecture

FinIntel uses a bounded research workflow rather than an unrestricted autonomous planner.

| Layer | Modules | Responsibility |
| --- | --- | --- |
| UI / HTTP | `frontend/`, `backend/app.py` | Query, progress, results, evidence, settings |
| Jobs workflow | `pipeline.py`, `runs.py` | Persistent stages, budgets, checkpoints, manual recovery |
| Social workflow | `social_pipeline.py`, `social_store.py`, `collectors/` | Source-specific collection, note/comment relationships, provenance |
| BOSS adapter | `source_boss.py`, `boss_browser_worker.py`, `boss_rendered_detail.py` | Search in frozen channel; verify rendered detail ID, URL and completeness |
| Model / validation | `ai_provider.py`, `analysis.py`, `social_analysis.py`, `pack.py`, `schemas.py` | Structured analysis, schema validation, exact quote offsets |
| Persistence | `store.py`, `migrations/` | SQLite migrations, source snapshots, analysis receipts |
| Human boundary | `login_bridge.py`, `channel_state.py`, `rate_gate.py`, `review.py` | User login, source readiness, risk stops, explicit review |

Snapshots are evidence, not executable instructions. A conclusion is accepted only when its quote matches the normalized source text at the declared offsets. Complete detail status requires independent page identity and section proof. Identical text with a different evidence receipt gets a separate snapshot; historical evidence metadata stays unchanged.

An interrupted collection keeps already obtained material. Reorganizing uses saved evidence. Resuming consumes remaining budget; it never refunds failed calls. Refreshing after the search allowance is spent starts a new run and preserves the historical run.

BOSS and social sources have different data shapes and separate collection paths. There is no implemented automatic fusion into one cross-platform conclusion. The home page lets the user select a source.

The local app serves static frontend files without a build tool. It stores private state on the same machine. Selected model evidence goes to the configured external inference endpoint. Windows dedicated-browser ownership checks are the validated target for BOSS; portable backend code does not establish platform support on other operating systems.

## Interview research bridge

Migration 018 adds per-session selected social evidence and generated research questions. `/api/interview/start` checks the run/job/snapshot relationship before freezing a JD. Interview preparation keeps JD/resume matching separate from public discussion; the latter only supplies explicitly selected, exact-quote-validated background practice questions. Versioned batch references reject stale model submissions. Changing the JD or resume requires matching again; changing background materials marks existing preparation stale. Research source tables remain read-only from this bridge.
