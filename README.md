# EGA Update Console (Update-OPS V1)

Private single-owner web console to manually update Hermes, OpenCode, Codex,
and T3 nightly on one Ubuntu 22.04 ARM64 VM behind Cloudflare Access + Tunnel.

- Frontend: React 19.2 + Vite 8.1 + TypeScript 7.0 (`frontend/`)
- API: FastAPI + Pydantic 2, serves compiled frontend + `/api/v1` (`backend/app/`)
- State: SQLite WAL + per-job redacted JSONL logs
- Worker: singleton systemd dispatcher + `systemd-run` job runner
- Scripts: thin `scripts/agent-update` dispatcher + `scripts/update-*.sh` wrappers
- Deploy: `systemd/`, `deploy/`, `docs/RUNBOOK.md`

Canonical requirements: `DOC/PRD-UPDATE SYSTEM.md`, `DOC/SPEC-UPDATE SYSTEM.md`,
`DOC/spec_scripts.md`. Implementation contracts: `docs/CONTRACTS.md`.
Dependency provenance: `docs/DEPENDENCIES.md`.

Implementation phase only: health checks and safeguards are implemented in
code but never executed here; runtime acceptance is **not executed, per
instruction**. See the handoff commit for the AC/SC mapping.
