CLUSTER_NAME := hypertrace
SERVICES := collector cost-intelligence behaviour-analysis decision-policy \
            remediation-executor security-signal-adapter api-bff

.PHONY: cluster-up cluster-down namespace rbac infra images load deploy all \
        test bench dev-up dev-down dashboard

cluster-up:
	kind create cluster --config infra/kind/kind-cluster.yaml

cluster-down:
	kind delete cluster --name $(CLUSTER_NAME)

namespace:
	kubectl apply -f infra/k8s/00-namespace.yaml

rbac:
	kubectl apply -f infra/k8s/01-rbac.yaml

infra:
	kubectl apply -f infra/k8s/rabbitmq/
	kubectl apply -f infra/k8s/postgres-timescaledb/
	kubectl apply -f infra/k8s/prometheus/
	kubectl apply -f infra/k8s/grafana/

# The victim workload has no shared package, so it builds from its own path.
images:
	@for s in $(SERVICES); do \
		echo "building $$s"; \
		docker build -q -f services/$$s/Dockerfile -t hypertrace/$$s:dev . || exit 1; \
	done
	docker build -q -f services/workload-simulator/victim/Dockerfile -t hypertrace/victim:dev .

load:
	kind load docker-image $(foreach s,$(SERVICES),hypertrace/$(s):dev) hypertrace/victim:dev --name $(CLUSTER_NAME)

deploy:
	kubectl apply -f infra/k8s/services/

# Full bring-up from nothing.
all: cluster-up namespace rbac infra images load deploy

# Unit tests only: infrastructure is stubbed, so no cluster needed.
test:
	python -m pytest tests -q --ignore=tests/integration

# Frontend component tests (vitest + jsdom). No cluster or browser needed.
test-frontend:
	npm test --prefix frontend

# Everything that runs without a cluster.
test-all: test test-frontend

# Integration tests: drives the live cluster. Opens the port-forwards it
# needs and tears them down afterwards.
integration:
	./scripts/integration-test.sh

bench:
	./scripts/benchmark.sh

dashboard:
	npm run dev --prefix frontend

dev-up:
	docker compose -f docker-compose.dev.yml up -d

dev-down:
	docker compose -f docker-compose.dev.yml down
