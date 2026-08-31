# Security Signal Adapter

Emits `SecuritySignal` messages onto the `security.signal` routing key, which
the Decision & Policy Engine correlates with cost anomalies to reach a
`suspected_abuse` classification (doc 14.3).

## What is real here, and what is not

**Real:** the ingestion contract and the correlation. `SecuritySignal`
(`shared/hypertrace_common/schemas.py`) is the shape any runtime-security
producer must emit, the Decision Engine genuinely consumes it, and the joint
classification genuinely changes its verdict when a security signal and a
cost anomaly land together for the same service within the correlation
window. That joint reasoning is the project's central claim (doc Section
9.1) and it is implemented and testable.

**Not real:** the producer. This adapter exposes an HTTP endpoint that a
human (or the workload simulator) calls to *inject* a signal. It is not
watching syscalls, processes, or network flows. A production deployment
would replace this one service with a real eBPF-based tool — Falco or
Tetragon (doc Section 7.4) — forwarding its alerts into the same routing
key, and nothing downstream would need to change.

Falco is deliberately not deployed in this prototype: it needs a privileged
kernel driver that is unreliable under kind-on-WSL2, and standing it up
would not change any code path this adapter doesn't already exercise. Per
doc Section 11.7, it is more defensible to ship a working, honestly-labelled
integration point than an overclaimed one.

**Do not present this as "HyperTrace detects cryptomining."** The accurate
claim is: *HyperTrace correlates a runtime-security signal with a cost
anomaly to classify abuse, and is wired to accept those signals from Falco
or Tetragon.*

## Usage

```bash
# Inject a signal for a service (as the workload simulator's cryptomining
# scenario does automatically)
curl -X POST "http://security-signal-adapter:8090/signal" \
  -H 'Content-Type: application/json' \
  -d '{"service":"hypertrace/victim-abc","rule":"unexpected_outbound_connection","severity":"critical"}'
```
