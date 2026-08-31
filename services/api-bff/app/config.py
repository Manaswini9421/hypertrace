import os

# Dev-only default — infra/k8s/services/api-bff.yaml overrides this from a
# Secret. Never reuse this literal value outside a local kind cluster.
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-insecure-secret-change-me")
JWT_ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = int(os.environ.get("TOKEN_EXPIRE_HOURS", "8"))
