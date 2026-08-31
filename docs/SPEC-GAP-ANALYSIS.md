# Spec Gap Analysis

## What happened

The implementation was built from `report.html`, which turns out to be
**Part I of four** in `HyperTrace_Architecture_Dossier.pdf`:

| Part | Chapters | Content | Used? |
|---|---|---|---|
| I | 1–16 | Problem, concept, competitive landscape | Yes — `report.html` is this |
| II | 17–29 | Detailed architecture: data model, cost model, detection, policy | **No** |
| III | 30 + 14 milestones | Hands-on build guide for kind + local stack | **No** |
| IV | Appendices | Reference and viva defense | **No** |

Parts II–IV were not read. The system was built against Part I plus an
independently invented 7-phase plan.

## Where it converged anyway

The independent plan landed close to Part III's milestone list:

| Spec milestone | Built? |
|---|---|
| M1 kind cluster · M2 victim app · M3 Prometheus · M4 TimescaleDB + schema | Yes |
| M5 event bus · M6 collector · M7 cost engine · M8 behaviour engine | Yes |
| M9 policy engine · M10 executor + audit · M11 FastAPI + WebSocket · M12 React dashboard | Yes |
| M13 four demonstrations | Partly — two scenarios, not four |
| M14 measurement harness | Yes — `scripts/benchmark.sh` |

Several Part II principles were arrived at independently, which is worth
noting because they were reasoned to rather than copied:

- **Single privileged writer.** Only the executor holds write credentials,
  and §17.1's principle is verified by SubjectAccessReview tests.
- **Stable workload identity, not pod names.** §21.5 argues pod names in
  series keys manufacture unbounded cardinality. The same conclusion was
  reached the hard way — via bug 3, where remediation restarted a pod and
  orphaned the baseline that had just detected the incident.
- **Rollback as a new ledger row**, never an update to the original (§21.4).
- **Not zero-filling gaps** in baselines (§18.4).
- Both NFR budgets are met with room: agent CPU 0.07–0.22% against a 3%
  ceiling and 1% target; memory 76 MiB against 128 MiB; detection latency
  0.03 s mean against 5 s p95.

## Gaps that change behaviour

Ordered by how much they affect whether the system is correct.

### 1. No traffic telemetry — the decoupling test is not implemented

§24.1 scores five metrics; the detector's core condition is
`resource_z > 3 AND cost_z > 3 AND traffic_z < 1.0`. Without HTTP
requests/second there is no `traffic_z`, so what is implemented is a
cost-only Z-score, not the specified conjunction.

Consequences: `legitimate_traffic_growth` can never be produced, so a real
flash sale classifies as waste or a deployment bug. §18.2 lists a **Traffic
adapter** as a named ingestion service — it was not built.

This is the single largest gap and was already recorded independently as
`KNOWN-LIMITATIONS.md` §2.

### 2. No confidence score, so authority is ungated

§24.3 defines `confidence()` as a weighted sum of magnitude, decoupling,
corroboration and baseline maturity, and makes authority *a function of
confidence only*:

- ≥ 0.85 → autonomous action permitted, if policy also allows
- ≥ 0.60 → recommend, require one-click approval
- < 0.60 → alert only, never proposed as an action

The implementation has no confidence at all: a policy match acts. Every
term in the spec's function is capped so no single huge Z-score can
authorise action — a 40-sigma CPU spike with no decoupling reaches 0.40 and
stays advisory. The current system would act on it.

This also means §17.1's "degrade authority before accuracy" is unimplemented.

### 3. Acts on a single sample; spec requires a 30-second dwell

§24.2 requires **three consecutive qualifying samples** before acting,
because a single sample can be a scrape landing mid-garbage-collection. The
implementation flags and dispatches on the first qualifying sample.

### 4. Scores one metric instead of five

Specified as scored: CPU cores, memory working set, egress bytes/s, total
$/hour, HTTP requests/s. Implemented: `cost_per_hour` only. Memory is
collected but never baselined, so a leak cannot be distinguished from a
compute loop (§24.1).

### 5. Buckets in UTC, not the service's timezone

§21.1 and §18.4 both call this out: bucketing hour-of-week in UTC smears a
business-hours pattern across adjacent buckets for any service not on UTC,
"roughly halving the detector's sensitivity". The `services.timezone`
column exists in the spec for exactly this.

### 6. Policy priority is inverted

The spec's YAML says `priority: 100  # lower number wins`. The
implementation orders `priority DESC` — higher wins. Any policy set written
against the spec would evaluate in reverse order here.

### 7. `actions_log` is mutated, which the spec forbids at the database level

§21.4 makes the audit table append-only with a `BEFORE UPDATE/DELETE`
trigger that raises, plus `REVOKE UPDATE, DELETE`. The executor's
`_finalize()` **updates** the row to record its outcome. Under the spec's
schema that write would be rejected outright.

The spec's model instead carries `prior_state`, `applied_state`,
`idempotency_key UNIQUE`, `mode`, and `initiated_by` on the original row,
so the outcome is a new row rather than a mutation.

### 8. Rate limits are looser than specified

| | Spec (§17.2) | Implemented |
|---|---|---|
| Global | 10 per 5 minutes | 5 per 10 minutes |
| Per service | 1 per 15 minutes | none |
| Rollback window | 60 minutes from execution | unbounded |

The missing per-service limit is the meaningful one: one noisy workload can
currently consume the entire global budget.

## Gaps in the data model

Nine tables specified, six built. Missing entirely: `teams`, `services`,
`users`, `policy_revisions`.

