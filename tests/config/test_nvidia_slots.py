import pytest

from free_claude_code.config.admin import nvidia_slots
from free_claude_code.config.admin.nvidia_slots import (
    SlotInput,
    current_slots,
    normalize_model,
    parse_pool,
    plan_update,
    slot_labels,
    store_pool,
)
from free_claude_code.core.vault import VaultUnavailable


def test_normalize_model_strips_prefix_and_validates() -> None:
    assert normalize_model("  nvidia_nim/a/b ") == "a/b"
    for bad in ("", "a b", "/x", "nvidia_nim/"):
        with pytest.raises(ValueError):
            normalize_model(bad)


def test_labels_are_sanitized_and_unique() -> None:
    assert slot_labels(["x/Nemotron-3.Nano", "y/nemotron-3.nano", "z/"]) == [
        "nemotron_3_nano",
        "nemotron_3_nano_2",
        "model",
    ]


def test_plan_orders_default_first_and_fallbacks() -> None:
    updates, pool = plan_update(
        [
            SlotInput("a/one", "k1"),
            SlotInput(""),
            SlotInput("nvidia_nim/b/two", None, default=True),
        ],
        {"old": "keep"},
    )
    assert updates["CHAT_MODELS"] == "nvidia_nim/b/two,nvidia_nim/a/one"
    assert updates["MODEL_FALLBACKS"] == "nvidia_nim/a/one"
    for key in ("MODEL", "MODEL_FABLE", "MODEL_OPUS", "MODEL_SONNET", "MODEL_HAIKU"):
        assert updates[key] == "nvidia_nim/b/two"
    assert pool == {"old": "keep", "one": "k1"}  # blank key added nothing, old kept


def test_plan_single_slot_clears_fallbacks_and_first_row_is_default() -> None:
    updates, _ = plan_update([SlotInput("a/one")], {})
    assert updates["MODEL_FALLBACKS"] is None
    assert updates["MODEL"] == "nvidia_nim/a/one"


@pytest.mark.parametrize(
    "slots",
    [
        [],
        [SlotInput("a"), SlotInput("nvidia_nim/a")],
        [SlotInput("a", "bad key")],
        [SlotInput(f"m{i}") for i in range(9)],
    ],
)
def test_plan_rejects_invalid(slots) -> None:
    with pytest.raises(ValueError):
        plan_update(slots, {})


def test_replacing_a_key_overwrites_its_label() -> None:
    _, pool = plan_update([SlotInput("a/one", "new")], {"one": "old"})
    assert pool == {"one": "new"}


def test_current_slots_from_config() -> None:
    slots = current_slots(
        ("nvidia_nim/a/one", "groq/x", "nvidia_nim/b/two"),
        "nvidia_nim/c/three",
        False,
    )
    assert [(s["model"], s["has_key"], s["default"]) for s in slots] == [
        ("a/one", False, False),
        ("b/two", False, False),
        ("c/three", False, True),
    ]
    single = current_slots(None, "nvidia_nim/a/one", True)
    assert single == [
        {"model": "a/one", "label": "one", "has_key": True, "default": True}
    ]
    assert current_slots(None, "groq/x", True) == []


class _FakeVault:
    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}

    def set(self, name: str, value: str) -> None:
        self.secrets[name] = value


def test_store_pool_uses_existing_vault_ref_or_default_name(monkeypatch) -> None:
    vault = _FakeVault()
    monkeypatch.setattr(nvidia_slots, "managed_vault", lambda: vault)
    assert store_pool({"a": "k1", "b": "k2"}, "vault:mine") == "vault:mine"
    assert parse_pool(vault.secrets["mine"]) == {"a": "k1", "b": "k2"}
    assert store_pool({"a": "k1"}, "plain=1") == "vault:nvidia_pool"
    assert vault.secrets["nvidia_pool"] == "a=k1"


def test_store_pool_falls_back_to_plain_without_vault(monkeypatch) -> None:
    def unavailable():
        raise VaultUnavailable("none")

    monkeypatch.setattr(nvidia_slots, "managed_vault", unavailable)
    assert store_pool({"a": "k1"}, None) == "a=k1"
