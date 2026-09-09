"""Tests for model switch CLI."""

from unittest.mock import patch

import pytest

from free_claude_code.cli.launchers.model_switch import (
    Model,
    apply_model,
    fetch_current_model,
    fetch_models,
    run_cli,
)


@pytest.fixture
def mock_admin_response():
    return {
        "models": [
            "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
            "nvidia_nim/nvidia/nemotron-3-super-120b-a12b",
            "nvidia_nim/mistralai/mistral-nemotron",
            "nvidia_nim/moonshotai/kimi-k3",
        ]
    }


@pytest.fixture
def mock_status_response():
    return {"model": "nvidia_nim/nvidia/nemotron-3-super-120b-a12b"}


@pytest.fixture
def mock_apply_response():
    return {"applied": True, "restart": {"automatic": True}}


@patch("free_claude_code.cli.launchers.model_switch._request")
def test_fetch_models(mock_request, mock_admin_response):
    mock_request.return_value = mock_admin_response
    models = fetch_models()
    assert len(models) == 4
    assert all(isinstance(m, Model) for m in models)
    assert models[0].provider_ref == "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b"
    assert models[0].short_name == "nemotron-3-ultra-550b-a55b"


@patch("free_claude_code.cli.launchers.model_switch._request")
def test_fetch_current_model(mock_request, mock_status_response):
    mock_request.return_value = mock_status_response
    current = fetch_current_model()
    assert current == "nvidia_nim/nvidia/nemotron-3-super-120b-a12b"


@patch("free_claude_code.cli.launchers.model_switch._request")
def test_apply_model(mock_request, mock_apply_response):
    mock_request.return_value = mock_apply_response
    result = apply_model("nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b")
    assert result["applied"] is True
    assert result["restart"]["automatic"] is True


@patch("free_claude_code.cli.launchers.model_switch.fetch_models")
@patch("free_claude_code.cli.launchers.model_switch.fetch_current_model")
@patch("free_claude_code.cli.launchers.model_switch.apply_model")
def test_run_cli_success(mock_apply, mock_current, mock_models):
    mock_models.return_value = [
        Model(
            slug="nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
            provider_ref="nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
            display_name="Nemotron 3 Ultra",
            allows_reasoning=False,
        ),
        Model(
            slug="nvidia_nim/nvidia/nemotron-3-super-120b-a12b",
            provider_ref="nvidia_nim/nvidia/nemotron-3-super-120b-a12b",
            display_name="Nemotron 3 Super",
            allows_reasoning=False,
        ),
    ]
    mock_current.return_value = "nvidia_nim/nvidia/nemotron-3-super-120b-a12b"
    mock_apply.return_value = {"applied": True, "restart": {"automatic": True}}

    # Test switching by provider ref
    assert run_cli("nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b") == 0

    # Test switching by short name
    assert run_cli("nemotron-3-ultra-550b-a55b") == 0


@patch("free_claude_code.cli.launchers.model_switch.fetch_models")
def test_run_cli_unknown_model(mock_models):
    mock_models.return_value = [
        Model(
            slug="nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
            provider_ref="nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
            display_name="Nemotron 3 Ultra",
            allows_reasoning=False,
        ),
    ]
    assert run_cli("unknown-model") == 1


@patch("free_claude_code.cli.launchers.model_switch.fetch_models")
@patch("free_claude_code.cli.launchers.model_switch.fetch_current_model")
def test_run_cli_already_using(mock_current, mock_models):
    mock_models.return_value = [
        Model(
            slug="nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
            provider_ref="nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
            display_name="Nemotron 3 Ultra",
            allows_reasoning=False,
        ),
    ]
    mock_current.return_value = "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b"
    assert run_cli("nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b") == 0
