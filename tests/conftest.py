"""Test bootstrap.

Two problems this solves:

1. Every service uses the same internal package name (`app`), so they
   collide on sys.path. Each service is instead loaded under a unique
   alias (`svc_behaviour_analysis`, `svc_decision_policy`, ...) via
   importlib, preserving the relative imports inside each package.

2. Service modules import SQLAlchemy, pika, and the Kubernetes client at
   module scope. Those are stubbed so the pure decision logic can be
   tested without a cluster or a database. Only logic that performs no
   I/O is unit-tested here; the I/O paths are covered by the live
   end-to-end runs recorded in docs/VERIFICATION.md.
"""

import importlib.util
import os
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVICES = REPO / "services"


def _stub(name: str, **attrs) -> types.ModuleType:
    module = sys.modules.get(name) or types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _install_stubs() -> None:
    """Stubs only what `requirements-dev.txt` deliberately leaves out.

    SQLAlchemy, pika, pydantic and the shared package itself are real
    dependencies of the suite, so the tests exercise real code rather than
    stubs. Only the Kubernetes client is faked: it is imported at module
    scope by services whose logic is tested here, but none of the tested
    functions touch the cluster, and pulling in the client would add a
    heavy dependency for no coverage.
    """
    try:
        import kubernetes  # noqa: F401
    except ImportError:
        _stub(
            "kubernetes",
            client=types.SimpleNamespace(
                CoreV1Api=object, AppsV1Api=object, AutoscalingV2Api=object, ApiException=Exception
            ),
            watch=types.SimpleNamespace(Watch=object),
            config=types.SimpleNamespace(ConfigException=Exception, load_incluster_config=lambda: None),
        )


def _load_service(service_dir: str, alias: str) -> None:
    """Loads `services/<service_dir>/app` as a package named `alias`, so
    `alias.stats`, `alias.policy`, etc. import without colliding with the
    identically-named `app` package in every other service.
    """
    package_path = SERVICES / service_dir / "app"
    spec = importlib.util.spec_from_file_location(
        alias, package_path / "__init__.py", submodule_search_locations=[str(package_path)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)


# Path first: the stub installer probes whether the real hypertrace_common
# is importable, so it has to be reachable before that check runs — otherwise
# a stub is installed permanently and tests that need the real module (e.g.
# test_messaging.py) cannot import it.
sys.path.insert(0, str(REPO / "shared" / "hypertrace_common"))

# The collector reads NODE_NAME at import time (the DaemonSet injects it via
# fieldRef). Set a placeholder so its module is importable under test; any
# test that cares about the value overrides it explicitly.
os.environ.setdefault("NODE_NAME", "test-node")

_install_stubs()
_load_service("behaviour-analysis", "svc_behaviour")
_load_service("decision-policy", "svc_decision")
_load_service("remediation-executor", "svc_remediation")
_load_service("collector", "svc_collector")