`services` is the consequential one — it carries `timezone`,
`criticality_tag`, `team_id` and `requests_cpu`. Without it there is no
ownership model, no criticality beyond a namespace prefix, and no
denominator for shared-cost amortisation. `users` is hardcoded in Python.

Per-table differences:

| Table | Spec | Implemented |
|---|---|---|
| `cost_events` | Decomposed: `cpu_usd_hr`, `mem_usd_hr`, `egress_usd_hr`, `storage_usd_hr`, `shared_usd_hr`, `total_usd_hr`, `requests`, `is_estimate`; composite PK `(resource_uid, window_start)` for idempotent upsert | One `cost_per_hour`; no natural key, so reprocessing double-counts |
| `baselines` | Normalized rows keyed `(service_id, metric, bucket)`, with `variance`, `n_samples`, `weeks_seen` | Hour-of-week buckets packed into one JSONB blob per service |
| `anomalies` | `confidence`, `cost_delta_usd_hr`, `baseline_mature`, status enum of five values | No confidence or maturity; status is free text |
| `actions_log` | `idempotency_key`, `mode`, `target`, `prior_state`, `applied_state`, `initiated_by`, `rollback_deadline`, `rollback_ref` as self-FK | `rollback_ref` is a JSON string, not a reference; none of the rest |
| `policies` | `name`, `enabled`, `dry_run`, `spec` JSONB, `created_by`, plus `policy_revisions` history | No enable/dry-run/versioning/authorship |

Also absent: the `cost_hourly` continuous aggregate (§21.3), compression
and retention policies, and the deliberate 10-minute `end_offset` that
stops a financial total from decreasing as late events arrive.

## Gaps in the cost model

- **Counter vs gauge normalization** (§18.3) — "the single most common
  implementation bug". Network counters are published raw and never
  differenced. Harmless today only because cost uses CPU and memory alone.
- **Egress, storage and shared cost** — specified as cost terms, not priced.
- **Shared-cost amortizer** — cluster-level cost belonging to no workload
  is not distributed, so totals do not reconcile.
- **Unallocated service** — §18.3 requires unresolvable resources to be
  attributed to a synthetic `unallocated` service "so the total always
  reconciles". They are dropped instead.
- **Rate versioning** — prices should carry validity intervals so history
  recomputes at the rates in force then. Rates are a static ConfigMap.
- **Billing Adapter / reconciliation** — §17.1 makes "the estimate must be
  reconcilable" a principle. There is no reconciliation loop and no
  `prices` table.

## Gaps in policy semantics

The spec's policy is a YAML document with capabilities none of which exist
here:

- `dry_run` and `enabled` flags, and revision history with an author
- Business-hours awareness — *"inside business hours a human decides;
  outside them, act and report"*, so autonomy follows human availability
- `then_if_persists` / `persist_for` escalation ladders
- `limits`: `max_actions_per_hour`, `min_replicas` (never scale below
  quorum), `never: [terminate]`, `auto_rollback_after`
- `min_confidence` as a matching condition (depends on gap 2)

Current `rule_dsl` supports `classifications`, `min_cost_per_hour`,
`service_prefix` and `requires_approval` — a small subset.

## Architectural differences

- **Event bus**: spec specifies **Redpanda** (Kafka API) at M5; RabbitMQ was
  chosen instead. This one is defensible — it was an explicit decision made
  from Part I's "Kafka or RabbitMQ" framing — but it diverges, and the
  spec's consumer-group/offset semantics in §18.2 ("resumes from its
  committed offset and catches up") assume Kafka.
- **Behaviour engine**: spec is a partitioned StatefulSet holding baselines
  in memory with periodic checkpoints, recovered by replaying the bus.
  Implemented as a stateless Deployment doing a database read and write per
  message — simpler, but it does I/O on the hot path the spec budgets at
  120 ms with "no I/O".
- **Missing services**: Traffic adapter, Billing Adapter CronJob,
  Notification service (Slack/email/webhook fan-out, listed in §18.2 and
  FR-13).
- **Staleness indicators**: §18.2 requires the UI show a staleness marker
  rather than presenting a frozen number as current. Not implemented.

## Recommendation

Not all of this is worth doing, and the working system should not be
destabilised chasing completeness. In priority order:

**Worth doing — these change whether the system is correct:**

1. **Traffic telemetry** (gap 1). Without it the headline claim is
   half-measured. A `/metrics` counter on the victim workload plus a
   traffic adapter is a contained change and unlocks both the real
   decoupling condition and `legitimate_traffic_growth`.
2. **Confidence score and authority gating** (gap 2). Self-contained: one
   function in the behaviour engine, one column, one check in the decision
   engine. It is also the most defensible thing in Part II under
   questioning.
3. **Dwell time** (gap 3). Three lines, removes single-sample false
   positives.
4. **Policy priority direction** (gap 6) and **per-service rate limit**
   (gap 8). Both small, both currently wrong rather than merely absent.
5. **`actions_log` append-only** (gap 7). Either adopt the spec's
   `prior_state`/`applied_state` columns or stop updating the row. The
   current design contradicts a stated NFR.

**Worth doing if there is time:** the `services` dimension table (unlocks
timezone bucketing, criticality tags and ownership), multi-metric scoring,
and the continuous aggregate.

**Probably not worth doing now:** Redpanda migration, StatefulSet
checkpointing, shared-cost amortisation, full billing reconciliation. These
are large, and the current substitutes work and are tested.

**Whatever is not closed should be stated**, in `KNOWN-LIMITATIONS.md` and
in the defense — the spec's own §31.3 is titled "the honest limitations to
state before you are asked", which is the same posture.
