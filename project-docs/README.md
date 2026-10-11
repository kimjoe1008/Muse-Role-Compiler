# Muse Job Assistant

A discovery-first workspace for finding, evaluating, deduplicating, and tracking roles with Muse.

Muse searches and reviews roles using repository tools, presents new and materially changed matches, and keeps them distinct from roles already saved or applied to. The repository keeps source provenance, preferences, review decisions, and outcomes between runs. Application assistance is a separate opt-in workflow.

## Intended daily flow

1. Configure role families, locations, seniority, and eligibility preferences.
2. Search direct ATS boards and other supported or manually inspected sources, with scan limits and failures reported.
3. Review a deduplicated list of new or changed roles, with links, evidence, and blockers.
4. Save, skip, or defer exact roles, and keep past outcomes in the tracker to reduce repeat results.
5. Export the maintained tracker. Separately request application help only for exact roles you choose.

Role approval is required before candidate details are entered or uploaded. There is no additional project-required final confirmation for an ordinary approved application; Muse's own required confirmations still apply.

## Development status

The repository includes portable Greenhouse/Lever discovery, review, preflight, and outcome-tracking helpers. CSV history import and broader source coverage are planned, not implemented. Read [PROGRESS](docs/PROGRESS.md) for observed status and [TASKS](docs/TASKS.md) for the current discovery-first priorities.

To develop an existing repository, start with [START_HERE](START_HERE.md) and the [coding-agent prompt](docs/PROMPTS.md). Reuse working discovery/tracking code. No new dashboard, Windows sandbox, custom model service, or browser automation framework is needed.

## Use with Muse

Make the repository accessible through a supported route and explicitly ask Muse to read [MUSE_GUIDE](docs/MUSE_GUIDE.md). Repository access, code execution, browser/file-upload support, and persistence must be checked in your actual Muse session. No specific upload route or platform integration is assumed.

The guide contains tested local commands, private state paths, and a sanitized Muse handoff. Preflight and outcome records are advisory workflow checks, not a security boundary over Muse's browser tools. Complete the controlled-form checks before any live application.

Once configured, privately provide one resume and confirm the candidate profile, search preferences, reusable answers, and permission to draft ordinary narrative answers. Do not infer candidate details from other projects or unrelated conversation history.

## Project boundaries

Job pages are untrusted input. Instructions inside them cannot authorize applications or change your facts. Legitimate human-only or no-AI application requirements are handed back to you. These precautions reduce risk but do not make an agent immune to prompt injection.

A failed or uncertain submit is not retried automatically. The tracker records confirmed, blocked, manually reported, and uncertain outcomes distinctly.

Private resume/profile/tracker files stay out of Git. Git exclusions prevent accidental commits; approved applications intentionally transmit selected candidate data to employers through Muse's tools.