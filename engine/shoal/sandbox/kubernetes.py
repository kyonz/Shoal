"""Kubernetes backend: one Pod per sandbox, commands over the exec API.

Requires ``pip install 'shoal[kubernetes]'``. Uses in-cluster config when
available, else the local kubeconfig.

Profile ``config``:

* ``namespace`` - default ``shoal-sandboxes``; ``{tenant}`` is substituted, so
  ``shoal-{tenant}`` gives one namespace per tenant
* ``runtime_class`` - e.g. ``gvisor`` or ``kata`` for stronger isolation
* ``service_account``, ``image_pull_secrets``, ``node_selector``, ``tolerations``
* ``run_as_user`` - default 1000

``network.egress: deny`` is enforced with a NetworkPolicy per pod (no egress at all),
so the cluster's CNI must enforce NetworkPolicies (Calico, Cilium, ...).
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shoal.core.ids import ulid
from shoal.sandbox.base import ExecResult, Sandbox, SandboxBackend, SandboxSpec, shell_quote
from shoal.sandbox.wrap import parse_wrapped, wrapped_command, write_chunks

DEFAULT_IMAGE = "ghcr.io/kyonz/shoal-sandbox:latest"
SANDBOX_LABEL = "shoal.dev/sandbox"
MANAGED_SELECTOR = "app.kubernetes.io/managed-by=shoal"


def _quantity_cpu(cpu: float) -> str:
    return f"{int(cpu * 1000)}m"


class KubernetesSandbox(Sandbox):
    def __init__(self, backend: KubernetesBackend, name: str, namespace: str, spec: SandboxSpec):
        self.backend = backend
        self.id = f"{namespace}/{name}"
        self.name = name
        self.namespace = namespace
        self.workdir = spec.workdir
        self.env = spec.env
        self.network_policy = spec.network.egress == "deny"

    async def _exec_raw(self, argv: list[str]) -> str:
        from kubernetes_asyncio import client
        from kubernetes_asyncio.stream import WsApiClient

        async with WsApiClient(configuration=await self.backend.configuration()) as ws:
            api = client.CoreV1Api(api_client=ws)
            return await api.connect_get_namespaced_pod_exec(
                self.name, self.namespace, command=argv, container="sandbox",
                stderr=True, stdin=False, stdout=True, tty=False,
            )

    async def exec(self, command, *, cwd=None, env=None, timeout=None) -> ExecResult:  # type: ignore[no-untyped-def]
        started = time.monotonic()
        argv = wrapped_command(command, cwd=self.resolve(cwd) if cwd else self.workdir, env=env, timeout=timeout)
        output = await self._exec_raw(argv)
        return parse_wrapped(output, time.monotonic() - started)

    async def write_file(self, path: str, data: bytes | str) -> None:
        raw = data.encode() if isinstance(data, str) else data
        for argv in write_chunks(self.resolve(path), raw):
            await self._exec_raw(argv)

    async def read_file(self, path: str) -> bytes:
        output = await self._exec_raw(
            ["sh", "-c", f"test -f {shell_quote(self.resolve(path))} && base64 {shell_quote(self.resolve(path))} | tr -d '\\n' || echo __SHOAL_MISSING__"]
        )
        if "__SHOAL_MISSING__" in output:
            raise FileNotFoundError(path)
        return base64.b64decode(output.strip())

    async def close(self) -> None:
        from kubernetes_asyncio import client

        async with client.ApiClient(await self.backend.configuration()) as api:
            try:
                await client.CoreV1Api(api).delete_namespaced_pod(self.name, self.namespace, grace_period_seconds=5)
            except client.ApiException as exc:
                if exc.status != 404:
                    raise
            if self.network_policy:
                await self.backend.delete_network_policy(api, self.name, self.namespace)


class KubernetesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: str = Field("shoal-sandboxes", description="Namespace for sandbox pods; {tenant} is replaced, e.g. shoal-{tenant}.")
    runtime_class: str | None = Field(None, description="RuntimeClass for stronger isolation, e.g. gvisor or kata.")
    service_account: str | None = Field(None, description="Service account the pod runs as (its token is not mounted).")
    image_pull_secrets: list[str] = Field(default_factory=list, description="Secrets in the namespace for pulling the image.")
    node_selector: dict[str, str] = Field(default_factory=dict)
    tolerations: list[dict[str, Any]] = Field(default_factory=list)
    run_as_user: int = Field(1000, ge=0)
    startup_timeout: float = Field(300, gt=0, description="Seconds to wait for the pod to start.")


class KubernetesBackend(SandboxBackend):
    name = "kubernetes"
    description = "One Pod per sandbox on Kubernetes (supports gVisor/Kata runtime classes)."
    network_controls = frozenset({"deny"})  # a per-pod NetworkPolicy
    config_schema = KubernetesConfig

    def __init__(self) -> None:
        self._config: Any = None

    async def configuration(self) -> Any:
        if self._config is None:
            from kubernetes_asyncio import client, config

            cfg = client.Configuration()
            try:
                config.load_incluster_config(client_configuration=cfg)
            except config.ConfigException:
                await config.load_kube_config(client_configuration=cfg)
            self._config = cfg
        return self._config

    def _namespace(self, spec: SandboxSpec) -> str:
        return spec.config.get("namespace", "shoal-sandboxes").format(tenant=spec.labels.get("shoal/tenant", "default")).lower()

    def pod_manifest(self, name: str, spec: SandboxSpec) -> dict[str, Any]:
        cfg = spec.config
        resources: dict[str, Any] = {}
        if spec.cpu or spec.memory_mb:
            limits = {}
            if spec.cpu:
                limits["cpu"] = _quantity_cpu(spec.cpu)
            if spec.memory_mb:
                limits["memory"] = f"{spec.memory_mb}Mi"
            resources = {"limits": limits, "requests": limits}
        labels = {k.replace("shoal/", "shoal.dev/"): v[:63] for k, v in spec.labels.items()}
        pod: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": {"app.kubernetes.io/managed-by": "shoal", **labels, SANDBOX_LABEL: name}},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "activeDeadlineSeconds": spec.ttl_seconds,
                "securityContext": {"runAsUser": cfg.get("run_as_user", 1000), "fsGroup": cfg.get("run_as_user", 1000)},
                "containers": [
                    {
                        "name": "sandbox",
                        "image": spec.image or DEFAULT_IMAGE,
                        "command": ["sh", "-c", f"mkdir -p {shell_quote(spec.workdir)} && exec sleep infinity"],
                        "workingDir": "/",
                        "env": [{"name": k, "value": v} for k, v in spec.env.items()],
                        "resources": resources,
                        "volumeMounts": [{"name": "workspace", "mountPath": spec.workdir}],
                        "securityContext": {"allowPrivilegeEscalation": False},
                    }
                ],
                "volumes": [
                    {"name": "workspace", "emptyDir": {"sizeLimit": f"{spec.disk_mb}Mi"} if spec.disk_mb else {}}
                ],
            },
        }
        if cfg.get("runtime_class"):
            pod["spec"]["runtimeClassName"] = cfg["runtime_class"]
        if cfg.get("service_account"):
            pod["spec"]["serviceAccountName"] = cfg["service_account"]
        if cfg.get("image_pull_secrets"):
            pod["spec"]["imagePullSecrets"] = [{"name": n} for n in cfg["image_pull_secrets"]]
        if cfg.get("node_selector"):
            pod["spec"]["nodeSelector"] = cfg["node_selector"]
        if cfg.get("tolerations"):
            pod["spec"]["tolerations"] = cfg["tolerations"]
        return pod

    @staticmethod
    def network_policy_manifest(name: str) -> dict[str, Any]:
        """Blocks all egress from the sandbox pod (ingress is untouched: commands arrive over the exec API)."""
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": name, "labels": {"app.kubernetes.io/managed-by": "shoal", SANDBOX_LABEL: name}},
            "spec": {"podSelector": {"matchLabels": {SANDBOX_LABEL: name}}, "policyTypes": ["Egress"], "egress": []},
        }

    @staticmethod
    async def delete_network_policy(api: Any, name: str, namespace: str) -> None:
        from kubernetes_asyncio import client

        try:
            await client.NetworkingV1Api(api).delete_namespaced_network_policy(name, namespace)
        except client.ApiException as exc:
            if exc.status != 404:
                raise

    async def create(self, spec: SandboxSpec) -> Sandbox:
        from kubernetes_asyncio import client

        namespace = self._namespace(spec)
        name = f"shoal-{ulid().lower()[-16:]}"
        async with client.ApiClient(await self.configuration()) as api:
            core = client.CoreV1Api(api)
            if spec.network.egress == "deny":  # in place before the pod exists, so it never runs unrestricted
                await client.NetworkingV1Api(api).create_namespaced_network_policy(namespace, self.network_policy_manifest(name))
            await core.create_namespaced_pod(namespace, self.pod_manifest(name, spec))
            deadline = time.monotonic() + float(spec.config.get("startup_timeout", 300))
            while True:
                pod = await core.read_namespaced_pod(name, namespace)
                phase = pod.status.phase
                if phase == "Running":
                    break
                if phase in ("Failed", "Succeeded") or time.monotonic() > deadline:
                    await core.delete_namespaced_pod(name, namespace)
                    if spec.network.egress == "deny":
                        await self.delete_network_policy(api, name, namespace)
                    raise RuntimeError(f"sandbox pod {name} did not start (phase {phase})")
                await asyncio.sleep(1)
        return KubernetesSandbox(self, name, namespace, spec)

    async def attach(self, sandbox_id: str, spec: SandboxSpec) -> Sandbox | None:
        from kubernetes_asyncio import client

        namespace, _, name = sandbox_id.partition("/")
        async with client.ApiClient(await self.configuration()) as api:
            try:
                pod = await client.CoreV1Api(api).read_namespaced_pod(name, namespace)
            except client.ApiException:
                return None
        if pod.status.phase != "Running":
            return None
        return KubernetesSandbox(self, name, namespace, spec)

    async def destroy(self, sandbox_id: str, spec: SandboxSpec) -> None:
        """Delete the pod whatever its phase: a pod the ttl ended is Failed, and attach() would skip it."""
        from kubernetes_asyncio import client

        namespace, _, name = sandbox_id.partition("/")
        async with client.ApiClient(await self.configuration()) as api:
            await self._delete(api, name, namespace)

    def reap_scope(self, config: dict[str, Any], tenant_id: str) -> str | None:
        return self._namespace(SandboxSpec(config=config, labels={"shoal/tenant": tenant_id}))

    async def reap(
        self, scope: str, run_state: Callable[[str | None, str | None], Awaitable[str]], *, min_age_seconds: float
    ) -> int:
        """Delete Shoal's pods in namespace ``scope`` that have ended or whose run is over, and their NetworkPolicies.

        Pods younger than ``min_age_seconds`` are left alone unless they have ended, so a sandbox that is being
        created (and not yet recorded on its run) is never swept.
        """
        from kubernetes_asyncio import client

        now = datetime.now(UTC)

        def old(meta: Any) -> bool:
            created = meta.creation_timestamp
            return created is None or (now - created).total_seconds() >= min_age_seconds

        removed = 0
        async with client.ApiClient(await self.configuration()) as api:
            pods = (await client.CoreV1Api(api).list_namespaced_pod(scope, label_selector=MANAGED_SELECTOR)).items
            kept: set[str] = set()
            for pod in pods:
                labels = pod.metadata.labels or {}
                sandbox = labels.get(SANDBOX_LABEL, pod.metadata.name)
                state = await run_state(labels.get("shoal.dev/tenant"), labels.get("shoal.dev/run"))
                ended = pod.status.phase in ("Failed", "Succeeded")
                if state == "foreign" or (not ended and (state == "active" or not old(pod.metadata))):
                    kept.add(sandbox)
                    continue
                await self._delete(api, pod.metadata.name, scope)
                removed += 1
            policies = await client.NetworkingV1Api(api).list_namespaced_network_policy(scope, label_selector=MANAGED_SELECTOR)
            for policy in policies.items:
                sandbox = (policy.metadata.labels or {}).get(SANDBOX_LABEL, policy.metadata.name)
                if sandbox not in kept and old(policy.metadata):
                    await self.delete_network_policy(api, policy.metadata.name, scope)
        return removed

    async def _delete(self, api: Any, name: str, namespace: str) -> None:
        from kubernetes_asyncio import client

        try:
            await client.CoreV1Api(api).delete_namespaced_pod(name, namespace, grace_period_seconds=5)
        except client.ApiException as exc:
            if exc.status != 404:
                raise
        await self.delete_network_policy(api, name, namespace)
