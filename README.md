# PromptMC — Distributed Monte Carlo Simulation Workbench

PromptMC is a verified Monte Carlo particle-transport simulation workbench and distributed compute orchestrator built around OpenMC. It pairs an event-sourced SQLite kernel and browser workbench with deterministic pre-run physics validation, strict input readback verification, multi-node compute orchestration (Protocol v4), and adaptive Windows/WSL2 host yielding.

## Architecture Overview

### 1. Event-Sourced Kernel & Browser Workbench (`p1/kernel/`, `p1/server.py`, `web/`)
- **JSON-RPC 2.0 + SSE Transport**: Clients issue 12 control requests over `POST /rpc` and receive 12 ordered notifications over `GET /events` (`p1/kernel/protocol.py`), backed by append-only event logs and content-addressed artifact storage in `p1/kernel/store.py`.
- **Two-Layer Mode Separation (`chat` vs. `work`)**: Conversational turns in `chat` mode can propose parameter edits (`proposal/respond`), trial runs (`trial_run`), or transitions into `work` mode with optimistic `version` concurrency control (`409` on stale mutations).
- **Pre-Confirmation Validation & Evidence Gate (`p1/kernel/rules.py`, `p1/kernel/checks.py`, `p1/kernel/acceptance.py`)**:
  - Evaluates five deterministic model and binding rules (`RC-0002` UO2 enrichment, `RC-0003` hydrogenous moderator thermal scattering, `RC-0010` concentric ring/pitch geometry, `RC-0011` nuclear data temperature coverage, `RC-0012` physical density and boron bounds).
  - Performs bidirectional input generation and XML readback verification (`p1/backends/openmc_gen.py`, `p1/backends/openmc_read.py`, `p1/backends/compare.py`) to guarantee generated `model.xml` decks round-trip back to the intended physics model before spending compute cycles.
  - Supports multi-step precheck inspections (`checks.py`) and explicit solver-to-solver or target `k_eff` acceptance gates (`acceptance.py`).

### 2. Distributed Compute Coordinator & Protocol v4 (`p1/nodes/`, `p1/kernel/remote.py`)
- **Two Workload Classes (`p1/nodes/jobs.py`)**:
  - **Sharded fixed-source jobs**: Reference MC decks are split across disjoint history ranges (`RAND GEN=2 HIST=h_k`), while OpenMC shards receive deterministic PRNG seeds `4 * (sha256(job/idx) mod 2^40) + 1` to preserve the 4-stream stride without substream overlap.
  - **Whole-case criticality jobs**: Run unmodified eigenvalue inputs with dynamic thread allocation (`[min_threads, threads]`), periodic progress heartbeats, and HDF5 statepoint (`statepoint.<batch>.h5`) checkpoint uploads (`CKPT_S = 120` s).
- **Monotonic Execution Ownership & Epoch Fencing (`p1/nodes/coord.py`, `p1/nodes/agent.py`)**:
  - Protocol `PROTO = 4` tracks each node instance with a monotonic `epoch`, strictly ordered heartbeat sequences (`hb_seq`), and per-claim counters (`claim_seq`) plus individual `(job, idx, lease)` execution rows in SQLite (`execs`).
  - Stale agent instances (e.g., an orphaned VM process waking after a snapshot restore or WSL2 thaw) are rejected with HTTP `409` (`node instance superseded`), preventing zombie writes or duplicate completion accounting.
- **Two-Phase Whole-Case Commit (`hold` / `activate` / `withdraw`)**:
  - Whole-case remote jobs are created in a held state (`HELD_TTL_S = 600`), durably recorded by the kernel runner, and activated only after persistent delivery records are committed. If an activation window expires or fails, jobs are cleanly withdrawn rather than left orphaned.
- **Unreachable-Node Stop Confirmation & Checkpointed Resume**:
  - Both coordinator and agents enforce `RENEW_LIMIT_S = 600` s: disconnected agents self-terminate active runs after losing coordinator contact for 600 s, allowing the coordinator to confirm stop status and safely re-dispatch from the latest uploaded HDF5 `statepoint.*.h5` without double-execution.
