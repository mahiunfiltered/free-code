"""HTTP contract for the local chat UI."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from free_claude_code.config.settings import Settings
from tests.api.support import create_test_app, provider_manager_for_app

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


def test_models_default_only_model(client: TestClient):
    body = client.get("/chat/api/models").json()
    assert [model["default"] for model in body["models"]] == [True]
    assert body["default"] == body["models"][0]["value"]
    assert body["default"].startswith("anthropic/")


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
