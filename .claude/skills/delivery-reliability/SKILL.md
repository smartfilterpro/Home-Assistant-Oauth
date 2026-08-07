---
name: delivery-reliability
description: Rules for getting thermostat/device events from this bridge into Core Ingest without loss or duplication. Use this skill whenever touching event posting, sequence numbers, retry/backoff logic, the outbound event log, batching, or anything that builds or sends payloads to /ingest/v1/events:batch — including "simple" changes to polling loops and webhook handlers that eventually emit events.
---

# Getting events to Core without losing them

Core Ingest deduplicates by high-water mark: for each device it stores the
max sequence number seen, and any event at or below it is SILENTLY
DROPPED — no error, no log on the bridge side. A bridge that resets its
counter to 1 (fresh DB, wiped volume, new deployment) stops delivering
events entirely while appearing perfectly healthy. Three bridges in this
fleet had that failure latent at once. The rules below are the pattern
that closed it, now packaged as `@smartfilterpro/bridge-core`.

## Rules

1. **Use `@smartfilterpro/bridge-core` — don't reimplement.** Sequence
   allocation, the outbound event log, payload building, JWT minting, and
   the resilient poster (circuit breaker, jittered backoff, 4xx
   short-circuit) live in the shared lib. Node bridges import it (pinned
   by commit SHA in package.json); non-Node bridges (Home Assistant/
   Python, Hubitat/Groovy) port the same semantics and say so in a
   comment. A bridge with its own bespoke retry loop is a bridge that
   will drift from Core's expectations.

2. **Seed fresh counters with epoch milliseconds, never 1.** When a
   device has no stored sequence, start at `Date.now()`, not 0 or 1. This
   guarantees a rebuilt bridge always lands above Core's high-water mark.
   Monotonic per device, allocated in the database (not in memory) so
   concurrent workers can't double-issue.

3. **Durability before POST.** Allocate the sequence number and write the
   event to the outbound event log BEFORE attempting the POST to Core. If
   the process dies mid-send, the event exists and Core's backfill
   (`GET /api/v1/backfill`) can recover the gap. Post-then-persist means
   a crash silently loses the event and the gap is invisible.

4. **Retry transports, not rejections.** Retry with jittered backoff on
   network errors and 5xx. A 4xx means Core rejected the payload — the
   payload is wrong, and resending it forever just burns quota and hides
   the bug; short-circuit, log at error level, and count it. The circuit
   breaker (5 failures → 60s cooldown → half-open probe) protects Core
   during outages; don't bypass it "just for this one path".

5. **Batches are bounded and per-device ordered.** Keep batch order
   stable per device (Core's high-water mark makes out-of-order intra-
   batch events self-drops). Cap batch size; a 10k-event catch-up batch
   that times out delivers nothing.

## Before finishing

Answer in the PR: where does the sequence number come from for a brand-new
device on a wiped database, and what happens to an event if the process
dies between building it and Core acknowledging it? The first answer must
be "epoch ms from bridge-core", the second must not be "it's gone".
