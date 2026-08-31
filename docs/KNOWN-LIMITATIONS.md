# Known Limitations

Written for the project defense. Every item here is a real gap between what
the dossier (`report.html`) describes and what the code actually does. A
panel that finds one of these before you name it costs you far more than
naming it first (dossier Section 11.7).

## 1. Throttle restarts the workload it throttles

The dossier's action table (Section 3, Phase 4) says throttle "caps
CPU/requests the workload can consume **without killing it**." Our
implementation patches the Deployment's container CPU limit, and Kubernetes
responds to any pod-template change with a **rolling restart**. The cost is
capped as intended and the Deployment stays available through the rollout,
but the pods are replaced and in-flight requests are dropped.

A non-disruptive implementation needs the Kubernetes in-place pod resize
feature (`pods/resize` subresource), which is alpha in 1.31 and behind a
feature gate — deliberately not relied on here.

**Say:** "throttle is implemented as a Deployment resource patch, which
triggers a rolling restart. Genuinely in-place throttling needs the alpha
in-place resize API."

## 2. No traffic telemetry, so one classification branch is missing

Doc 14.3 branches on `traffic_z` to separate a legitimate demand spike from
a bug. HyperTrace collects CPU/memory/network from the kubelet but has **no
request-count or trace-count telemetry**, so it cannot compute `traffic_z`.

Consequences:
- `legitimate_traffic_growth` is defined in the schema but is **never
  produced**. A real flash-sale would be classified as waste or a
  deployment bug.
- `cost_per_unit_of_work` (FR-4, cost-per-request) is a column in
  `cost_events` that is **always NULL**.

This is the single largest gap between the dossier's headline claim
("cost rising while traffic stays flat") and the implementation: we detect
the cost half and infer the rest. Closing it means scraping application
request counters (e.g. a Prometheus `http_requests_total`) per workload.

## 3. The security signal is injected, not detected

The correlation is real; the producer is not. See
`services/security-signal-adapter/README.md`. Falco/Tetragon is not
deployed. The accurate claim is *"HyperTrace correlates runtime-security
signals with cost anomalies, and is wired to accept them from Falco"* — not
*"HyperTrace detects cryptomining."*

## 4. Cost model is compute-only and uses static pricing

`cost-intelligence` prices CPU and memory from a static ConfigMap
(`pricing.yaml`). It does **not** price network egress, storage, GPUs,
load balancers, or managed services, and it never reconciles against a real
cloud bill. The dossier's Billing Adapter Service (Section 5.1), which
would poll AWS Cost & Usage Reports for authoritative rates, **was not
built**. Absolute dollar figures are therefore illustrative; the *relative*
cost signal driving detection is what matters here.

## 5. Detection is univariate

Only `cost_per_hour` is baselined. The dossier's Isolation Forest over
`[cpu, mem, net, cost, request_count]` (Section 14.4) is not implemented —
it was explicitly listed as an optional stretch, and doc 13 argues an
unvalidated ML claim is worse than an honestly-scoped statistical one.

## 6. A perfectly flat metric is undetectable

`BucketStats.z_score` returns 0 when stddev is 0, to avoid dividing by
zero. A workload whose cost never varies at all therefore never triggers,
no matter how far a new value jumps. Real metrics carry enough jitter that
this has not been observed in practice, but it is a genuine blind spot — a
relative-change guard alongside the z-score would close it.

## 7. Multi-replica workloads mix their replicas into one baseline

Identity is `namespace/workload`, so every replica's cost sample feeds the
same baseline. For a Deployment with uneven replicas (or a DaemonSet across
heterogeneous nodes) this inflates the variance and dulls sensitivity.
Per-replica baselines under a shared workload identity would be the fix.

## 8. Single-replica stateful services

`decision-policy` holds its recent-deployment and recent-security-signal
maps **in memory**, so it must run at `replicas: 1` (noted in its
manifest). Scaling it out would split that state and silently degrade
classification. Moving the state to Redis or Postgres is the fix.

## 9. Prototype-grade operational posture

Not addressed, and out of scope by design: credentials are plaintext dev
values in manifests; TLS is absent between services; there is no
multi-tenancy (dossier 5.4) — one `org_id` column exists but nothing
enforces isolation; and quarantine/terminate (FR-8) are **not implemented**,
only throttle and freeze-scaling.

## 10. Broker-failure handling is barely exercised

`RabbitMQClient.publish` now reconnects once and retries. That path is
covered by unit tests with a faked connection *and* an integration test
that closes a real connection mid-flight, so it is reasonably solid.

The weaker half is the failure handling around it: a publish failure moves
the `actions_log` row to `dispatch_failed` and returns a 503, but that
branch has fired exactly once, during the live incident that prompted it.
There is no chaos testing and no automated coverage of it, because
provoking it means taking the broker down underneath a request.

Related, and untested: the consumers (`consume`) have no equivalent
reconnect. They rely on the pod crashing and Kubernetes restarting it,
which works but means a broker blip shows up as a CrashLoopBackOff rather
than a graceful reconnect.

## 11. What was verified, and how

Detection, classification, remediation, rollback, RBAC, and the safety
floors were all exercised against a live cluster — see
`docs/VERIFICATION.md` for the specific evidence, including the eight bugs
that testing uncovered.

182 automated tests now cover the backend logic (64 unit), the I/O paths
(71 integration) and the dashboard (47 component tests) — including an
end-to-end run through the deployed services, real-cluster tests of the
remediation executor's Kubernetes writes, real-kubelet tests of the
collector, and SubjectAccessReview checks that both agents' RBAC is as
narrow as claimed.

What still has no automated coverage: **end-to-end browser flows** (the
components are tested, but nothing drives a real browser against the real
API) and the broker-outage `dispatch_failed` path. Both are listed with
their reasons in `docs/VERIFICATION.md`.
