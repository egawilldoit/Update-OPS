# Frontend request ownership acceptance

Base: `8d243a7a8cda8a1b53ffad91a934a8b00c812c8c`.

Previously, one `checkingId` represented every tool check. Per-tool plan
counters guarded one global plan, allowing a different tool's late plan to
replace the selected plan. Check responses ignored durable probe handles.

Checks now own per-tool state and an AbortController identity. Duplicate
requests are suppressed; explicit supersession cancels the previous poll.
A pending response supplies the durable request ID. The browser reads that
request with serial 1, 2, 4, 8, 16, then 30-second intervals, honors 429
Retry-After within the existing 30-second ceiling, and waits for the
backend's applied observation. Terminal errors stop polling without changing
tool health. Completion refreshes only the checked card from GET /tools.

The selected plan workflow owns one request identity. Navigation invalidates
it before the next plan starts. Success, failure, and cleanup all check the
same identity. Unmount, external logout navigation through pagehide, and
401/403 session expiry cancel local checks and plans. There is no in-app
logout endpoint or new logout UI.

Global active jobs remain owned by their own latest request. Tool list
refreshes cannot replace a card updated after that list request began.

## Verification

Initial behavior tests ran against the accepted implementation before
correction. All three failed on their intended assertions:

- Hermes ceased showing checking when OpenCode check started.
- Late OpenCode plan replaced the successful Hermes plan.
- A pending durable probe ceased showing checking immediately.

`npm test` now runs 19 behavior tests in two files. Coverage includes
concurrent A/B checks, duplicate same-tool clicks, B-before-A completion,
old A after newer A, applied observations, probe errors, repeated pending
polls, timer and in-flight supersession, unmount, session expiry, external
logout navigation, plan success/error races, repeated plans, navigation,
active-job gating, legacy immediate responses, 429 backoff, and cancellation
while the response body is being read.

The App tests render real components and intercept HTTP responses. Hook
tests deliberately use a transport that ignores cancellation to verify
request identity independently from AbortController behavior.

Commands from `frontend/`:

```sh
npm test
npm run typecheck
npm run build
```

No lint script is configured. `git diff --check` is required. The backend
suite also checks frontend source markers; its obsolete requirement for
the variable name `planGen` was removed because the behavior is now tested
directly. No backend runtime or API contract changed.

Residual limits: network/DOM behavior is tested with jsdom, not a deployed
browser session. Probe lifecycle and observation correctness remain backend
responsibilities. VM acceptance is a separate gate.
