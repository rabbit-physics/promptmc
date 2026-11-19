# Changelog

All notable engineering milestones in this sanitized public code mirror of PromptMC are summarized below.

## 2026-11-20 — Public Sanitized Mirror
- Translated comments, docstrings, diagnostics, and browser UI copy into English and stripped environment-specific paths, hosts, and proprietary solver labels.

## 2026-11-08 — Cross-Check & Diagnostics Polish
- Fixed nuclide coverage checks, deterministic OpenMC surface/material ID assignments, and input readback comparison verdicts (`p1/backends/openmc_gen.py`, `p1/backends/openmc_read.py`, `p1/backends/compare.py`, `p1/kernel/checks.py`).
- Added per-node solver readiness badges (OpenMC and Reference MC) in the browser node settings dialog (`web/nodes.js`, `p1/server.py`, `p1/nodes/coord.py`).

## 2026-11-07 — Distributed Nodes v3 -> v4, Windows EcoQoS Yield, & Epoch Fencing
- **Execution ownership fencing (`coord.py`, `agent.py`, `runner.py`)**: Persisted terminal delivery states across retries and introduced per-instance monotonic `epoch`, ordered `hb_seq`, and `claim_seq` fencing so stale worker instances receive HTTP `409 node instance superseded` after VM snapshot/WSL2 thaw.
- **Protocol v4 (`PROTO = 4`)**: Added two-phase whole-case submission (`hold`/`activate`/`withdraw` with `HELD_TTL_S = 600`), explicit `execs` attempt records, `RENEW_LIMIT_S = 600` unreachable-node stop confirmation, wall-time budget accounting excluding confirmed suspension intervals, and per-node speed/memory-aware `CasePlan` scheduling.
- **180 s Long-Suspend Replication (`SUSPEND_DUP_S = 180`)**: Replicated stalled whole-case/sharded work onto peer nodes after 180 s of continuous suspension while allowing partial-suspend slot backfill (`suspend N`).
- **Whole-Case Remote Execution (`remote.py`, `jobs.py`)**: Enabled unsharded OpenMC eigenvalue and Reference MC KCODE dispatch to compute nodes with local fallback, statepoint `.h5` checkpoint uploads (`CKPT_S = 120`), and deterministic OpenMC 4-stream PRNG seed spacing.
- **Windows 11 CPU Yield Daemon (`yield_policy.py`, `controller.pyw`, `bench.py`)**: Implemented full-speed default scheduling, `>90%` EcoQoS + E-core pinning, `>=97%` gradual quarter-slot `SIGSTOP` suspension, and immediate battery pause.
- **Workbench & Compute Node Integration (`nodes/`, `web/nodes.js`, `p1/kernel/runner.py`)**: Added interactive `chat` vs. `work` layering, `proposal/respond` trial-run and edit review cards, wall-time budgets, persistent stop records, and the initial Protocol v3 distributed node coordinator and CLI (`ctl.py`, `client.py`).

## 2026-11-06 — Core Kernel, Validation Gate, & Browser Workbench
- Implemented the SQLite event-sourced store (`p1/kernel/store.py`), 24-method JSON-RPC/SSE protocol (`p1/kernel/protocol.py`), deterministic validation rules (`p1/kernel/rules.py`), OpenMC 0.15.3 XML generator/readback comparator, and SSE browser workbench (`web/`).