- **Work-Conserving `CasePlan` Scheduling & 180 s Long-Suspend Replication**:
  - `CasePlan` ranks whole-case jobs largest-first using baseline core-second estimates, node memory headroom, and empirical per-thread solver speeds (`CASE_SPEED_RECENT = 8`), yielding heavy runs to faster peers while reclaiming work after slack expiry.
  - If a holder enters sustained suspension for `SUSPEND_DUP_S = 180` s, the coordinator speculatively replicates the shard/case onto an available peer node and deducts confirmed suspension windows from wall-time budget accounting.

### 3. Adaptive Windows 11 / WSL2 CPU Yielding (`deploy/nodes/windows/`)
- `yield_policy.py` and `controller.pyw` run a host daemon polling Windows CPU utilization, `vmmemWSL` usage, and AC power every 5 s:
  - Runs at full speed by default (`mode = 'full'`).
  - Transitions to efficiency mode (`ECO_ENTER = 0.90` sustained for `30` s), enabling Windows EcoQoS and pinning the VM to efficiency cores until projected full-speed utilization drops below `85%` for `120` s.
  - Under near-saturation (`SAT_TOTAL = 0.97`, `SAT_OTHER = 0.60` for `30` s), writes incremental `PAUSE` directives (`suspend N`), triggering `agent.py` to `SIGSTOP` newest worker processes one quarter of active slots at a time (resuming oldest first as foreground load recedes, and immediately suspending all shards on battery power).

## Repository Layout

This repository contains 42 curated source, deployment, and web UI files:

```text
promptmc/
├── p1/
│   ├── server.py               # HTTP + JSON-RPC + SSE server entrypoint
│   ├── util.py                 # Time, hashing, diffing, and formatting helpers
│   ├── backends/
│   │   ├── openmc_gen.py       # Physics model -> OpenMC 0.15.3 model.xml generator
│   │   ├── openmc_read.py      # Independent XML readback parser
│   │   └── compare.py          # Bidirectional physics/binding equivalence checks
│   ├── kernel/
│   │   ├── protocol.py         # 24 JSON-RPC request and SSE notification definitions
│   │   ├── store.py            # SQLite event log & content-addressed artifact DAG
│   │   ├── runner.py           # Case execution engine, local/remote dispatch, budgets
│   │   ├── remote.py           # Coordinator whole-case remote execution bridge
│   │   ├── rules.py            # Deterministic validation rules (RC-0002..RC-0012)
│   │   ├── checks.py           # Multi-step pre-confirmation precheck gate
│   │   ├── acceptance.py       # Reference-vs-OpenMC & target k_eff verification
│   │   └── llm.py              # LLM client wrapper for chat/precheck/report prompts
│   └── nodes/
│       ├── coord.py            # Protocol v4 coordinator & CasePlan scheduler
│       ├── agent.py            # Stdlib-only worker agent with HDF5 ckpt & SIGSTOP yield
│       ├── jobs.py             # Shard splitting, OpenMC PRNG striding, tally merge
│       ├── client.py           # Token-authenticated coordinator admin client
│       └── ctl.py              # CLI operations & diagnostics (status/cancel/bench)
├── deploy/
│   ├── promptmc.service        # Workbench backend systemd unit
│   └── nodes/
│       ├── Dockerfile          # Worker container recipe (OpenMC + Python)
│       ├── pmx-coord.service   # Coordinator systemd unit
│       ├── pmx-node.service    # Linux worker node systemd unit
│       └── windows/
│           ├── controller.pyw  # Windows host tray/background yield controller
│           ├── yield_policy.py # Stateful EcoQoS & gradual suspend hysteresis
│           └── bench.py        # Multi-slot throughput benchmark utility
├── web/                        # Dependency-free ES module browser workbench UI
│   ├── index.html, app.css, app.js, cards.js, llm.js,
│   └── nodes.js, panel.js, review.js, rules.js, ui.js
├── README.md
└── CHANGELOG.md
```

## Public Mirror Notice

Proprietary solver benchmark datasets, licensed geometry packs, domain composition/parameter-extraction libraries (`domain/`, `binding/`, `intake.py`, `documents.py`, `core.py`, `report.py`), local integration test suites (`tests/`), and internal deployment/credential files have been omitted from this sanitized public code mirror.
