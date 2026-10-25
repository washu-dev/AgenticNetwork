"""Tests for the CloudRunBackend (Step 4) and its supervisor deploy trigger.

Exercised with an injected fake Cloud Run client — no real GCP, no
google-cloud-run install, no HTTP. The fake stands in for _CloudRunClient, so the
whole google client library stays out of the test path (it's lazily imported only
by the real client). These assert the deploy *spec* the backend would send, the
image digest / :latest fallback contract from Step 3, the invoker binding, and
that is_alive/stop never touch HTTP.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from registration_service import db, supervisor as sup_mod
from registration_service.backend import (
    CloudRunBackend,
    _cloudrun_service_id,
    _CloudRunClient,
    _deploy_spec,
    make_backend,
)
from registration_service.card_resources import RESOURCE_REQUIREMENTS_EXTENSION_URI
from registration_service.fake_db import reset_fake_db
from registration_service.models import CustomConfig, ResourceRequirements
from registration_service.supervisor import Supervisor, _build_agent_card

# This module mixes pure-sync (_deploy_spec) and async (backend/supervisor) tests,
# so mark the async ones individually rather than with a module-level pytestmark.
_aio = pytest.mark.asyncio


# ── Fakes ─────────────────────────────────────────────────────────────────────────


class _FakeCloudRunClient:
    """Records deploy specs / invoker grants; never makes a network call."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.invokers: list[tuple[str, str]] = []
        self._services: dict[str, dict] = {}
        self.http_calls = 0  # stays 0 — is_alive must not HTTP-GET
        self.get_calls = 0   # Admin-API get() invocations (liveness probes)

    async def create_or_replace(self, spec: dict) -> str:
        uri = f"https://{spec['service_id']}-abc123-uc.a.run.app"
        self.created.append(spec)
        self._services[spec["service_id"]] = {"ready": True, "uri": uri}
        return uri

    async def delete(self, service_id: str) -> None:
        self.deleted.append(service_id)
        self._services.pop(service_id, None)

    async def get(self, service_id: str) -> dict | None:
        self.get_calls += 1
        return self._services.get(service_id)

    async def set_invoker(self, service_id: str, member: str) -> None:
        self.invokers.append((service_id, member))


class _FakePublisher:
    class _Rec:
        backend_ref = "fake-cid"

    def __init__(self) -> None:
        self.published: list[dict] = []
        self.unpublished: list[str] = []

    def publish(self, card, advertise_url=None):
        self.published.append({"card": card, "advertise_url": advertise_url})
        return self._Rec()

    def unpublish(self, cid):
        self.unpublished.append(cid)
        return True  # DirectoryPublisher.unpublish: True once the directory confirmed


def _custom_config() -> CustomConfig:
    return CustomConfig(class_name="H", python_version="3.12", requirements=[], extra={})


async def _fresh_db():
    reset_fake_db()
    db._pool = None
    await db.init_db()


async def _make_custom(image_ref="sha256:deadbeef", status="image_ready", resources=None):
    agent = await db.create_agent(
        name="Paper Reviewer",
        description="Reviews papers",
        handler_type="custom",
        oasf_skills=[{"name": "nlp/text-summarization", "id": 1}],
        config={"class_name": "H", "python_version": "3.12", "requirements": [], "extra": {}},
        approved=True,
        resources=resources,
    )
    await db.update_agent(agent["id"], status=status, image_ref=image_ref)
    return agent


def _cloud_env(monkeypatch):
    monkeypatch.setenv("SPAWN_BACKEND", "cloudrun")
    monkeypatch.setenv("GCP_PROJECT", "example-project")
    monkeypatch.setenv("GCP_REGION", "us-central1")
    monkeypatch.setenv("AGENT_RUNTIME_SA", "agent-runtime@example-project.iam.gserviceaccount.com")
    monkeypatch.setenv("CHAT_APP_INVOKER_SA", "chat-app@example-project.iam.gserviceaccount.com")
    monkeypatch.setenv("VPC_CONNECTOR", "chat-app-connector")


@pytest.mark.parametrize("invoker, warns", [("chat-app@example-project.iam.gserviceaccount.com", False), (None, True)])
def test_a_missing_invoker_account_is_logged(monkeypatch, caplog, invoker, warns):
    """Without CHAT_APP_INVOKER_SA the chat app cannot call deployed agents; say so at startup."""
    _cloud_env(monkeypatch)
    if invoker is None:
        monkeypatch.delenv("CHAT_APP_INVOKER_SA")
    with caplog.at_level("WARNING", logger="registration_service.backend"):
        CloudRunBackend(client=object())
    assert any("cloudrun_no_invoker_sa" in r.getMessage() for r in caplog.records) is warns


