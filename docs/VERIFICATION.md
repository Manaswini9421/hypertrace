# Verification Record

What was actually exercised against a live `kind` cluster, with the
evidence. Anything not listed here should be treated as unverified.

## Environment

Local `kind` cluster `hypertrace` (1 control-plane + 2 workers, Kubernetes
v1.31.0) on Docker Desktop. All 10 services deployed into the `hypertrace`
namespace.

## Pipeline (Phases 1–3)

| What | Evidence |
|---|---|
| Collector publishes metrics | Both DaemonSet pods logged `published metrics for node=... (N pods)` every ~10s; a sampled RabbitMQ message matched the `MetricEvent` schema exactly |
| Prometheus scrapes healthy | All targets `up` (`kubernetes-nodes-cadvisor` ×3, `kube-state-metrics`) |
| Grafana serves live data | Queried through Grafana's own datasource proxy — returned per-pod CPU/memory for every running pod |
| Schema applied | All 6 tables from dossier 5.2 created on first boot |
| Cost computed live | `cost-intelligence` logged `$/hour` per workload; `cost_events` grew continuously |
| Baseline + detection | Injected a CPU burn; detector flagged it at **z = 11.09 / 15.80** against a ~$0.00007/hr idle baseline, persisted to `anomalies` |
| No false positives at rest | A steady-state cluster produced no anomalies over multiple quiet minutes |

## Decision and remediation (Phase 4)

| What | Evidence |
|---|---|
| Policy-gated autonomous throttle | Incident → `decision-policy` dispatched `throttle` → executor patched the Deployment; CPU limit went **1 → 100m** |
| Idempotency | Repeat dispatches for an already-throttled workload returned `no_op: already throttled` — no duplicate patch, rollback reference preserved |
| Rollback (FR-11) | Restored CPU limit **100m → 1**; `actions_log` shows `throttle/executed` and `rollback/rolled_back` as two distinct append-only rows |
| Protected floor (doc 11.3) | Injected a remediation request targeting `kube-system/kube-proxy` directly onto the queue; executor **refused** (`blocked_by_protected_floor`) and `kube-proxy` was confirmed unmodified |
| RBAC (FR-14) | `finance` role got **403** creating a policy; `sre` succeeded |
| Least privilege (NFR-4) | Executor runs under a namespace-scoped Role limited to `deployments: get,patch` and `horizontalpodautoscalers: get,list,patch` — it cannot delete pods or read secrets |

## Dashboard (Phase 5)

Driven through a real browser:

