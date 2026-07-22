# X v4 Forensic Closeout and v5 Candidate

This document describes a source-only candidate. It does not authorize or record
an activation, deployment, scheduler change, wrapper installation, or runtime
evidence migration.

## V4 classification

X forward v4 is mixed-version forensic evidence, not one homogeneous promotion
experiment. Its protocol JSON identity remained constant while the locked
implementation identity changed under the same activation timestamp.

Confirmed audit facts:

- active lock: `6daadee95492e6b1cbedfe11af1f4ceaa209222ede21d858883d1e66c084442d`;
- original lock: `497bee78a3eeeb994747014299db2959d612a6a29f9473a59e6e714e4f438ef7`;
- rows 1-33 used the original lock and rows 34-39 used the active lock;
- the displayed completed cluster per arm came from sequence 4 under the original lock;
- active-lock-only completed clusters were zero per arm at audit time;
- no v4 row may be rewritten, deleted, relabeled, filtered, re-hashed, or copied into v5.

`verify_xtracker_forward_v4.py` now fails explicitly when ledger, registry,
event, entry, completion, state, or status provenance is missing, foreign, or
ambiguous. It never turns a mixed ledger into a passing subset. Its production
CLI still defaults to `/data/workspace/polymarket-research` and writes the audit
there; tests use imported no-write functions and temporary data only.

## V5 boundary

The committed v5 artifact is an unactivated template. A future separately
authorized activation must generate:

- a new `xtracker_forward_v5_<UTC timestamp>` protocol ID;
- a fresh activation timestamp that is not the v4 timestamp;
- a protocol hash and complete self-hashed lock over exact source bytes;
- `reports/xtracker_forward_validation/v5` as a new append-only tree;
- empty registry and event chains;
- a header-only independent ledger;
- zero opportunities, positions, completions, and promotion counts;
- paper-only status with zero live orders and no wallet or authentication use.

The activation refuses any existing v5 target, requires the literal confirmation
`CREATE_EMPTY_XTRACKER_V5`, verifies that the economic sections equal v4, and
never reads rows from v3 or v4 into v5. It also requires an existing absolute
deployment-wrapper path and binds that exact external file hash into the lock;
this repository does not create or install the wrapper. Do not run activation
until a separate operational review authorizes the exact source SHA, wrapper,
scheduler transition, and timestamp.

## Provenance model

The complete experiment identity is:

```text
protocol_id
protocol_sha256
baseline_lock_sha256
experiment_identity_sha256
```

The identity is included in registry and event hash-chain bodies, ledger rows,
mutable state projections, completed-cluster projections, status summaries, and
the activation manifest. Ledger rows link to the exact registry record hash.
Eligible entries link to that registry record. A completion must link to the
exact entry record and registry record; lifecycle ID alone is insufficient.

The v5 engine adapter reuses the unchanged hardened capture and monitor modules.
It changes no threshold, fee, latency, fill, exit, settlement, quantity, arm, or
promotion rule. It adds provenance before append/write operations and retains the
existing single non-blocking process lock around capture, enrichment, and monitor
execution.

## Review and activation gates

Before any activation, an independent review must verify the candidate tests,
all locked source hashes, the future deployment wrapper, scheduler cutover,
empty v5 target, disk/runtime backups, and a coordinated stop of v4 collection.
Activation, deployment, scheduler changes, CI, branch protection, and any merge
remain separate explicit approvals.
