# HyperTrace / CloudAegis

Autonomous cloud cost-intelligence and infrastructure-protection platform.
The full problem statement, architecture, requirements spec, tech-stack
justification, and defense kit live in [`docs/report.html`](docs/report.html)
(also exported as PDF alongside it) — read that first if you're new to the
project. This repo is the phase-by-phase implementation of that dossier.

Stack: **FastAPI** services, **RabbitMQ** as the event bus, **PostgreSQL +
TimescaleDB** for storage, **Prometheus + Grafana** for raw metrics, **React +
TypeScript** for the dashboard, all running locally on **kind** (Kubernetes
in Docker) — no cloud account required to build or demo.

## Repository layout

```
docs/               source dossier (report.html + PDF exports)
infra/
  kind/             local cluster definition
  k8s/              Kubernetes manifests, grouped by component
  sql/              schema shared by docker-compose and the in-cluster DB
services/
  collector/               per-node metrics + lifecycle-event agent (DaemonSet)
  cost-intelligence/       turns metrics into live $/hour
  behaviour-analysis/      rolling baselines + Z-score anomaly detection
  decision-policy/         joint classification + policy evaluation
  remediation-executor/    the only component with cluster write access
  security-signal-adapter/ runtime-security signal ingestion point
  api-bff/                 FastAPI gateway: auth, reads, approve/rollback, WS
  workload-simulator/      synthetic incident generator (victim + trigger.sh)
frontend/                  React + TypeScript dashboard
tests/                     64 backend unit tests; tests/integration/ 71 live-cluster
frontend/src/**/*.test.tsx  47 React component tests
scripts/                   benchmark.sh (NFRs), integration-test.sh
shared/
  hypertrace_common/  pip-installable package: message schemas, RabbitMQ
                       client, DB session helper — used by every service
docker-compose.dev.yml   RabbitMQ + TimescaleDB only, for fast local dev
Makefile                 cluster/build/deploy shortcuts (see below)
```

### How the pieces connect

```
collector ─┬─ metric.raw ──────▶ cost-intelligence ── cost.event ──▶ behaviour-analysis
           └─ event.lifecycle ─┐                                            │
                              │                                    anomaly.flagged
security-signal-adapter ── security.signal ─┐                               │
                              └─────────────┴──▶ decision-policy ◀──────────┘
                                                        │
                                            remediation.requested
                                                        ▼
                                             remediation-executor ──▶ Kubernetes API

TimescaleDB ◀── cost_events / anomalies / actions_log ──▶ api-bff ──▶ React dashboard
```

## Prerequisites

