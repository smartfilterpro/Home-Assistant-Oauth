---
name: new-endpoint
description: Security checklist for HTTP surface changes. Use this skill whenever adding, modifying, or mounting ANY HTTP route, webhook handler, Express router, or API endpoint — including "quick" debug endpoints, health checks, internal tools, and webhook receivers — even if the request doesn't mention security. Also use it when changing auth middleware or API-key handling.
---

# New or changed HTTP endpoints

Every route in this fleet declares its auth class explicitly, before the
handler is written. The 2026-08 security review found the same omission
independently in four services (an unauthenticated customer-PII endpoint,
eleven open AI-service routes, an unauthenticated device webhook, and a
fail-open admin dashboard) — not because anyone decided those routes should
be open, but because nothing forced the question at write time. This skill
forces the question.

## Step 1: Pick the auth class — there is no default

Every new or modified route gets exactly one of these, stated in a comment
above the mount:

| Class | Middleware pattern | Used for |
|---|---|---|
| Machine key | `CORE_API_KEY`-style shared secret, timing-safe | Service↔service calls (Core backfill, Bubble integration pushes) |
| Admin key | `ADMIN_API_KEY`, fail-closed | Operator/debug/management surfaces |
| Signed webhook | Vendor signature verification (Pub/Sub OIDC, SmartThings HTTP-Signature) | Inbound vendor events |
| Service JWT | HS256 via SERVICE_JWT_SECRET, scoped | Bridge→Core ingest |
| Public | Comment explaining WHY it is safe open | `/health` and almost nothing else |

"Public" requires a written justification in the code. `/health` is public
because it leaks nothing and load balancers need it; that is the bar.

## Step 2: Implementation rules (each one earned by a real incident)

- **Fail closed when the secret is unset.** Admin/integration surfaces
  return 503 with a clear `*_not_configured` error, never pass-through.
  The ops-review dashboard passed every request as super-admin for months
  because unset Azure config meant "skip auth".
- **Timing-safe comparison, always.** Hash both sides with sha256, then
  `crypto.timingSafeEqual`. Plain `===` leaks length and content timing.
- **Headers only — never accept credentials via query string.** Query
  strings land in access logs, proxy logs, and Referer headers. Accept
  `Authorization: Bearer <key>` and `x-api-key`.
- **Generic error bodies.** Log the full error server-side under a
  request-scoped error id; return `{ error: "...", errorId }`. Raw
  `err.message` has leaked UPS account internals and Postgres schema
  details to callers here before.
- **Webhook receivers verify the sender.** New vendor webhooks implement
  the vendor's signature scheme with a warn-only rollout flag (enforce
  when `<VENDOR>_WEBHOOK_VERIFY=true`) so deployment can't brick event
  delivery. An unauthenticated webhook is an event-injection path into
  Core under this bridge's own service JWT.

## Step 3: Prove it

Before finishing, list every route the change added or touched and its
auth class in the PR/commit description. If any route is "Public", the
justification sentence goes there too. A route you can't classify is a
route that isn't done.
