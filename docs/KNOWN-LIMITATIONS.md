# Known Limitations

> **Read `SPEC-GAP-ANALYSIS.md` alongside this.** This file lists gaps
> against Part I of the dossier (`report.html`), which is what the system
> was built from. The dossier's Parts II-III contain a detailed
> architecture spec and a 14-milestone build guide that were not used;
> the gap analysis records where the implementation diverges from them.

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

## 2. Detection scores three metrics, not five

Traffic telemetry now exists, so the decoupling test is real: the detector
scores cost, CPU and request rate per hour-of-week and requires cost/resource
past 3σ *while traffic stays under 1σ*. `legitimate_traffic_growth` is
reachable, and a flash sale is recognised rather than throttled.

What is still missing from §24.1's list: **memory working set** and **egress
bytes/second** are collected but never baselined. Memory is what
distinguishes a leak from a compute loop, and egress is both a cost driver
and the signal the cryptomining heuristic leans on — so without it, abuse
detection depends entirely on the injected security signal (§3 below).

`cost_per_unit_of_work` (FR-4, cost-per-request) remains **always NULL**:
the request rate is scored but never divided into cost.

**The demo caveat worth stating first.** Confidence includes a baseline
maturity term that needs ~720 samples (two hours). A demo baseline is
minutes old, so live confidence caps around 0.70 — the approval band. The
system therefore *asks* rather than *acts* in any short demo. That is the
specified behaviour, not a defect, but it means autonomous action is
demonstrated by the test suite rather than on screen.

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

## 5. Detection is statistical, not learned

Scoring is per-metric Z-scores combined by an explicit conjunction. The
dossier's Isolation Forest over `[cpu, mem, net, cost, request_count]`
(§14.4) is not implemented — it was listed as an optional stretch, and
§24.5 argues the point directly: a model that flags more accurately but
cannot state which condition it relied on cannot be granted authority to
act, because the audit requirement would be unsatisfiable. An
unexplainable detector can inform a human; only an explainable one can
replace them.

## 6. Baselines still bucket in UTC

§21.1 stores a `timezone` on each service because bucketing hour-of-week in
UTC smears a business-hours pattern across adjacent buckets for any service
whose users are not on UTC, "roughly halving the detector's sensitivity".
There is no `services` table, so there is nowhere to put the timezone and
every service is bucketed in UTC.

## 7. A perfectly flat metric is undetectable

`BucketStats.z_score` returns 0 when stddev is 0, to avoid dividing by
zero. A workload whose cost never varies at all therefore never triggers,
no matter how far a new value jumps. Real metrics carry enough jitter that
this has not been observed in practice, but it is a genuine blind spot — a
relative-change guard alongside the z-score would close it.

## 8. Multi-replica workloads mix their replicas into one baseline

Identity is `namespace/workload`, so every replica's cost sample feeds the
same baseline. For a Deployment with uneven replicas (or a DaemonSet across
heterogeneous nodes) this inflates the variance and dulls sensitivity.
Per-replica baselines under a shared workload identity would be the fix.

## 9. Single-replica stateful services

`decision-policy` holds its recent-deployment and recent-security-signal
maps **in memory**, so it must run at `replicas: 1` (noted in its
manifest). Scaling it out would split that state and silently degrade
classification. Moving the state to Redis or Postgres is the fix.

## 10. Prototype-grade operational posture

Not addressed, and out of scope by design: credentials are plaintext dev
values in manifests; TLS is absent between services; there is no
multi-tenancy (dossier 5.4) — one `org_id` column exists but nothing
enforces isolation; and quarantine/terminate (FR-8) are **not implemented**,
only throttle and freeze-scaling.

## 11. Broker-failure handling is barely exercised

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

## 12. Shared-package fixes only take effect once every consuming service is rebuilt

`shared/hypertrace_common` is installed into each service's Docker image at
build time, not mounted or shared at runtime. A fix landed in the shared
package therefore does nothing for a service until that service's image is
rebuilt and redeployed — there is no mechanism that rebuilds the fleet
automatically, and nothing that flags a service still running a stale copy.

This bit in practice: the AMQP reconnect fix (bug 5 in `VERIFICATION.md`)
landed in the shared package, but `security-signal-adapter` and the
`collector` DaemonSet were never rebuilt afterward. The cryptomining demo
then failed silently in a later session — `security-signal-adapter`
returned a 500 on the first signal it tried to publish after sitting idle,
because it was still running the pre-fix code — until both images were
rebuilt and redeployed.

**Say:** "each service bakes its own copy of the shared package at build
time, so a fix there needs every consumer rebuilt, not just the one
service being worked on. There is no automated staleness check; this one
was found by a demo failing." A CI step or Makefile target that rebuilds
every service whenever `shared/` changes — or diffs each running image's
installed package version against source at deploy time — would close
this.

## 13. What was verified, and how

Detection, classification, remediation, rollback, RBAC, and the safety
floors were all exercised against a live cluster — see
`docs/VERIFICATION.md` for the specific evidence, including the nine bugs
that testing uncovered.

213 automated tests now cover the backend logic (89 unit), the I/O paths
(75 integration) and the dashboard (49 component tests) — including an
end-to-end run through the deployed services, real-cluster tests of the
remediation executor's Kubernetes writes, real-kubelet tests of the
collector, and SubjectAccessReview checks that both agents' RBAC is as
narrow as claimed.

What still has no automated coverage: **end-to-end browser flows** (the
components are tested, but nothing drives a real browser against the real
API) and the broker-outage `dispatch_failed` path. Both are listed with
their reasons in `docs/VERIFICATION.md`.