# ── _deploy_spec (pure) ─────────────────────────────────────────────────────────────


def test_deploy_spec_uses_digest(monkeypatch):
    monkeypatch.delenv("CLOUDRUN_FORWARD_ENV", raising=False)
    spec = _deploy_spec(
        agent_id="a1",
        name="Paper Reviewer",
        description="Reviews papers",
        handler_type="custom",
        config=_custom_config(),
        image_ref="sha256:deadbeef",
        service_account="agent-runtime@x.iam.gserviceaccount.com",
        invoker_member="serviceAccount:chat-app@x.iam.gserviceaccount.com",
        vpc_connector="chat-app-connector",
    )
    assert spec["service_id"] == "agent-paper-reviewer"
    assert spec["image"].endswith("/agenticnetwork-custom-paper-reviewer@sha256:deadbeef")
    assert spec["min_instances"] == 0
    assert spec["max_instances"] == 1
    assert spec["container_port"] == 8080
    assert spec["ingress"] == "internal-only"
    assert spec["allow_unauthenticated"] is False
    assert spec["service_account"] == "agent-runtime@x.iam.gserviceaccount.com"
    assert spec["invoker_member"] == "serviceAccount:chat-app@x.iam.gserviceaccount.com"
    assert spec["vpc_connector"] == "chat-app-connector"
    # The container binds the pinned Cloud Run port, not the DB-allocated one.
    assert '"port": 8080' in spec["env"]["AGENT_CONFIG_JSON"]
    assert spec["env"]["AGENT_PORT"] == "8080"


def test_deploy_spec_latest_fallback_when_no_digest():
    spec = _deploy_spec(
        agent_id="a1",
        name="Paper Reviewer",
        description="d",
        handler_type="custom",
        config=_custom_config(),
        image_ref=None,
        service_account=None,
        invoker_member=None,
        vpc_connector=None,
    )
    assert spec["image"].endswith("/agenticnetwork-custom-paper-reviewer:latest")


_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def test_deploy_spec_uses_the_commit_tag_when_no_digest():
    """Without a digest, deploy the image tagged with the approval's commit, not
    ':latest', which may hold another approval's build. A digest still wins."""
    common = dict(
        agent_id="a1", name="Paper Reviewer", description="d", handler_type="custom",
        config=_custom_config(), service_account=None, invoker_member=None,
        vpc_connector=None, build_commit=_COMMIT,
    )
    spec = _deploy_spec(image_ref=None, **common)
    assert spec["image"].endswith(f"/agenticnetwork-custom-paper-reviewer:{_COMMIT}")
    spec = _deploy_spec(image_ref="sha256:deadbeef", **common)
    assert spec["image"].endswith("/agenticnetwork-custom-paper-reviewer@sha256:deadbeef")


