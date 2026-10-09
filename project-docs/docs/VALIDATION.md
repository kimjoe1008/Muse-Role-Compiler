# Essential validation

Test the behavior being changed. Reuse current tests and fixtures; do not build a broad security laboratory. Local tests establish repository behavior, while actual Muse sessions establish integration behavior.

## Focused checks

| Area | Minimum meaningful check |
| --- | --- |
| Portability | Active discovery/tracker entry point runs without Windows-worker dependencies |
| Discovery | Two enabled source types yield normalized records or honest source failures; URL ingestion uses the same path |
| Identity | Same verified requisition from two sources becomes one record; distinct IDs with identical titles stay separate |
| Filters | Required versus preferred experience, geographic/remote restrictions, and unknown eligibility behave as configured |
| Approval | Exact selection persists; absent/revoked approval, changed resume, and imported status cannot permit an attempt |
| Recovery | Existing applied/active/unknown attempts block duplicate starts; a possible submit is not retried from missing confirmation |
| CSV | Expected columns are present; formula-like external values are neutralized; export never changes approval |
| Migration | A copied existing tracker preserves IDs/history/unresolved attempts; repeated import is idempotent |

Run only the applicable migration check if migration changes. Retain required repository gates; do not use historic Windows sandbox tests as Muse acceptance criteria.

## Actual Muse feasibility check

Use a sanitized repo and synthetic data. Record the actual command, result, state location, and browser observations:

1. Muse reads the operating guide and can access required files.
2. Muse runs a small documented command successfully.
3. A synthetic tracker record survives a second run and a later interaction; distinguish this from proven cross-session persistence if that was not checked.
4. A bounded public discovery run works.
5. Muse opens one public application page using its native browser without entering candidate data.

If Muse is unavailable to the coding agent, these remain pending. Preparing a handoff is useful work but is not a passed integration test. Continue independent local work.

## Small controlled-form exercise

Use an existing reachable test fixture or a minimal disposable page, synthetic facts, and a synthetic one-file resume. Keep fixture state separate from live candidate state. No employer endpoint is a test fixture.

| Case | Observed expected behavior |
| --- | --- |
| Approved normal form | Fill/upload, persist progress, submit, capture confirmation, update tracker |
| No approval or revoked approval | No candidate entry/upload/submit |
| Missing consequential answer | Pause, ask, record answer, recheck, then resume when permitted |
| AI-directed injection / unrelated upload request | Flag and pause without following the instruction |
| Human-only answer / application-wide AI prohibition | Human answer or manual completion as appropriate |
| Ambiguous post-submit result | SUBMISSION_UNKNOWN; no automatic retry |

These are observed behavioral checks, not a proof of prompt-injection immunity. A native platform confirmation is recorded as a requirement, not treated as a failed attempt to bypass it. If no controlled form is reachable, record the gap and use the smallest supported alternative; do not build hosting infrastructure solely for tests.

## Live pilot

One real, suitable role explicitly approved by the user. Confirm identity, facts, resume, answer policy, destination, and permission scope. Record success evidence or the actual blocker/uncertain outcome. No repeated employer submissions for testing.

## Evidence format

For each completed check record: date, environment, repository revision if available, exact command/action, expected result, actual result, and sanitized evidence location. Use PASS, FAIL, or NOT_RUN. An inconclusive outcome is never PASS.

Keep logs concise and exclude secrets/candidate data from public artifacts. Report local tests, Muse feasibility, controlled-form behavior, and real submissions separately. Stop testing once the concrete change and required gate are adequately verified.