- Login, and correct rejection of a wrong password
- Live cost ticker, ranked service list, and a populated time-series chart
- WebSocket alert stream connected (`101` upgrade for a valid token, `403` for a bad one) with anomalies arriving live
- Classification badges rendering, including `likely_bug_from_deployment` correctly firing for pods restarted moments earlier
- **Rollback triggered from the UI** — verified end-to-end down to the restored CPU limit (`throttle → executed` followed by `rollback → rolled_back`, and the Deployment's CPU limit back at `1`)
- **Recent / Needs action** filter, added after the actionable incident proved unreachable in the default feed (bug 7)

## The decoupling test

The conjunction from §24.2, verified live against the real workload with
real traffic from the load generator:

| What | Evidence |
|---|---|
| Traffic pipeline | `traffic-adapter` publishes `hypertrace/victim` at ~4.7 req/s, matching the load generator's configured rate |
| Three metrics baselined | `cost_per_hour`, `cpu_cores` and `requests_per_second` rows in `baselines` |
| Dwell before acting | Observed `dwell=1/3` → `2/3` → ANOMALY; a single sample never flags |
| Baseline not poisoned during dwell | Sample count held at `n=14` across the dwell window while `cost_z` stayed at ~986 instead of decaying |
| Decoupling | Incident flagged at `cost_z=986`, `traffic_z=-3.08` — cost up, traffic flat |
| Authority gated | Confidence 0.70 → approval band, so the workload was **not** touched; the ledger shows `pending_approval` and the CPU limit stayed at 1 |

## Joint reasoning (Phase 6)

The differentiating claim (dossier 9.1). The **same** cost spike classifies
differently depending on corroborating signals:

| Condition | Classification | Evidence recorded |
|---|---|---|
| Cost spike alone | `misconfiguration_or_waste` | `{}` |
| Cost spike shortly after a deploy | `likely_bug_from_deployment` | `{"recent_deployment": true}` |
| Cost spike + security signal | `suspected_abuse` | `{"security_rule": "unexpected_outbound_connection"}` |

All three were observed live. Caveat: the security signal is injected —
see `KNOWN-LIMITATIONS.md` §3.

## Measured NFRs (Phase 7)

Via `scripts/benchmark.sh`:

| Target | Measured | Result |
|---|---|---|
| NFR-2 collector CPU ≤ 3%/node | **0.218%** of one core (79.8 MiB) | Pass |
| NFR-1 detection latency ≤ 5s p95 | **0.02s** mean, **0.08s** worst over 148 anomalies | Pass |

The latency figure measures cost-sample → anomaly-flag. End-to-end
detection additionally carries the collector's 10s poll interval, so
real-world time-to-detect is ~10s, still inside target.

## Automated tests

```bash
pip install -r requirements-dev.txt
pytest -q                      # 164 backend tests
npm test --prefix frontend     # 49 frontend tests
```

**213 tests: 89 backend unit + 75 integration + 49 frontend.**

### Unit (`tests/`, 89) — no cluster required

| File | Tests | Covers |
|---|---|---|
| `test_detection.py` | 11 | Welford's algorithm against `statistics` ground truth, serialisation round-trips, the zero-variance guard, the baseline-suppression regression (bug 4) |
| `test_policy.py` | 9 | Policy matching and its boundaries, the protected floor |
| `test_messaging.py` | 4 | The publish reconnect path (bug 5) |
| `test_remediation.py` | 17 | Throttle/freeze-scaling/rollback against a fake Kubernetes client: what gets patched, idempotency, rollback-reference round trip, and every refusal path |
| `test_confidence.py` | 25 | The confidence function and its caps, the authority thresholds, the detection conjunction (cost up / traffic flat vs cost up / traffic up), and classification precedence |
| `test_collector.py` | 23 | Kubelet payload parsing (bug 2), nanocore conversion, multi-container aggregation, workload resolution and caching (bug 3), lifecycle-event mapping, and the published MetricEvent shape |

These run against the real `hypertrace_common` package with only the
Kubernetes client stubbed. `make test` runs just these.

### Integration (`tests/integration/`, 75) — drives the live cluster

| File | Tests | Covers |
|---|---|---|
| `test_messaging_roundtrip.py` | 4 | Real broker: MetricEvent round-trip, routing-key isolation, reconnect after a dropped connection, and `consume()` itself |
| `test_database.py` | 10 | Real TimescaleDB: schema matches the services, hypertables exist, JSONB evidence round-trips, baseline upsert survives restart, audit-log lifecycle, FK rejects orphan actions |
| `test_api_contract.py` | 21 | Real api-bff: auth, forged tokens, response shapes, **RBAC 403s**, policy CRUD, remediation guards, WebSocket handshake auth |
| `test_pipeline_e2e.py` | 4 | **Across service boundaries:** publish metrics → deployed cost-intelligence prices them → deployed behaviour-analysis flags the spike |
| `test_remediation_k8s.py` | 14 | **Real cluster writes:** throttle patches a real Deployment, rollback restores it exactly, idempotency holds; plus **RBAC verified via SubjectAccessReview** and the rate-limit window |
| `test_collector_k8s.py` | 18 | **Real kubelet:** the stats payload parses (bug 2's definitive guard), metrics are plausible, real Pod→ReplicaSet→Deployment and DaemonSet ownership resolves, replicas agree on identity, and the collector has **no write access anywhere** |

The remediation tests operate on a **disposable Deployment the test creates
and deletes itself**, never on the demo workloads, which is what makes them
safe to run against the live cluster.

The RBAC checks ask the API server what the executor's ServiceAccount may
actually do, rather than trusting the YAML. They assert it *can* get and
patch Deployments, and *cannot* delete pods, delete Deployments, read
secrets, create pods, patch nodes, or touch `kube-system` — the concrete
evidence behind defense-kit Q4.

`make integration` opens the required port-forwards and tears them down.
Without them the suite **skips rather than fails** (41 skipped), since a
missing port-forward is an environment gap, not a regression. The 30 tests
that still run are the Kubernetes ones, which use the local kubeconfig
directly rather than a forwarded port. Everything is
written under an `itest-` prefix and cleaned up, so it is safe to run
against the live demo cluster.

### Frontend (`frontend/src/**/*.test.tsx`, 49) — vitest + jsdom

| File | Tests | Covers |
|---|---|---|
| `lib/api.test.ts` | 12 | Token storage, role extraction from the JWT, bearer headers, service-name URL encoding, the 401 session-clear, and the WebSocket token-in-query contract |
| `components/Incidents.test.tsx` | 15 | The feed, action badges, **the Needs action filter and its deeper fetch (bug 7)**, the WebSocket truncation sub-bug, and which remediation controls appear for which role |
| `components/Policies.test.tsx` | 11 | Policy listing, rule construction from the form, the requires-approval flag, and read-only gating |
| `App.test.tsx` | 9 | Sign-in gating, tab routing, sign-out clearing the session, and `canAct` derived from the role |

No browser or cluster needed — `make test-frontend`.

### These tests were validated by breaking things

A test that has never been seen to fail proves nothing, so each of the
important ones was checked against a deliberately broken system:

- **Reconnect tests** — reverted the `publish` fix; 2 tests failed, then
  passed once restored.
- **End-to-end pipeline** — scaled `cost-intelligence` to zero replicas;
  the test failed with `timed out after 45s waiting for cost_events rows`,
  then passed after scaling back up.
- **Executor RBAC** — temporarily granted the ServiceAccount `delete pods`;
  `test_cannot_do_anything_else[delete-pods-]` failed, then passed once the
  original Role was reapplied. So the RBAC assertions genuinely detect
  privilege widening rather than passing by default.
- **Collector RBAC** — same treatment with `patch deployments`;
  `test_has_no_write_access_at_all[patch-deployments-apps]` failed, then
  passed after restoring the ClusterRole.
- **Kubelet parsing (bug 2)** — reverted the `_preload_content=False` fix;
  both the unit and integration tests failed with the same
  `JSONDecodeError` that originally crash-looped the DaemonSet, then passed
  once restored.
- **WebSocket truncation (bug 7)** — reverted the `slice(0, 50)` fix. The
  first version of the test **passed anyway**: `waitFor` with a
  "still present" assertion succeeds on its first check, against the DOM
  from *before* React flushed the bad update. Rewritten to push the alert
  inside `act()`; it then failed against the reverted code and passed once
  restored. A worthwhile reminder that an unvalidated regression test can
  be worse than none, because it looks like coverage.

`test_pipeline_e2e.py` is the only test that would catch a break *between*
services — a renamed routing key, a dropped schema field, a consumer that
stopped acking. It is also the slowest, and is marked `slow`
(`pytest -m "not slow"` skips the two heaviest cases).

### What is still not covered

- **End-to-end browser flows.** The frontend has component tests, but
  nothing drives a real browser against the real API — the login → detect →
  roll back journey is still only verified by hand.
- **Broker-outage handling.** The `dispatch_failed` path has fired once, in
  the live incident that prompted it; provoking it in a test means taking
  RabbitMQ down underneath an in-flight request.

## Bugs found by this testing

Nine real defects. Seven were found by running the system rather than
reading it, the eighth by writing tests for it, and the ninth by a demo
failing in a later session. None were visible from reading the code.

### Found while building the pipeline

1. **Kubernetes service-link env collision** — Kubernetes injects
   `RABBITMQ_PORT=tcp://...` for any Service named `rabbitmq`, colliding
   with our own `RABBITMQ_*` settings prefix and crashing every service at
   startup. Fixed with `enableServiceLinks: false`.
2. **Double-serialised kubelet response** — the Kubernetes Python client
   returned the stats endpoint's JSON as a Python `repr()` string, which
   `json.loads` cannot parse. Fixed by reading the raw response.
3. **Pod-name identity destroyed baselines** — keying services on
   `namespace/pod` meant that throttling a workload (which restarts its
   pods) changed its identity and orphaned the very baseline that detected
   the incident. Baselines could never mature past a restart. Fixed by
   resolving the owning workload via `ownerReferences`.
4. **Baseline poisoning** — folding flagged readings into the baseline
   taught the detector to accept the anomalies it should catch. Observed
   directly: after two injected spikes, a **470× cost jump scored only
   z = 2.4 and went undetected**. Fixed by withholding flagged readings
   from the baseline, with a bounded escape hatch so a genuinely shifted
   workload is eventually re-baselined. Covered by a regression test.

### Found by clicking "Roll back" in the UI

A single user action surfaced three more:

5. **Stale AMQP connection broke the first click after any idle period.**
   `api-bff` publishes only when a human approves or rolls back, so its
   connection sat idle and the broker reaped it. pika only discovers this
   on the next write — `connection.is_closed` still reads `False` — so the
   reconnect check in `_ensure_channel` never fired and the request
   returned **500 `StreamLostError`**. Fixed with reconnect-and-retry in
   `RabbitMQClient.publish`, the AMQP equivalent of the `pool_pre_ping` the
   database side already had. Covered by `tests/test_messaging.py`, which
   was confirmed to fail against the pre-fix code.

   This is the one most likely to have hit a live demo: set up, present for
   twenty minutes, click the button, get an error.

6. **A failed publish left the audit log asserting work that never
   happened.** The `actions_log` row is written *before* publishing
   (the executor finalizes the outcome by updating that row), so when the
   publish failed the row sat at `dispatched` forever. For an append-only
   audit trail that is a correctness problem, not cosmetics. A failed
   publish now moves the row to a terminal `dispatch_failed` state and the
   caller gets a 503 instead of a false success.

7. **The actionable incident was unreachable in the triage view.** A quiet
   cluster emits a steady trickle of low-value anomalies, and the incident
   that actually triggered remediation had sunk to **rank 223** while the
   feed loaded only the newest 50 — so the Roll back button was never
   rendered. Added a **Recent / Needs action** filter. Fixing that exposed a
   sub-bug: the WebSocket handler hard-capped the list at 50 on every
   incoming alert, truncating the deeper fetch moments after it loaded.

### Found by writing the tests themselves

8. **A scaled-to-zero workload was mishandled by accident.**
   `freeze_scaling` read `hpa.status.current_replicas or previous_max` —
   and `0` is falsy in Python, so a workload genuinely at zero replicas was
   treated as "status unavailable" and fell back to `previous_max`. The
   observable behaviour happened to be correct (a no-op), but only by
   coincidence: any change to that fallback would have silently started
   pinning `maxReplicas` at `0`, freezing the workload permanently.

   Rewritten to distinguish "not reported yet" (`is None`) from "genuinely
   zero", each with its own explicit no-op and reason string. Caught by
   `test_scaled_to_zero_is_left_alone` before it could ever fire in
   production, since no demo workload has an HPA.

### Found by a demo failing in a later session

9. **A shared-package fix didn't reach every service that needed it.**
   `shared/hypertrace_common` is baked into each service's Docker image at
   build time, so the AMQP reconnect fix from bug 5 only took effect for
   services rebuilt afterward. `security-signal-adapter` and the
   `collector` DaemonSet were not, and kept running the pre-fix code. The
   cryptomining demo then failed silently — `security-signal-adapter`
   returned a 500 on the first signal it published after sitting idle,
   for the exact reason bug 5 was supposed to have fixed everywhere.

   Caught by re-running the demo, tracing the 500 back to the deployed
   image, and confirming with `inspect.getsource()` against the running
   pod that it lacked the retry logic present in the checked-out source.
   Fixed by rebuilding and redeploying both images; see
   `docs/KNOWN-LIMITATIONS.md` §12 for the structural gap this exposes —
   there is no automated check that a running service matches the shared
   package it was built against.

### Which to raise unprompted in the defense

Bugs 3, 4, 5, and 9. Each is a case where the system was quietly *wrong*
rather than visibly broken:

- **3** — remediation destroyed the baseline that detected the incident
- **4** — the detector learned to accept the anomalies it exists to catch
- **5** — the control worked in testing and failed exactly when a real
  operator would reach for it, after a period of inactivity
- **9** — a fix that was genuinely correct and tested still didn't protect
  the system, because deployment, not code, was the gap

Bug 6 is worth mentioning alongside 5 if the panel probes auditability,
since it is the difference between an audit log that is trustworthy and one
that merely looks complete.