def test_deploy_spec_forwards_llm_env(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://vllm:8100/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-placeholder")
    monkeypatch.delenv("CLOUDRUN_FORWARD_ENV", raising=False)
    spec = _deploy_spec(
        agent_id="a1", name="X", description="d", handler_type="custom",
        config=_custom_config(), image_ref=None, service_account=None,
        invoker_member=None, vpc_connector=None,
    )
    assert spec["env"]["OPENAI_BASE_URL"] == "http://vllm:8100/v1"
    assert spec["env"]["OPENAI_API_KEY"] == "sk-placeholder"


def test_service_id_is_rfc1035_safe():
    assert _cloudrun_service_id("My Cool Agent!!") == "agent-my-cool-agent"
    assert _cloudrun_service_id("a" * 80).startswith("agent-")
    assert len(_cloudrun_service_id("a" * 80)) <= 63


# ── CloudRunBackend (fake client) ───────────────────────────────────────────────────


@_aio
async def test_spawn_deploys_by_digest_and_grants_invoker(monkeypatch):
    _cloud_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom(image_ref="sha256:cafe")
    aid = str(agent["id"])

    fake = _FakeCloudRunClient()
    backend = CloudRunBackend(client=fake)
    url = await backend.spawn(
        agent_id=aid, name="Paper Reviewer", description="Reviews papers",
        handler_type="custom", config=_custom_config(), port=19110,
    )

    assert url == "https://agent-paper-reviewer-abc123-uc.a.run.app"
    assert len(fake.created) == 1
    assert fake.created[0]["image"].endswith("@sha256:cafe")
    assert fake.created[0]["service_account"].startswith("agent-runtime@")
    # Planner SA granted run.invoker on the service.
    assert fake.invokers == [
        ("agent-paper-reviewer", "serviceAccount:chat-app@example-project.iam.gserviceaccount.com")
    ]
    await db.close_db()


@_aio
async def test_spawn_falls_back_to_latest_when_image_ref_none(monkeypatch):
    _cloud_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom(image_ref=None)
    aid = str(agent["id"])

    fake = _FakeCloudRunClient()
    backend = CloudRunBackend(client=fake)
    await backend.spawn(
        agent_id=aid, name="Paper Reviewer", description="d",
        handler_type="custom", config=_custom_config(), port=19110,
    )
    assert fake.created[0]["image"].endswith("agenticnetwork-custom-paper-reviewer:latest")
    await db.close_db()


@_aio
async def test_spawn_deploys_the_commit_tag_when_image_ref_none(monkeypatch):
    _cloud_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom(image_ref=None)
    aid = str(agent["id"])
    await db.update_agent(aid, build_commit=_COMMIT)

    fake = _FakeCloudRunClient()
    backend = CloudRunBackend(client=fake)
    await backend.spawn(
        agent_id=aid, name="Paper Reviewer", description="d",
        handler_type="custom", config=_custom_config(), port=19110,
    )
    assert fake.created[0]["image"].endswith(f"agenticnetwork-custom-paper-reviewer:{_COMMIT}")
    await db.close_db()


@_aio
async def test_is_alive_reads_ready_without_http(monkeypatch):
    _cloud_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom()
    aid = str(agent["id"])

    fake = _FakeCloudRunClient()
    backend = CloudRunBackend(client=fake)

    assert await backend.is_alive(aid) is False           # not deployed yet
    await backend.spawn(
        agent_id=aid, name="Paper Reviewer", description="d",
        handler_type="custom", config=_custom_config(), port=19110,
    )
    assert await backend.is_alive(aid) is True             # exists & Ready
    assert fake.http_calls == 0                            # never cold-started
    await db.close_db()


@_aio
async def test_stop_deletes_service(monkeypatch):
    _cloud_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom()
    aid = str(agent["id"])

    fake = _FakeCloudRunClient()
    backend = CloudRunBackend(client=fake)
    await backend.spawn(
        agent_id=aid, name="Paper Reviewer", description="d",
        handler_type="custom", config=_custom_config(), port=19110,
    )
    await backend.stop(aid)
    assert fake.deleted == ["agent-paper-reviewer"]
    assert await backend.is_alive(aid) is False
    await db.close_db()


@_aio
async def test_cloud_run_operations_wait_a_bounded_time(monkeypatch):
    """A deploy or delete holds the agent lock, so the client gives up on a Cloud
    Run operation after CLOUDRUN_OPERATION_TIMEOUT_SECONDS instead of waiting
    forever (google-api-core's default)."""
    pytest.importorskip("google.api_core.exceptions")
    monkeypatch.setenv("CLOUDRUN_OPERATION_TIMEOUT_SECONDS", "42")
    waits: list = []

    class _Operation:
        async def result(self, timeout=None):
            waits.append(timeout)
            return SimpleNamespace(uri="https://agent-x.a.run.app")

    class _Services:
        async def create_service(self, **_):
            return _Operation()

        async def delete_service(self, **_):
            return _Operation()

    client = _CloudRunClient.__new__(_CloudRunClient)
    client._parent = "projects/example/locations/us-central1"
    client._services = _Services()
    client._build_service = lambda spec: SimpleNamespace()

    assert await client.create_or_replace({"service_id": "agent-x"}) == "https://agent-x.a.run.app"
    await client.delete("agent-x")
    assert waits == [42.0, 42.0]


def test_make_backend_selects_cloudrun(monkeypatch):
    # No client is injected; construction must not import google-cloud-run.
    _cloud_env(monkeypatch)
    backend = make_backend()
    assert isinstance(backend, CloudRunBackend)


# ── Supervisor deploy trigger (image_ready → deploy → publish) ──────────────────────


@_aio
async def test_build_poll_deploys_and_publishes_run_app_url(monkeypatch):
    """On the cloud backend, _check_building_agents deploys inline and publishes
    the run.app URL (the cluster path instead defers to the host loop)."""
    _cloud_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPO", "example-org/agents")
    await _fresh_db()
    agent = await _make_custom(image_ref=None, status="building")

    async def yes(image, since=None):
        return True

    async def digest(image, tag="latest"):
        return "sha256:feed"

    monkeypatch.setattr(sup_mod, "_image_exists_on_dockerhub", yes)
    monkeypatch.setattr(sup_mod, "_image_digest_on_dockerhub", digest)

    fake = _FakeCloudRunClient()
    publisher = _FakePublisher()
    sup = Supervisor(backend=CloudRunBackend(client=fake), publisher=publisher)
    await sup._check_building_agents()

    got = await db.get_agent(agent["id"])
    assert got["status"] == "live"
    assert got["image_ref"] == "sha256:feed"
    # Deployed by digest (captured just before deploy) and published at run.app.
    assert fake.created and fake.created[0]["image"].endswith("@sha256:feed")
    assert publisher.published
    advertise = publisher.published[0]["advertise_url"]
    assert advertise == "https://agent-paper-reviewer-abc123-uc.a.run.app"
    assert got["endpoint_url"] == advertise
    # Published once, decoupled: the run.app card is recorded (ads_cid persisted)
    # and nothing was unpublished during the deploy.
    assert len(publisher.published) == 1
    assert got["ads_cid"] == "fake-cid"
    assert publisher.unpublished == []
    await db.close_db()


@_aio
async def test_build_poll_marks_failed_when_deploy_raises(monkeypatch):
    _cloud_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPO", "example-org/agents")
    await _fresh_db()
    agent = await _make_custom(image_ref="sha256:x", status="building")

    async def yes(image, since=None):
        return True

    async def digest(image, tag="latest"):
        return "sha256:x"

    monkeypatch.setattr(sup_mod, "_image_exists_on_dockerhub", yes)
    monkeypatch.setattr(sup_mod, "_image_digest_on_dockerhub", digest)

    class _Boom(_FakeCloudRunClient):
        async def create_or_replace(self, spec):
            raise RuntimeError("cloud run 500")

    sup = Supervisor(backend=CloudRunBackend(client=_Boom()), publisher=_FakePublisher())
    await sup._check_building_agents()

    got = await db.get_agent(agent["id"])
    assert got["status"] == "failed"
    await db.close_db()


# ── Step 5: decoupled publish (publish once, unpublish only on explicit stop) ───────


async def _deploy_to_live(monkeypatch, fake, publisher):
    """Drive a custom agent through the build poll to 'live' on the cloud backend.
    Returns (supervisor, agent_row)."""
    _cloud_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPO", "example-org/agents")
    await _fresh_db()
    agent = await _make_custom(image_ref="sha256:feed", status="building")

    async def yes(image, since=None):
        return True

    async def digest(image, tag="latest"):
        return "sha256:feed"

    monkeypatch.setattr(sup_mod, "_image_exists_on_dockerhub", yes)
    monkeypatch.setattr(sup_mod, "_image_digest_on_dockerhub", digest)
    sup = Supervisor(backend=CloudRunBackend(client=fake), publisher=publisher)
    await sup._check_building_agents()
    return sup, agent


@_aio
async def test_scale_to_zero_keeps_card_published(monkeypatch):
    """A live cloud agent that has scaled to zero must stay published — the
    supervisor never unpublishes based on liveness (only on explicit stop). The
    health loop is a no-op for it (supports_health_restart=False), so a full
    health pass leaves the card and 'live' status untouched."""
    fake = _FakeCloudRunClient()
    publisher = _FakePublisher()
    sup, agent = await _deploy_to_live(monkeypatch, fake, publisher)

    # Sanity: deployed + published once, service exists (would be at 0 instances live).
    assert (await db.get_agent(agent["id"]))["status"] == "live"
    assert len(publisher.published) == 1

    # A health pass (the scale-to-zero window) must not touch it.
    await sup._check_all_live_agents()

    got = await db.get_agent(agent["id"])
    assert got["status"] == "live"                 # still live
    assert got["ads_cid"] == "fake-cid"            # card persists
    assert publisher.unpublished == []             # never unpublished on idle
    assert fake.deleted == []                      # service never torn down
    await db.close_db()


@_aio
async def test_stop_and_unpublish_is_the_only_teardown(monkeypatch):
    """Explicit stop is the sole path that unpublishes the card AND deletes the
    Cloud Run service — the decoupled counterpart to 'publish once on deploy'."""
    fake = _FakeCloudRunClient()
    publisher = _FakePublisher()
    sup, agent = await _deploy_to_live(monkeypatch, fake, publisher)
    aid = str(agent["id"])

    await sup.stop_and_unpublish(aid)

    got = await db.get_agent(agent["id"])
    assert got["status"] == "stopped"
    assert got["ads_cid"] is None
    assert publisher.unpublished == ["fake-cid"]   # card unpublished on stop
    assert fake.deleted == ["agent-paper-reviewer"]  # service deleted on stop
    await db.close_db()


# ── Step 6: health-loop skip + recovery reconcile (no re-spawn blindly) ─────────────


@_aio
async def test_health_loop_never_probes_cloud_agent(monkeypatch):
    """The health loop must skip Cloud Run agents entirely (supports_health_restart=
    False) — probing would cold-start a min=0 service. Cloud Run self-heals."""
    fake = _FakeCloudRunClient()
    publisher = _FakePublisher()
    sup, agent = await _deploy_to_live(monkeypatch, fake, publisher)
    fake.get_calls = 0  # ignore any deploy-time bookkeeping

    await sup._check_all_live_agents()

    assert fake.get_calls == 0                     # never probed (early-returned)
    assert fake.http_calls == 0                    # and certainly no HTTP
    assert (await db.get_agent(agent["id"]))["status"] == "live"
    await db.close_db()


@_aio
async def test_recovery_confirms_live_agent_via_api(monkeypatch):
    """On restart, a 'live' cloud agent whose service still exists is confirmed via
    the Admin API and left alone — NOT re-spawned, and NOT republished (the
    decoupled card persists)."""
    fake = _FakeCloudRunClient()
    publisher = _FakePublisher()
    await _deploy_to_live(monkeypatch, fake, publisher)
    deploys_before = len(fake.created)
    publishes_before = len(publisher.published)

    # Simulate a reg-service restart: fresh Supervisor, but Cloud Run (fake client)
    # and ADS (publisher) persist.
    sup2 = Supervisor(backend=CloudRunBackend(client=fake), publisher=publisher)
    await sup2.recover_live_agents()

    assert fake.get_calls >= 1                      # reconciled via the API...
    assert len(fake.created) == deploys_before      # ...not re-deployed
    assert len(publisher.published) == publishes_before  # ...not republished
    assert publisher.unpublished == []
    await db.close_db()


@_aio
async def test_recovery_redeploys_when_service_vanished(monkeypatch):
    """A 'live' row whose Cloud Run service is gone gets its stale card dropped and
    is redeployed + republished."""
    fake = _FakeCloudRunClient()
    publisher = _FakePublisher()
    _sup, agent = await _deploy_to_live(monkeypatch, fake, publisher)
    deploys_before = len(fake.created)
    fake._services.clear()  # the service disappeared out-of-band

    sup2 = Supervisor(backend=CloudRunBackend(client=fake), publisher=publisher)
    await sup2.recover_live_agents()

    got = await db.get_agent(agent["id"])
    assert got["status"] == "live"                  # back to live
    assert len(fake.created) == deploys_before + 1  # redeployed
    assert publisher.unpublished == ["fake-cid"]    # stale card dropped first
    await db.close_db()


@_aio
async def test_recovery_deploys_stranded_image_ready(monkeypatch):
    """An 'image_ready' row (deploy never fired before a restart — the Step-4 gap)
    is deployed on recovery."""
    _cloud_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom(image_ref="sha256:feed", status="image_ready")

    fake = _FakeCloudRunClient()
    publisher = _FakePublisher()
    sup = Supervisor(backend=CloudRunBackend(client=fake), publisher=publisher)
    await sup.recover_live_agents()

    got = await db.get_agent(agent["id"])
    assert got["status"] == "live"                  # deployed
    assert len(fake.created) == 1
    assert len(publisher.published) == 1
    assert publisher.published[0]["advertise_url"].endswith(".run.app")
    await db.close_db()


# ── Step 8: resource-requirements card extension (right-sizing + GKE seam) ──────────


def test_resource_requirements_validation():
    import pytest as _pytest
    # Valid hints coerce/clean.
    r = ResourceRequirements(cpu=" 2 ", memory="2Gi", concurrency=4, max_instances=10)
    assert r.cpu == "2" and r.memory == "2Gi" and r.concurrency == 4 and r.max_instances == 10
    # Bad formats / out-of-range rejected.
    for bad in (
        {"cpu": "two"},
        {"memory": "2 gigs"},
        {"concurrency": 0},
        {"max_instances": 0},
    ):
        with _pytest.raises(Exception):
            ResourceRequirements(**bad)


def test_deploy_spec_maps_resource_hints():
    spec = _deploy_spec(
        agent_id="a1", name="Paper Reviewer", description="d", handler_type="custom",
        config=_custom_config(), image_ref="sha256:x",
        service_account=None, invoker_member=None, vpc_connector=None,
        resource={"cpu": "2", "memory": "2Gi", "concurrency": 4, "max_instances": 10, "gpu": False},
    )
    assert spec["cpu"] == "2"
    assert spec["memory"] == "2Gi"
    assert spec["concurrency"] == 4
    assert spec["max_instances"] == 10
    assert spec["min_instances"] == 0


def test_deploy_spec_no_resource_uses_defaults_and_omits_concurrency():
    spec = _deploy_spec(
        agent_id="a1", name="Paper Reviewer", description="d", handler_type="custom",
        config=_custom_config(), image_ref="sha256:x",
        service_account=None, invoker_member=None, vpc_connector=None,
        resource=None,
    )
    assert spec["cpu"] == "1"
    assert spec["memory"] == "512Mi"
    assert spec["max_instances"] == 1
    assert spec["concurrency"] is None      # unset → leave Cloud Run's default


def test_build_agent_card_carries_resource_extension():
    card = _build_agent_card(
        "a1", "Paper Reviewer", "d",
        [{"name": "nlp/text-summarization", "id": 1}], 8080,
        resources={"cpu": "2", "memory": "2Gi", "gpu": False},
    )
    exts = card.capabilities.extensions or []
    uris = [e.uri for e in exts]
    assert RESOURCE_REQUIREMENTS_EXTENSION_URI in uris
    res_ext = next(e for e in exts if e.uri == RESOURCE_REQUIREMENTS_EXTENSION_URI)
    assert res_ext.params["cpu"] == "2" and res_ext.params["memory"] == "2Gi"


def test_build_agent_card_omits_extension_when_no_resources():
    card = _build_agent_card(
        "a1", "Paper Reviewer", "d",
        [{"name": "nlp/text-summarization", "id": 1}], 8080,
    )
    assert card.capabilities.extensions is None


@_aio
async def test_spawn_uses_resource_hints(monkeypatch):
    _cloud_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom(
        image_ref="sha256:cafe",
        resources={"cpu": "2", "memory": "2Gi", "concurrency": 4, "max_instances": 8, "gpu": False},
    )
    fake = _FakeCloudRunClient()
    backend = CloudRunBackend(client=fake)
    await backend.spawn(
        agent_id=str(agent["id"]), name="Paper Reviewer", description="d",
        handler_type="custom", config=_custom_config(), port=19110,
    )
    spec = fake.created[0]
    assert spec["cpu"] == "2"
    assert spec["memory"] == "2Gi"
    assert spec["concurrency"] == 4
    assert spec["max_instances"] == 8
    await db.close_db()


@_aio
async def test_spawn_rejects_gpu_request(monkeypatch):
    """A GPU request is the GKEBackend seam: Cloud Run can't schedule GPUs, so
    spawn must fail loudly rather than deploy an agent that can't run."""
    _cloud_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom(resources={"gpu": True, "memory": "4Gi"})
    fake = _FakeCloudRunClient()
    backend = CloudRunBackend(client=fake)
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="GPU"):
        await backend.spawn(
            agent_id=str(agent["id"]), name="Paper Reviewer", description="d",
            handler_type="custom", config=_custom_config(), port=19110,
        )
    assert fake.created == []   # never deployed
    await db.close_db()