- Docker Desktop (running)
- `kubectl`
- [`kind`](https://kind.sigs.k8s.io/docs/user/quick-start/#installation)
- Python 3.11+ (for running/testing services outside a container)

`make` is optional — every target below is a single command, listed in the
Makefile and repeated here so Windows users without `make` can copy-paste
directly.

## Bringing up the base layer

```bash
# 1. Create the local cluster
kind create cluster --config infra/kind/kind-cluster.yaml

# 2. Namespace + least-privilege RBAC for the collector
kubectl apply -f infra/k8s/00-namespace.yaml
kubectl apply -f infra/k8s/01-rbac.yaml

# 3. Shared infra: RabbitMQ, TimescaleDB, Prometheus, Grafana
kubectl apply -f infra/k8s/rabbitmq/
kubectl apply -f infra/k8s/postgres-timescaledb/
kubectl apply -f infra/k8s/prometheus/
kubectl apply -f infra/k8s/grafana/

# 4. Build and load the collector image into the cluster
docker build -f services/collector/Dockerfile -t hypertrace/collector:dev .
kind load docker-image hypertrace/collector:dev --name hypertrace

# 5. Deploy the collector DaemonSet
kubectl apply -f infra/k8s/services/collector-daemonset.yaml
```

Or, with `make`: `make phase1`.

### Verifying it worked

```bash
# Pods should all reach Running/Ready
kubectl -n hypertrace get pods

# Grafana — user/pass not needed, anonymous admin is enabled for local dev
kubectl -n hypertrace port-forward svc/grafana 3000:3000
# open http://localhost:3000 -> HyperTrace folder -> "Cluster Overview"
# you should see live CPU/memory lines per pod within ~30s

# RabbitMQ management UI (login: hypertrace / hypertrace-dev)
kubectl -n hypertrace port-forward svc/rabbitmq 15672:15672
# open http://localhost:15672 -> Queues -> confirm the hypertrace.events
# exchange has a non-zero publish rate (collector is publishing MetricEvents
# every ~10s per node)
```

### Local dev without the cluster

```bash
docker compose -f docker-compose.dev.yml up -d
```

Brings up just RabbitMQ + TimescaleDB with the same schema
(`infra/sql/init.sql`), useful when iterating on a service's logic without
redeploying into kind each time.

## Roadmap

This is the same phase breakdown as `docs/report.html` Section 15, adapted
to the FastAPI/kind/RabbitMQ stack. All phases are implemented and verified
against a live cluster, except Phase 6, which ships the security-signal
ingestion point and correlation but not a real Falco deployment — see
`services/security-signal-adapter/README.md`.

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Repo scaffold, shared schemas/messaging/DB package | Done |
| 1 | kind cluster, Prometheus/Grafana, RabbitMQ, TimescaleDB, collector agent | Done |
| 2 | Cost Intelligence Engine (live $/hour), API-BFF cost endpoints | Done |
| 3 | Behaviour Analysis Engine (Z-score baselines), workload simulator | Done |
| 4 | Decision & Policy Engine, Remediation Executor (throttle/freeze-scaling) | Done |
| 5 | React dashboard (cost view, incident feed, policy builder, live alerts) | Done |
| 6 | Security-signal correlation (ingestion point; Falco not deployed) | Partial |
| 7 | Benchmarking, test suite, defense-ready docs | Done |

## Running the whole system

After the Phase 1 bring-up above, deploy the remaining services:

```bash
kubectl apply -f infra/k8s/services/
```

Build and load each image first (`docker build -f services/<name>/Dockerfile -t hypertrace/<name>:dev .`, then `kind load docker-image hypertrace/<name>:dev --name hypertrace`).

### Demo: the joint-reasoning differentiator

The clearest short demo is the same cost spike classifying two different
ways depending on the corroborating evidence.

```bash
kubectl -n hypertrace port-forward svc/api-bff 18000:8000
npm run dev --prefix frontend
```

Sign in at http://localhost:5173 as `sre` / `hypertrace-dev`, open the
Incidents tab, then run:

```bash
./services/workload-simulator/trigger.sh runaway-retry 60
```

Let the baseline settle (~1 minute), then run:

```bash
./services/workload-simulator/trigger.sh cryptomining 60
```

The first classifies as waste or a deployment bug. The second — the same
CPU burn, but corroborated by a security signal — classifies as
`suspected_abuse`. With a matching policy in place, remediation fires
automatically; switch the Incidents feed to **Needs action** to find the
incident that acted, where a **Roll back** button undoes it.

### Tests and benchmarks

```bash
pip install -r requirements-dev.txt

make test           # 64 backend unit tests, no cluster needed
make test-frontend  # 47 React component tests (vitest + jsdom)
make test-all       # both of the above
make integration    # 71 integration tests against the live cluster
pytest -q           # both (integration skips if the cluster is unreachable)
./scripts/benchmark.sh   # measures NFR-1 and NFR-2
```

The integration suite drives real RabbitMQ, TimescaleDB, the api-bff and
the Kubernetes API — including an end-to-end test that publishes metrics and
asserts the deployed services turn them into a flagged anomaly, and
remediation tests that throttle and roll back a disposable Deployment while
verifying the executor's RBAC really is as narrow as it claims. `make integration` opens the
port-forwards it needs and cleans up after itself; everything it writes is
namespaced `itest-` and deleted afterwards, so it is safe against the demo
cluster.

## Scope honesty

Per `docs/report.html` Section 11.7, this is a narrow working slice per
phase, not a production-parity system.

**Read [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md) before
presenting this.** It lists every real gap between the dossier's claims and
the implementation — most importantly that there is no traffic telemetry
(so the "cost rising while traffic stays flat" claim is only half-measured),
that throttle restarts the workload it throttles, and that the security
signal is injected rather than detected by Falco.

[`docs/VERIFICATION.md`](docs/VERIFICATION.md) records what was actually
tested against a live cluster, the measured NFR numbers, and the seven real
bugs that testing uncovered - including three found by a single click on
the dashboard's Roll back button.
