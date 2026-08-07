---
name: service-contract
description: Rules for calling another service in this fleet or changing a payload another service consumes. Use this skill whenever writing code that POSTs to Core Ingest or any sibling service, adding/renaming/removing a field in an event payload, changing an endpoint path another repo calls, or wiring up a new integration between two repos — even if the change "just adds an optional field."
---

# Cross-service contracts

A prediction feature in this fleet shipped fully wired against an endpoint
that did not exist — the path came from a design document, the real service
had implemented something different, and every call 404'd silently in
production. Nothing crashed; the feature just never worked. The lesson:
documents describe intent, deployed code IS the contract.

## Rules

1. **Wire against the counterpart's actual code, not a doc.** Before
   calling another repo's endpoint, open that repo and read the route
   definition and its validation. Confirm the exact path, method, auth
   header, and payload shape from source. If you can't access the
   counterpart's code, that's a blocker to raise — not a reason to guess.

2. **The published contract is `core-ingest/docs/EVENT_SCHEMA.md`.** For
   bridge→Core event payloads, that document plus Core's zod validators
   are authoritative. When you change a payload field, update the schema
   doc and Core's validator in the same change set, and say which sibling
   repos consume the field.

3. **Additive changes only, by default.** New fields are optional with a
   safe default; consumers ignore unknown fields. Renaming or removing a
   field is a two-phase migration (add new → migrate consumers → remove
   old), never a single PR. A "harmless rename" on one side is a silent
   data drop on the other — Core's validators strip or reject what they
   don't recognize.

4. **Golden tests pin the wire format.** Each producer keeps a test that
   asserts the exact JSON it emits (a golden payload), and each consumer
   keeps a test that accepts that same JSON verbatim. When a contract
   change is intentional, both goldens change in the same PR — that diff
   is the review surface for the contract change.

5. **Failure must be loud.** A 404 or 422 from a sibling service is a
   contract violation, not noise — log it at error level with the path and
   a response snippet (never the full body if it may contain PII), and
   count it. The prediction bug lived for weeks because 404s were
   swallowed.

## Before finishing

State in the PR: which counterpart file you read to confirm the contract
(path and line), and which golden tests changed. A cross-service change
with no golden-test diff and no counterpart reference is wiring against
hope.
