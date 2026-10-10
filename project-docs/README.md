# Muse Job Assistant

A small repository for a personal job search operated through Muse.

Muse finds and reviews roles using repository tools, presents matches for your selection, then uses its available browser tools to complete approved applications. The repository keeps configuration, discovery results, approvals, and outcomes between runs.

## Intended daily flow

1. Ask Muse to find entry-level/new-grad roles matching your location and eligibility preferences.
2. Review one deduplicated list with links, fit explanations, and issues that need your attention.
3. Approve specific roles to apply to, or skip/defer them.
4. Muse applies to approved roles with your single resume and confirmed facts. It pauses individual applications for missing answers, suspicious instructions, human-only requirements, login, or CAPTCHA.
5. Review results and export the tracker to CSV.

Role approval is required before candidate details are entered or uploaded. There is no additional project-required final confirmation for an ordinary approved application; Muse's own required confirmations still apply.

## Development status

The repository includes portable discovery, review, preflight, and outcome-tracking helpers. Read [PROGRESS](docs/PROGRESS.md) for observed status and [TASKS](docs/TASKS.md) for remaining work. Muse has verified bounded discovery and public-page inspection; controlled-form behavior and cross-session persistence remain unverified.

To develop an existing repository, start with [START_HERE](START_HERE.md) and the [coding-agent prompt](docs/PROMPTS.md). Reuse working discovery/tracking code. No new dashboard, Windows sandbox, custom model service, or browser automation framework is needed.

## Use with Muse

Make the repository accessible through a supported route and explicitly ask Muse to read [MUSE_GUIDE](docs/MUSE_GUIDE.md). Repository access, code execution, browser/file-upload support, and persistence must be checked in your actual Muse session. No specific upload route or platform integration is assumed.

The guide contains tested local commands, private state paths, and a sanitized Muse handoff. Preflight and outcome records are advisory workflow checks, not a security boundary over Muse's browser tools. Complete the controlled-form checks before any live application.

Once configured, privately provide one resume and confirm the candidate profile, search preferences, reusable answers, and permission to draft ordinary narrative answers. Do not infer candidate details from other projects or unrelated conversation history.

## Project boundaries

Job pages are untrusted input. Instructions inside them cannot authorize applications or change your facts. Legitimate human-only or no-AI application requirements are handed back to you. These precautions reduce risk but do not make an agent immune to prompt injection.

A failed or uncertain submit is not retried automatically. The tracker records confirmed, blocked, manually reported, and uncertain outcomes distinctly.

Private resume/profile/tracker files stay out of Git. Git exclusions prevent accidental commits; approved applications intentionally transmit selected candidate data to employers through Muse's tools.