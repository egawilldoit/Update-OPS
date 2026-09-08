# Dependency pins and provenance (verified 2026-09-08)

Baseline: Ubuntu 22.04 ARM64, Node 24.18.0, npm 11.19.1 (reused, never
installed/upgraded by this project). Python 3.10 in a dedicated app venv.

## Frontend (npm, exact pins in `frontend/package.json`)

| Package | Pinned | Source |
| --- | --- | --- |
| react | 19.2.8 | npm `react` latest + GitHub react/react 19.2.8 (2026-07-21) |
| react-dom | 19.2.8 | paired with react 19.2.8 |
| @types/react | 19.2.18 | npm latest tag (2026-07-30) |
| @types/react-dom | 19.2.7 | npm release history (2026-09-03) |
| vite | 8.1.5 | latest 8.1 patch (2026-07-16); 8.2.x is newer but baseline targets 8.1 |
| @vitejs/plugin-react | 6.1.1 | GitHub vite-plugin-react 6.1.1 (2026-08-28); Vite 8 compatible |
| typescript | 7.0.2 | npm + GitHub microsoft/TypeScript v7.0.2 (2026-08-20); Go-native, 10x faster |

Notes: TS 7.0 has no stable programmatic API (stable API lands in 7.1), so
`tsc --noEmit` is used for type-checking only; Vite build does not depend on
the TS compiler API. ESLint/typescript-eslint stacks stay out of V1.

## Backend (PyPI, exact pins in `backend/requirements.txt`)

| Package | Pinned | Source |
| --- | --- | --- |
| fastapi | 0.136.1 | FastAPI release notes, latest stable cited 2026-04-23 |
| pydantic | 2.13.5 | PyPI release history (2026-08-28) |
| pydantic-settings | 2.15.0 | PyPI (2026-08-07) |
| uvicorn | 0.52.4 | PyPI (2026-08-19) |
| PyJWT | 2.13.0 | PyPI + jpadilla/pyjwt security release (2026-05-21); RS256 via `cryptography` |
| requests | 2.32.5 | transitive JWKS fetch client (pinned; refresh at deploy if superseded) |

`cryptography` and `starlette` resolve transitively via `PyJWT[crypto]` and
`fastapi`; their exact versions are recorded by `pip freeze` at release time,
not invented here.

## Deferred

No `package-lock.json` / `pip freeze` contents are invented. Generation:
`npm install --package-lock-only` + `pip freeze` in the deploy environment
(see `backend/requirements.pinned.txt`). Runtime acceptance not executed in
this phase, per instruction.
