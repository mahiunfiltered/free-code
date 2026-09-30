"""HTTP contract for the local chat UI."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from free_claude_code.api.chat_routes import summarize_endpoints
from free_claude_code.cli.managed import interactive
from free_claude_code.config.settings import Settings
from free_claude_code.core.json_types import JsonObject
from tests.api.support import (
    create_test_app,
    provider_manager_for_app,
    runtime_for_app,
)
from tests.workbench.test_coordinator import FIX, fake_claude, make_repo, pytest_profile

LOCAL = "http://127.0.0.1:8082"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return TestClient(create_test_app(), client=("127.0.0.1", 50000), base_url=LOCAL)


def test_page_and_assets_served_locally(client: TestClient):
    page = client.get("/chat")
    assert page.status_code == 200 and "chat.js" in page.text
    assert client.get("/chat/assets/chat.js").status_code == 200
    assert client.get("/chat/assets/chat.css").status_code == 200
    assert client.get("/chat/assets/index.html").status_code == 404


def test_chat_is_loopback_only():
    remote = TestClient(
        create_test_app(), client=("203.0.113.10", 50000), base_url=LOCAL
    )
    assert remote.get("/chat").status_code == 403
    assert remote.get("/chat/api/sessions").status_code == 403
    local = TestClient(create_test_app(), client=("127.0.0.1", 50000), base_url=LOCAL)
    cross_site = local.post(
        "/chat/api/live", json={"cwd": "."}, headers={"Origin": "https://evil.example"}
    )
    assert cross_site.status_code == 403


def test_sessions_and_transcript_crud(client: TestClient, tmp_path: Path):
    sid = "dddddddd-0000-0000-0000-000000000001"
    path = tmp_path / "projects" / "p" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"type": "user", "cwd": "/w", "message": {"content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    data = client.get("/chat/api/sessions").json()
    assert data["transcripts"][0]["session_id"] == sid and data["live"] == []
    assert (
        client.get(f"/chat/api/transcripts/{sid}").json()["events"][0]["type"]
        == "fcc_user"
    )
    assert (
        client.patch(
            f"/chat/api/transcripts/{sid}", json={"title": "Renamed"}
        ).status_code
        == 200
    )
    assert (
        client.get("/chat/api/sessions").json()["transcripts"][0]["title"] == "Renamed"
    )
    assert (
        client.patch(f"/chat/api/transcripts/{sid}", json={"title": ""}).status_code
        == 422
    )
    assert client.delete(f"/chat/api/transcripts/{sid}").status_code == 200
    assert client.get(f"/chat/api/transcripts/{sid}").status_code == 404
    assert client.delete(f"/chat/api/transcripts/{sid}").status_code == 404
    assert (
        client.patch(
            "/chat/api/transcripts/nope-nope-1", json={"title": "x"}
        ).status_code
        == 404
    )


def test_live_endpoints_validate_input(client: TestClient, tmp_path: Path):
    missing = client.post("/chat/api/live", json={"cwd": str(tmp_path / "missing")})
    assert missing.status_code == 400
    assert "does not exist" in missing.json()["error"]["message"]
    for path in ("/chat/api/live/nope/messages", "/chat/api/live/nope/permissions/r1"):
        body = (
            {"content": "x"}
            if path.endswith("messages")
            else {"decision": {"behavior": "allow"}}
        )
        assert client.post(path, json=body).status_code == 404
    assert client.get("/chat/api/live/nope/events").status_code == 404
    assert client.delete("/chat/api/live/nope").status_code == 404
    assert (
        client.post(
            "/chat/api/live/nope/control", json={"request": {"subtype": "initialize"}}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/chat/api/live/nope/permissions/r1",
            json={"decision": {"behavior": "maybe"}},
        ).status_code
        == 400
    )


def test_file_search_for_mentions(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "no-such-git-dir"))
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "Main.py").write_text("x", encoding="utf-8")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "main.js").write_text("x", encoding="utf-8")

    found = client.get("/chat/api/files", params={"cwd": str(project), "q": "main"})
    assert found.status_code == 200
    assert found.json() == {"files": ["src/Main.py"]}
    everything = client.get("/chat/api/files", params={"cwd": str(project)})
    assert everything.json() == {"files": ["src/Main.py"]}

    missing = client.get(
        "/chat/api/files", params={"cwd": str(tmp_path / "missing"), "q": ""}
    )
    assert missing.status_code == 400
    assert "does not exist" in missing.json()["error"]["message"]
    assert client.get("/chat/api/files").status_code == 422


def test_control_allowlist_includes_sdk_introspection(client: TestClient):
    for subtype in ("get_context_usage", "set_max_thinking_tokens", "mcp_status"):
        response = client.post(
            "/chat/api/live/nope/control", json={"request": {"subtype": subtype}}
        )
        assert response.status_code == 404  # allowed through, session unknown


def test_models_lists_only_configured_refs_as_gateway_ids(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = Settings(
        model="nvidia_nim/nvidia/nemotron",
        model_sonnet="nvidia_nim/nvidia/nemotron",
        model_fallbacks=("groq/vendor/fallback",),
        chat_models=("nvidia_nim/moonshotai/kimi", "nvidia_nim/z-ai/glm"),
        nvidia_nim_api_key="nvapi-test",
        groq_api_keys="a=gsk-test",
    )
    app = create_test_app(settings)
    no_thinking = {("nvidia_nim", "z-ai/glm")}
    monkeypatch.setattr(
        provider_manager_for_app(app),
        "cached_model_supports_thinking",
        lambda provider_id, model_id: (
            False if (provider_id, model_id) in no_thinking else None
        ),
    )
    body = (
        TestClient(app, client=("127.0.0.1", 50000), base_url=LOCAL)
        .get("/chat/api/models")
        .json()
    )

    assert body == {
        "models": [
            {
                "value": "anthropic/nvidia_nim/moonshotai/kimi",
                "label": "nvidia_nim/moonshotai/kimi",
                "provider": "nvidia_nim",
                "default": False,
            },
            {
                "value": "claude-3-freecc-no-thinking/nvidia_nim/z-ai/glm",
                "label": "nvidia_nim/z-ai/glm",
                "provider": "nvidia_nim",
                "default": False,
            },
            {
                "value": "anthropic/nvidia_nim/nvidia/nemotron",
                "label": "nvidia_nim/nvidia/nemotron",
                "provider": "nvidia_nim",
                "default": True,
            },
            {
                "value": "anthropic/groq/vendor/fallback",
                "label": "groq/vendor/fallback",
                "provider": "groq",
                "default": False,
            },
        ],
        "default": "anthropic/nvidia_nim/nvidia/nemotron",
    }


def test_models_default_only_model():
    app = create_test_app(Settings(nvidia_nim_api_key="nvapi-test"))
    client = TestClient(app, client=("127.0.0.1", 50000), base_url=LOCAL)
    body = client.get("/chat/api/models").json()
    assert [model["default"] for model in body["models"]] == [True]
    assert body["default"] == body["models"][0]["value"]
    assert body["default"].startswith("anthropic/")


def test_models_hide_providers_without_keys():
    settings = Settings(
        model="nvidia_nim/nvidia/nemotron",
        chat_models=("groq/vendor/fast", "ollama/llama3"),
    )
    client = TestClient(
        create_test_app(settings), client=("127.0.0.1", 50000), base_url=LOCAL
    )
    body = client.get("/chat/api/models").json()
    # Key-based providers without a key are hidden; local providers need none.
    assert [model["label"] for model in body["models"]] == ["ollama/llama3"]
    assert body["default"] is None


def test_dirs_lists_visible_subfolders_sorted(client: TestClient, tmp_path: Path):
    root = tmp_path / "root"
    for name in ("beta", "Alpha", ".hidden", "gamma"):
        (root / name).mkdir(parents=True)
    (root / "file.txt").write_text("x", encoding="utf-8")
    if sys.platform == "win32":
        subprocess.run(["attrib", "+h", str(root / "gamma")], check=True)

    body = client.get("/chat/api/dirs", params={"path": str(root)}).json()

    visible = (
        ["Alpha", "beta"] if sys.platform == "win32" else ["Alpha", "beta", "gamma"]
    )
    assert body["path"] == str(root)
    assert body["parent"] == str(tmp_path)
    assert body["dirs"] == [{"name": n, "path": str(root / n)} for n in visible]
    if sys.platform == "win32":
        assert "C:\\" in body["roots"]
    else:
        assert "roots" not in body


def test_dirs_defaults_to_home_and_root_has_no_parent(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert client.get("/chat/api/dirs").json()["path"] == str(tmp_path)

    anchor = Path(tmp_path.anchor)
    top = client.get("/chat/api/dirs", params={"path": str(anchor)}).json()
    assert top["path"] == str(anchor) and top["parent"] is None


def test_dirs_rejects_missing_folder(client: TestClient, tmp_path: Path):
    for target in (tmp_path / "missing", tmp_path / "file.txt"):
        if target.suffix:
            target.write_text("x", encoding="utf-8")
        response = client.get("/chat/api/dirs", params={"path": str(target)})
        assert response.status_code == 400
        assert "does not exist" in response.json()["error"]["message"]


def test_chat_rejects_other_origins_and_rebinding_hosts():
    local = TestClient(create_test_app(), client=("127.0.0.1", 50000), base_url=LOCAL)
    assert local.get("/chat/api/sessions", headers={"Origin": LOCAL}).status_code == 200
    other_port = local.get(
        "/chat/api/sessions", headers={"Origin": "http://127.0.0.1:3000"}
    )
    assert other_port.status_code == 403
    rebinding = TestClient(
        create_test_app(), client=("127.0.0.1", 50000), base_url="http://evil.example"
    )
    assert rebinding.get("/chat/api/sessions").status_code == 403


def test_control_rejects_unknown_permission_mode():
    client = TestClient(create_test_app(), client=("127.0.0.1", 50000), base_url=LOCAL)
    response = client.post(
        "/chat/api/live/nope/control",
        json={"request": {"subtype": "set_permission_mode", "mode": "yolo"}},
    )
    assert response.status_code == 400


# ----- verified tasks, policy presets, endpoint summary ----------------------


def test_policy_presets_route_lists_presets(client: TestClient):
    body = client.get("/chat/api/policy/presets").json()
    ids = [p["id"] for p in body["presets"]]
    assert ids == ["restricted", "workspace", "privileged"]
    assert body["presets"][1]["permission_mode"] == "acceptEdits"


def test_endpoint_summary_counts_keys_per_provider(client: TestClient):
    assert client.get("/chat/api/endpoints/summary").json() == {"providers": []}
    health: JsonObject = {
        "endpoints": [
            {"provider_id": "nim", "circuit": "HEALTHY", "available": True},
            {"provider_id": "nim", "circuit": "OPEN", "available": False},
            {
                "provider_id": "nim",
                "circuit": "HEALTHY",
                "available": True,
                "cooldown_remaining_s": 12.0,
            },
            {"provider_id": "groq", "circuit": "HALF_OPEN", "available": True},
        ]
    }
    assert summarize_endpoints(health) == [
        {"provider_id": "nim", "healthy": 1, "total": 3, "cooling": 1, "open": 1},
        {"provider_id": "groq", "healthy": 1, "total": 1, "cooling": 0, "open": 0},
    ]
    assert summarize_endpoints({}) == []


def test_task_routes_404_for_unknown_tasks(client: TestClient):
    assert client.get("/chat/api/tasks/nope").status_code == 404
    assert client.get("/chat/api/tasks/nope/evidence").status_code == 404
    assert client.post("/chat/api/tasks/nope/revert").status_code == 404
    assert client.post("/chat/api/tasks/nope/cancel").status_code == 404
    assert client.post("/chat/api/tasks/nope/resume").status_code == 404
    assert client.post("/chat/api/tasks/nope/resume", json={}).status_code == 404
    clarify = client.post("/chat/api/tasks/nope/clarify", json={"answers": "x"})
    assert clarify.status_code == 404
    assert (
        client.post("/chat/api/tasks/nope/clarify", json={"answers": ""}).status_code
        == 422
    )
    assert client.get("/chat/api/sessions/nope/tasks").json() == {"tasks": []}
    bad_mode = client.post(
        "/chat/api/live/nope/messages", json={"content": "x", "mode": "yolo"}
    )
    assert bad_mode.status_code == 422
    unknown_live = client.post(
        "/chat/api/live/nope/messages", json={"content": "x", "mode": "verified"}
    )
    assert unknown_live.status_code == 404


def test_start_rejects_bad_preset_and_budget(client: TestClient, tmp_path: Path):
    preset = client.post(
        "/chat/api/live", json={"cwd": str(tmp_path), "policy_preset": "yolo"}
    )
    assert preset.status_code == 422
    budget = client.post(
        "/chat/api/live", json={"cwd": str(tmp_path), "budget": {"max_turns": 0}}
    )
    assert budget.status_code == 422


def test_start_without_model_uses_fcc_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without an explicit --model, Claude Code would pick the model pinned in the
    # user's own ~/.claude/settings.json instead of FCC's MODEL.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    fake = fake_claude(tmp_path)
    monkeypatch.setattr(interactive.shutil, "which", lambda _name: fake)
    settings = Settings(port=1, model="ollama/pick")
    with TestClient(
        create_test_app(settings), client=("127.0.0.1", 50000), base_url=LOCAL
    ) as client:
        started = client.post("/chat/api/live", json={"cwd": str(tmp_path)}).json()
        chosen = client.post(
            "/chat/api/live", json={"cwd": str(tmp_path), "model": "anthropic/x/y"}
        ).json()
        for live in (started, chosen):
            client.delete(f"/chat/api/live/{live['live_id']}")
    assert started["model"] == "anthropic/ollama/pick"
    assert chosen["model"] == "anthropic/x/y"


def test_verified_task_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    fake = fake_claude(tmp_path)
    monkeypatch.setattr(interactive.shutil, "which", lambda _name: fake)
    repo = make_repo(tmp_path)
    # Port 1: helper-model calls fail fast and degrade (never hit a real server).
    app = create_test_app(Settings(port=1))
    runtime_for_app(app).workbench.coordinator.options.profile = pytest_profile(repo)

    with TestClient(app, client=("127.0.0.1", 50000), base_url=LOCAL) as client:
        started = client.post(
            "/chat/api/live",
            json={
                "cwd": str(repo),
                "policy_preset": "workspace",
                "budget": {"max_turns": 40, "max_output_tokens": 100000},
            },
        ).json()
        assert started["policy_preset"] == "workspace"
        assert started["permission_mode"] == "acceptEdits"
        assert started["budget"]["max_turns"] == 40
        assert started["usage"]["turns"] == 0
        assert started["preset_permission_mode"] == "acceptEdits"
        live_id = started["live_id"]
        looser = client.post(
            f"/chat/api/live/{live_id}/control",
            json={"request": {"subtype": "set_permission_mode", "mode": "auto"}},
        )
        assert looser.status_code == 400 and "stricter" in looser.text

        sent = client.post(
            f"/chat/api/live/{live_id}/messages",
            json={
                "content": f"Fix add in calc.py so the tests pass\n{FIX}",
                "mode": "verified",
            },
        ).json()
        task_id = sent["task_id"]
        assert sent["ok"] is True and task_id

        deadline = time.monotonic() + 60
        detail = client.get(f"/chat/api/tasks/{task_id}").json()
        while detail["task"]["status"] not in (
            "VERIFIED",
            "FAILED",
            "RECOVERY_REQUIRED",
        ):
            assert time.monotonic() < deadline, detail["task"]
            time.sleep(0.1)
            detail = client.get(f"/chat/api/tasks/{task_id}").json()
        assert detail["task"]["status"] == "VERIFIED"
        assert detail["task"]["mode"] == "verified"
        assert detail["evidence"][0]["disposition"] == "VERIFIED"
        assert any(e["type"] == "task.status" for e in detail["events"])

        markdown = client.get(f"/chat/api/tasks/{task_id}/evidence")
        assert markdown.headers["content-type"].startswith("text/markdown")
        assert "Evidence package: VERIFIED" in markdown.text
        as_json = client.get(
            f"/chat/api/tasks/{task_id}/evidence", params={"format": "json"}
        ).json()
        assert as_json["disposition"] == "VERIFIED"

        session_id = detail["task"]["session_id"]
        [listed] = client.get(f"/chat/api/sessions/{session_id}/tasks").json()["tasks"]
        assert listed["id"] == listed["task_id"] == task_id
        assert listed["evidence"][0]["disposition"] == "VERIFIED"

        normal = client.post(
            f"/chat/api/live/{live_id}/messages", json={"content": "thanks"}
        ).json()
        assert normal == {"ok": True, "task_id": None}

        resumed = client.post(f"/chat/api/tasks/{task_id}/resume", json={})
        assert resumed.status_code == 400 and "needs your attention" in resumed.text

        reverted = client.post(f"/chat/api/tasks/{task_id}/revert").json()
        assert reverted == {"reverted": ["calc.py"]}
        assert client.post(f"/chat/api/tasks/{task_id}/cancel").json() == {"ok": True}
        client.delete(f"/chat/api/live/{live_id}")


def test_parallel_node_permission_answered_via_node_live_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    fake = fake_claude(tmp_path)
    monkeypatch.setattr(interactive.shutil, "which", lambda _name: fake)
    repo = make_repo(tmp_path)
    app = create_test_app(Settings(port=1))
    workbench = runtime_for_app(app).workbench
    workbench.coordinator.options.profile = pytest_profile(repo)

    with TestClient(app, client=("127.0.0.1", 50000), base_url=LOCAL) as client:
        live_id = client.post("/chat/api/live", json={"cwd": str(repo)}).json()[
            "live_id"
        ]
        task_id = client.post(
            f"/chat/api/live/{live_id}/messages",
            json={
                "content": f"Fix add in calc.py so the tests pass\n{FIX}\nASK Bash",
                "mode": "parallel",
                "strategy": "economy",
            },
        ).json()["task_id"]

        # The node session lives in the same registry as the chat.
        deadline = time.monotonic() + 60
        node_ids: list[str] = []
        while not node_ids:
            assert time.monotonic() < deadline, "node session never started"
            time.sleep(0.1)
            live = client.get("/chat/api/sessions").json()["live"]
            node_ids = [s["live_id"] for s in live if s["live_id"] != live_id]
        [node_id] = node_ids
        url = f"/chat/api/live/{node_id}/permissions/perm_Bash"
        decision = {"decision": {"behavior": "allow", "updatedInput": {}}}
        cross_site = client.post(
            url, json=decision, headers={"Origin": "https://evil.example"}
        )
        assert cross_site.status_code == 403
        answered = client.post(url, json=decision)
        while answered.status_code == 400:  # not asked yet
            assert time.monotonic() < deadline, answered.text
            time.sleep(0.1)
            answered = client.post(url, json=decision)
        assert answered.json() == {"ok": True}

        status = client.get(f"/chat/api/tasks/{task_id}").json()["task"]["status"]
        while status not in ("VERIFIED", "FAILED", "RECOVERY_REQUIRED"):
            assert time.monotonic() < deadline, status
            time.sleep(0.1)
            status = client.get(f"/chat/api/tasks/{task_id}").json()["task"]["status"]
        assert status == "VERIFIED"
        [record] = workbench.audit.recent(action="permission.decision")
        assert (record.resource, record.decision) == ("Bash", "allow")
        assert json.loads(record.payload_json)["live_id"] == node_id
        client.delete(f"/chat/api/live/{live_id}")


def test_project_reports_git_branch_and_dirty(client: TestClient, tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert client.get("/chat/api/project", params={"cwd": str(plain)}).json() == {
        "git": False,
        "branch": None,
        "dirty": False,
    }
    repo = make_repo(tmp_path)
    clean = client.get("/chat/api/project", params={"cwd": str(repo)}).json()
    assert clean == {"git": True, "branch": "main", "dirty": False}
    (repo / "new.txt").write_text("x", "utf-8")
    dirty = client.get("/chat/api/project", params={"cwd": str(repo / "tests")})
    assert dirty.json() == {"git": True, "branch": "main", "dirty": True}
    missing = client.get("/chat/api/project", params={"cwd": str(tmp_path / "nope")})
    assert missing.status_code == 400


def test_start_uses_preset_mode_unless_a_stricter_one_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    fake = fake_claude(tmp_path)
    monkeypatch.setattr(interactive.shutil, "which", lambda _name: fake)
    app = create_test_app()
    with TestClient(app, client=("127.0.0.1", 50000), base_url=LOCAL) as client:
        cases = [
            ("restricted", None, "dontAsk"),
            ("restricted", "bypassPermissions", "dontAsk"),
            ("restricted", "plan", "plan"),
            ("workspace", "default", "default"),
            ("privileged", None, "bypassPermissions"),
            (None, None, "default"),
            (None, "dontAsk", "dontAsk"),
        ]
        for preset, mode, expected in cases:
            started = client.post(
                "/chat/api/live",
                json={
                    "cwd": str(tmp_path),
                    "policy_preset": preset,
                    "permission_mode": mode,
                },
            ).json()
            assert started["permission_mode"] == expected, (preset, mode)
            client.delete(f"/chat/api/live/{started['live_id']}")


def test_ultra_task_over_http_runs_directly_when_analysis_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    fake = fake_claude(tmp_path)
    monkeypatch.setattr(interactive.shutil, "which", lambda _name: fake)
    repo = make_repo(tmp_path)
    # Port 1: the lead agent's model call fails fast, so Ultra runs directly.
    app = create_test_app(Settings(port=1))

    with TestClient(app, client=("127.0.0.1", 50000), base_url=LOCAL) as client:
        live_id = client.post("/chat/api/live", json={"cwd": str(repo)}).json()[
            "live_id"
        ]
        url = f"/chat/api/live/{live_id}/messages"
        for bad in (0, 7):
            invalid = client.post(
                url, json={"content": "x", "mode": "ultra", "max_parallel": bad}
            )
            assert invalid.status_code == 422
        sent = client.post(
            url,
            json={
                "content": f"Fix add in calc.py\n{FIX}",
                "mode": "ultra",
                "max_parallel": 3,
                "verify": False,
            },
        ).json()
        task_id = sent["task_id"]
        assert sent["ok"] is True and task_id

        deadline = time.monotonic() + 60
        detail = client.get(f"/chat/api/tasks/{task_id}").json()
        while detail["task"]["status"] not in ("COMPLETED", "FAILED", "CANCELLED"):
            assert time.monotonic() < deadline, detail["task"]
            time.sleep(0.1)
            detail = client.get(f"/chat/api/tasks/{task_id}").json()
        assert detail["task"]["status"] == "COMPLETED"
        assert detail["task"]["mode"] == "ultra"
        [analysis] = [e for e in detail["events"] if e["type"] == "ultra.analysis"]
        assert analysis["payload"]["route"] == "direct"
        assert analysis["payload"]["degraded"] is True
        assert "return a + b" in (repo / "calc.py").read_text("utf-8")
        client.delete(f"/chat/api/live/{live_id}")
