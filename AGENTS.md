# AGENTS.md

These instructions apply to Codex and every automated coding agent working in this repository.

## Non-negotiable safety boundary

1. **Paper trading only.** Never add, enable, simulate as real, or invoke live-order functionality.
2. Never add wallet signing, private-key loading, authenticated Polymarket order APIs, deposits, withdrawals, approvals, or on-chain transaction submission.
3. Never expose secrets in source, logs, test output, reports, commits, issues, prompts, or documentation. Do not print credential values while debugging.
4. Treat `.env`, credentials, cookies, API tokens, private keys, wallet files, mnemonics, SSH keys, and runtime state as outside Git.
5. Do not start, stop, restart, pause, resume, replace, or edit the host scheduler unless the user gives explicit operational authorization for that exact action.
6. Do not run a scheduled entrypoint manually while its scheduler is enabled; duplicate execution can race append-only evidence and mutable state.

## Strategy integrity

1. Do not change strategy thresholds, entry/exit rules, quantities, fees, latency assumptions, filters, cluster definitions, or promotion gates without explicit authorization and evidence.
2. Never tune a strategy against the current runtime reports and then claim the same sample is validation.
3. The X v4 protocol is locked. A substantive change requires a new protocol version and a new forward sample; never amend v4 in place.
4. Preserve paper/no-trade/evidence-failure/partial-fill/residual-position records. Never survivorship-filter generated evidence.
5. Preserve causal timing, exact token/condition identity, displayed depth, fees, settlement proof, and raw hashes.
6. Keep interim X arm P&L hidden until the registered endpoint. Do not add ranking or optional-stopping output.

## Data and replay

1. Treat `data/` and `reports/` as **read-only production runtime data** unless the task explicitly concerns a controlled runtime operation.
2. Do not delete, rewrite, normalize, relocate, or commit runtime evidence.
3. Keep source code separate from generated reports, captured order books, JSONL chains, databases, and raw archives.
4. Preserve deterministic replay and append-only hash-chain semantics.
5. Tests must use temporary directories or checked-in fixtures, never live paths.
6. Do not globally ignore fixture extensions. Small deterministic fixtures under `tests/fixtures/`, `testdata/`, or `fixtures/` belong in Git.
7. A full replay must verify integrity before calculating any economics. Later marks must never substitute for decision-time entry or exit books.

## Development workflow

1. Read `HANDOFF.md` and `deploy/runtime-manifest.md` before editing operational code.
2. Create a branch for every change. Do not work directly on `main`.
3. Keep changes narrowly scoped. Separate migration/packaging work from strategy work.
4. Run the relevant self-tests and unit tests before committing.
5. Run `python tools/secret_scan.py --git-candidates` before staging and again against staged files before committing.
6. Review `git diff --check`, `git status --short`, and the exact staged diff before every commit.
7. Never use `git add .` until the secret scan and ignore-boundary review are clean. Prefer explicit paths.
8. Do not add a remote or push without explicit authorization.
9. Do not rewrite or import the nested `repos/polymarket12` Git history into this repository.

## Required verification baseline

From the repository root:

```bash
python -m unittest -v \
  test_both_sides_spike.py \
  test_event_evidence_pipeline.py \
  test_strategy_evidence_integration.py \
  test_xtracker_forward_capture.py \
  test_xtracker_forward_v4.py
python stock_price_paper_bot.py --self-test
python multi_market_research_bot.py --self-test
python public_record_reaction_bot.py --self-test
python tools/secret_scan.py --git-candidates
```

`test_both_sides_collector_supervisor.py` is a host integration test and currently depends on `/data/scripts/both_sides_collector_supervisor.py`.

## Review checklist

Before proposing a commit, confirm all of the following:

- Paper-only assertions remain true.
- No live order/auth/wallet path was introduced.
- No strategy constant changed unintentionally.
- Runtime paths are ignored and untracked.
- Deterministic tests pass.
- No secret scanner finding is unresolved.
- No generated file or large capture is staged.
- The change is on a branch.
- No remote push was performed.