@_aio
async def test_resources_persist_and_reach_deploy_and_card(monkeypatch):
    """End-to-end: resources declared at registration persist, drive the deploy
    spec, and surface on the published card."""
    _cloud_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPO", "example-org/agents")
    await _fresh_db()
    agent = await _make_custom(
        image_ref=None, status="building",
        resources={"cpu": "2", "memory": "2Gi", "concurrency": 2, "max_instances": 5, "gpu": False},
    )

    async def yes(image, since=None):
        return True

    async def digest(image, tag="latest"):
        return "sha256:feed"

    monkeypatch.setattr(sup_mod, "_image_exists_on_dockerhub", yes)
    monkeypatch.setattr(sup_mod, "_image_digest_on_dockerhub", digest)
    fake = _FakeCloudRunClient()
    publisher = _FakePublisher()
    sup = Supervisor(backend=CloudRunBackend(client=fake), publisher=publisher)
    await sup._check_building_agents()

    # Persisted on the row.
    assert (await db.get_agent(agent["id"]))["resources"]["cpu"] == "2"
    # Drove the deploy.
    assert fake.created[0]["cpu"] == "2" and fake.created[0]["concurrency"] == 2
    # Surfaced on the published card.
    card = publisher.published[0]["card"]
    uris = [e.uri for e in (card.capabilities.extensions or [])]
    assert RESOURCE_REQUIREMENTS_EXTENSION_URI in uris
    await db.close_db()


