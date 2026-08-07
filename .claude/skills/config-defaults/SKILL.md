---
name: config-defaults
description: Rules for environment variables, config values, and secrets. Use this skill whenever adding or changing an env var, a config default, a hardcoded URL or API base, a fallback value, or anything involving secrets/tokens/keys — including "temporary" defaults and test-environment switches. Also use it when writing code that reads process.env or a config module.
---

# Config, env vars, and secrets

The same bug shipped three separate times in this fleet: a hardcoded
default pointing at Bubble's **test** database (`/version-test/`), so
production silently ran against test data whenever the env var wasn't set.
It was found in the Home Assistant integration, the React app, and nearly
in a fourth service. Defaults are production behavior — treat them that way.

## Rules

1. **Defaults must be production-safe.** A fallback URL points at LIVE, a
   fallback flag picks the safe branch. If a test environment is a
   legitimate target, it is an explicit opt-in (an env var or a UI toggle
   defaulting to live) — never the silent default. When in doubt, no
   default at all: warn loudly at startup and refuse the risky behavior.

2. **Every new variable lands in the checklist.** The fleet env inventory
   is `core-ingest/docs/ENV_VARIABLES_CHECKLIST.md`. Add the variable, its
   default, and whether it is required or tuning. A variable that only
   exists in code is a variable nobody will set.

3. **Secrets never travel in URLs.** Not in query strings, not embedded in
   git clone URLs (they persist in `.git/config`), not in log lines. If a
   third-party API demands a key in the URL (some do), note the exposure in
   a comment and keep that URL out of error objects and logs.

4. **Unset-secret behavior is deliberate, per surface class:**
   - Admin/operator surfaces: fail CLOSED (503) — no availability excuse.
   - Availability-critical paths (webhooks receiving device events): warn
     loudly at startup, run unverified, and gate enforcement behind a flag
     (`*_ENFORCE=true` / `*_VERIFY=true`) so a deploy can't silently drop
     events. Flipping the flag is a tracked follow-up, not an afterthought.

5. **Two-unit timestamps are banned.** `expires_at` has existed in this
   fleet as both epoch-seconds and epoch-milliseconds simultaneously. New
   fields carry the unit in the name (`expires_at_ms`) or normalize on
   read (values > 10^10 are milliseconds — divide). See
   `core-ingest/docs/CONVENTIONS.md`.

6. **Build-time vs runtime.** For CRA/frontend builds, `REACT_APP_*` is
   inlined at BUILD time — changing the platform env var does nothing
   without a rebuild. Say so in the PR whenever touching one.

## Before finishing

State in the commit/PR description: every var added or changed, its
default, and what happens when it is unset. If you can't answer "what
happens when it's unset" you haven't finished the change.
