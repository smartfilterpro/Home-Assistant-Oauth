---
name: pii-handling
description: Rules for handling customer data, tokens, and secrets in logs, error responses, and debug output. Use this skill whenever writing an error handler, a catch block, a log line, a debug endpoint, or any response that might include user data (names, addresses, emails, device ids, OAuth tokens, API keys) — even for "internal only" tooling and temporary debugging.
---

# Customer data, tokens, and what leaves the process

This fleet has returned customers' names and shipping addresses in the
body of an unauthenticated error response, and has logged OAuth tokens in
full. Neither was a decision — both were a default (`res.json(err)`-style
handlers and `console.log(config)`-style debugging) doing what defaults
do. The rule of thumb for every byte that leaves the process: assume the
recipient is hostile and the log is public.

## Rules

1. **Error responses carry no data — only an id.** Log the full error
   server-side under a generated `errorId`, return
   `{ error: "<generic message>", errorId }` to the caller. Never
   serialize the raw error object, the request body, a database row, or a
   vendor API response into an HTTP error body. Raw `err.message` here has
   leaked carrier account internals and Postgres schema details before.

2. **PII appears in exactly one place: the endpoint that owns it,
   authenticated.** Names, addresses, emails, and phone numbers never
   appear in logs, error bodies, health/debug endpoints, or metrics
   labels. If you need to correlate a log line with a customer, log the
   opaque id (user_id, device_id), never the human fields.

3. **Tokens and keys are masked everywhere.** When a token must be logged
   for debugging, log `first4…last4` and its length, never the value.
   Grep-check before committing: no `access_token`, `refresh_token`,
   `api_key`, or `Authorization` value should ever be interpolated whole
   into a string. This includes "temporary" debug lines — those are the
   ones that ship.

4. **Debug endpoints are admin surfaces.** Any route that dumps state
   (`/debug`, `/status` with config, queue inspectors) sits behind
   `ADMIN_API_KEY`, fail-closed. A debug endpoint's audience being "just
   us" is how the unauthenticated PII endpoint happened — routes outlive
   their intent.

5. **Vendor responses are not yours to forward.** When a call to UPS,
   Ecobee, Google, etc. fails, their error body may contain account
   identifiers or echoes of what you sent. Log a truncated snippet
   server-side; forward only your own generic error to the client.

## Before finishing

Search the diff for `err.message`, `JSON.stringify(req.body)`, `token`,
and `address` reaching a `res.` or a log call. State in the PR that you
did, and what each hit resolves to. A catch block you didn't inspect is a
leak you didn't find yet.
