# Design notes

`orchestra` is the distilled core of a multi-agent system that ran in
production. Each component exists because of a specific, observed failure mode.
This doc records the reasoning so the guardrails aren't cargo-culted.

## The shape: orchestrator + workers, not a swarm

One controller decides what fans out, how many run at once, what tier each agent
may act at, and whether the result is good enough to ship. Workers are stateless
and disposable. This is deliberately *not* a peer-to-peer swarm: a single point
of authority is what makes permissions, cost caps, and definition-of-done
enforceable. The `Fleet` is that controller.

## Fleet — bounded concurrency is a feature, not a limit

The concurrency cap isn't about hardware; it's about **rate limits and blast
radius**. Above a handful of concurrent streams you hit provider throttling, and
a bug that spawns work in a loop should be capped, not unbounded. Fleet runs a
thread pool over the client protocol; failures are isolated onto each
`RunHandle` so one agent raising never takes down its siblings, and cost rolls up
to a real dollar figure the human can see.

## Message bus — durability is about *honesty*, not magic

The failure this prevents: a naive inter-agent RPC returns `""` on a transport
blip, which is indistinguishable from "the agent said nothing." A bus can look
perfectly alive while silently dropping every message.

The fix is to **persist before delivering** and to walk a strict status machine
(`pending → processing → done | failed | dead`). A handler that throws or returns
empty is **dead-lettered and raised**, never swallowed. An unknown recipient is
**refused**, never fabricated. The storage layer is pluggable:

- `SqliteStore` (WAL): real crash-durability, zero external services.
- `MemoryStore`: tests and embedding.

**Honest limits.** "Durable" here means *survives a process crash or transport
blip*. It does **not** mean *survives a full-machine outage* — that requires
replication (Postgres/Supabase/etc.) behind the same `Store` protocol. The
library states its guarantee rather than overselling it.

## Circuit breaker — no infinite, silent error loops

The failure this prevents (a real incident): a credential expired and every
scheduled run kept firing 401s for **13+ hours**, burning tokens, with a
liveness watchdog happily reporting the job as "running" the whole time.

A breaker gives each dependency a state machine: `threshold` consecutive
failures trip it `OPEN`; it short-circuits during a `cooldown` and alerts
**exactly once**; after cooldown it goes `HALF_OPEN` for a single trial. State
persists to JSON so it survives the process restarts that are normal for
cron-driven agents. This is generic on purpose — it is not a patch for one API.

## Best-of-N — sample, then verify

Temperature and model spread give you diverse candidates; an **adversarial LLM
judge** gives you quality. The judge must return per-candidate scores, a winner,
and a one-sentence justification as JSON — and it is never allowed to pick a
candidate that *failed to generate*. When only one candidate succeeds, no judge
call is made (don't pay for a decision with one option).

## Eval harness — "done" is a predicate, not a vibe

Two gates you run before trusting output:

- **Rubric grader.** A weighted rubric graded by a skeptical LLM judge that must
  justify each score. Malformed judge output scores 0 (fail closed), so a broken
  judge can never accidentally *pass* bad work.
- **Done gate.** An ordered list of machine-checkable invariants that must *all*
  hold. Critical invariants short-circuit (no point smoke-testing a build that
  isn't deployed); non-critical ones warn without failing. This kills the
  recurring "it should be live soon" — either every invariant holds or it is
  `NOT DONE`, by name.

## Permissions — capability vs. risk, first-class

Agents are dangerous because they act, so capability and risk are modeled
explicitly:

- **Graduated tiers** `READ < WRITE < EXTERNAL < IRREVERSIBLE`. An agent's grant
  is checked **before** any tokens are spent, so a denied action costs nothing.
- **Human-approval gate** on the irreversible tier: no approver, or an approver
  that returns `False`, means the action does not happen.
- **Hash-chained audit log**: every decision is appended with a SHA-256 hash that
  includes the previous entry's hash. Tamper with history and `verify()` returns
  `False`. This gives you an after-the-fact answer to "who did what, under whose
  authority?"

Least privilege + human-in-the-loop for irreversible actions is what bounds the
blast radius of a misbehaving agent by *policy*, not by hope.