@_aio
async def test_spawn_handles_jsonb_string_resources(monkeypatch):
    """Regression: asyncpg returns the resources JSONB column as a *string*, but
    fake_db returns a dict. spawn must deserialize the string (the real-DB deploy
    failed with \"'str' object has no attribute 'get'\" before this)."""
    import json as _json
    _cloud_env(monkeypatch)
    await _fresh_db()
    agent = await _make_custom(image_ref="sha256:x")
    aid = str(agent["id"])

    row = dict(await db.get_agent(aid))
    row["resources"] = _json.dumps({"cpu": "2", "memory": "2Gi", "concurrency": 3, "gpu": False})

    async def fake_get_agent(agent_id):
        return row

    monkeypatch.setattr(db, "get_agent", fake_get_agent)
    fake = _FakeCloudRunClient()
    backend = CloudRunBackend(client=fake)
    await backend.spawn(
        agent_id=aid, name="Paper Reviewer", description="d",
        handler_type="custom", config=_custom_config(), port=19110,
    )
    assert fake.created[0]["cpu"] == "2"
    assert fake.created[0]["concurrency"] == 3
    await db.close_db()


@_aio
async def test_recovery_retries_failed_agent_with_image(monkeypatch):
    """A 'failed' row that has an image_ref (built, but the deploy step failed) is
    retried on recovery; a 'failed' row without one is left alone."""
    _cloud_env(monkeypatch)
    await _fresh_db()
    built = await _make_custom(image_ref="sha256:beef", status="failed")
    unbuilt = await db.create_agent(
        name="No Image", description="d", handler_type="custom",
        oasf_skills=[{"name": "nlp/text-summarization", "id": 1}],
        config={"class_name": "H", "python_version": "3.12", "requirements": [], "extra": {}},
        approved=True,
    )
    await db.update_agent(unbuilt["id"], status="failed")  # no image_ref

    fake = _FakeCloudRunClient()
    sup = Supervisor(backend=CloudRunBackend(client=fake), publisher=_FakePublisher())
    await sup.recover_live_agents()

    assert (await db.get_agent(built["id"]))["status"] == "live"       # retried
    assert (await db.get_agent(unbuilt["id"]))["status"] == "failed"   # left alone
    assert len(fake.created) == 1
    await db.close_db()


@_aio
async def test_startup_purge_spares_failed_with_image(monkeypatch):
    """The startup purge deletes 'failed' agents that never built, but spares those
    with an image_ref (built, deploy-failed) so recovery can retry them — otherwise
    the failed-retry in _recover_cloud_agents is dead code (the purge runs first)."""
    from registration_service.fake_db import init_fake_db
    _cloud_env(monkeypatch)
    await _fresh_db()
    built = await _make_custom(image_ref="sha256:x", status="failed")
    unbuilt = await db.create_agent(
        name="No Image", description="d", handler_type="custom",
        oasf_skills=[{"name": "nlp/text-summarization", "id": 1}],
        config={"class_name": "H", "python_version": "3.12", "requirements": [], "extra": {}},
        approved=True,
    )
    await db.update_agent(unbuilt["id"], status="failed")  # failed, no image_ref

    await init_fake_db()  # re-run the startup purge over the populated store

    assert await db.get_agent(built["id"]) is not None    # built → spared
    assert await db.get_agent(unbuilt["id"]) is None       # never built → purged
    await db.close_db()
