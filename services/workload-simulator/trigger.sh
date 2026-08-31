#!/usr/bin/env bash
# Injects a controllable synthetic incident against the victim workload, for
# demoing/testing the detection and decision pipeline (doc Section 15, Phase 3).
#
# Usage: ./trigger.sh <scenario> [seconds]
#
#   runaway-retry   Pegs ~1 CPU core on the victim pod for `seconds`
#                   (default 90). Cost spikes with no corroborating security
#                   signal, so the Decision Engine should classify this as
#                   a deployment bug or plain waste — doc Section 2.2.
#
#   traffic-surge   Scales the load generator up so request volume AND cost
#                   rise together. The detector must NOT act: this is the
#                   flash-sale case, and throttling it would be worse than
#                   the problem. Classified `legitimate_traffic_growth`.
#                   Run `./trigger.sh traffic-surge-stop` to restore.
#
#   cryptomining    The same CPU burn, but also emits a runtime-security
#                   signal for the victim. The Decision Engine should now
#                   classify the same cost spike as `suspected_abuse`
#                   instead — this is the joint-reasoning differentiator
#                   from doc Section 9.1, and running both scenarios
#                   back-to-back is the clearest way to demonstrate it.
#                   NOTE: the security signal is injected, not detected —
#                   see services/security-signal-adapter/README.md.
set -euo pipefail

SCENARIO="${1:-runaway-retry}"
DURATION="${2:-90}"
NS=hypertrace

burn() {
  kubectl -n "$NS" run "trigger-$RANDOM" --image=curlimages/curl --rm -i --restart=Never --quiet -- \
    curl -sS -X POST "http://victim:8080/burn?seconds=${DURATION}" >/dev/null
  echo "CPU burn started on victim (~1 core, ${DURATION}s)."
}

emit_security_signal() {
  # Service identity is namespace/workload (the stable Deployment name), not
  # namespace/pod — see services/collector/app/workload_resolver.py.
  local service="${NS}/victim"
  kubectl -n "$NS" run "sec-$RANDOM" --image=curlimages/curl --rm -i --restart=Never --quiet -- \
    curl -sS -X POST "http://security-signal-adapter:8090/signal" \
      -H 'Content-Type: application/json' \
      -d "{\"service\":\"${service}\",\"rule\":\"unexpected_outbound_connection\",\"severity\":\"critical\",\"detail\":{\"note\":\"synthetic signal from workload-simulator\"}}" >/dev/null
  echo "Security signal emitted for ${service}."
}

case "$SCENARIO" in
  traffic-surge)
    kubectl -n "$NS" scale deployment/loadgen --replicas="${2:-12}" >/dev/null
    echo "Load generator scaled to ${2:-12} replicas."
    echo "Expect: cost and traffic rise together -> legitimate_traffic_growth, no action."
    echo "Run './trigger.sh traffic-surge-stop' when done."
    ;;
  traffic-surge-stop)
    kubectl -n "$NS" scale deployment/loadgen --replicas=1 >/dev/null
    echo "Load generator restored to 1 replica."
    ;;
  runaway-retry)
    burn
    echo "Expect classification: likely_bug_from_deployment or misconfiguration_or_waste."
    ;;
  cryptomining)
    emit_security_signal
    burn
    echo "Expect classification: suspected_abuse (cost anomaly corroborated by a security signal)."
    ;;
  *)
    echo "Unknown scenario: $SCENARIO (known: runaway-retry, cryptomining, traffic-surge, traffic-surge-stop)" >&2
    exit 1
    ;;
esac
