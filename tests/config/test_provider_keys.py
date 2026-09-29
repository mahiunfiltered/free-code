"""Plural provider key pools: parsing, labels, dedupe, catalog-wide settings."""

import pytest

from free_claude_code.config.admin.manifest import FIELD_BY_KEY
from free_claude_code.config.loader import compose_settings_snapshot
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.provider_keys import (
    ProviderKey,
    parse_provider_keys,
    provider_keys,
)
from free_claude_code.config.settings import Settings
from free_claude_code.providers.runtime.config import (
    has_provider_configuration,
    provider_credential,
)

NIM = PROVIDER_CATALOG["nvidia_nim"]


def _labels(raw: str | None) -> list[tuple[str, str]]:
    return [(key.label, key.key) for key in parse_provider_keys(raw)]


@pytest.mark.parametrize("raw", [None, "", " , ,\n "])
def test_empty_values_parse_to_no_keys(raw: str | None) -> None:
    assert parse_provider_keys(raw) == ()


def test_plain_list_gets_positional_labels() -> None:
    assert _labels("k-a, k-b ,k-c") == [
        ("key1", "k-a"),
        ("key2", "k-b"),
        ("key3", "k-c"),
    ]


def test_labeled_and_mixed_entries() -> None:
    assert _labels("fast=k-a,k-b\nbackup = spaced") == [
        ("fast", "k-a"),
        ("key2", "k-b"),
        ("backup", "spaced"),
    ]


def test_base64_padding_is_not_mistaken_for_a_label() -> None:
    assert _labels("abc==,lbl=xyz=") == [("key1", "abc=="), ("lbl", "xyz=")]


def test_duplicate_keys_keep_first_and_duplicate_labels_are_suffixed() -> None:
    assert _labels("a=k1,b=k1,a=k2,k2") == [("a", "k1"), ("a-3", "k2")]


def test_key_is_not_in_repr_and_fingerprint_is_stable() -> None:
    key = ProviderKey(label="x", key="secret-value")
    assert "secret-value" not in repr(key)
    assert key.fingerprint == ProviderKey(label="y", key="secret-value").fingerprint
    assert key.fingerprint != ProviderKey(label="x", key="other").fingerprint
    assert len(key.fingerprint) == 12


def test_single_key_is_appended_when_distinct() -> None:
    settings = Settings.model_validate(
        {"NVIDIA_NIM_API_KEY": "single", "NVIDIA_NIM_API_KEYS": "a=one,b=two"}
    )
    assert [(k.label, k.key) for k in provider_keys(NIM, settings)] == [
        ("a", "one"),
        ("b", "two"),
        ("primary", "single"),
    ]


def test_single_key_already_in_pool_is_not_duplicated() -> None:
    settings = Settings.model_validate(
        {"NVIDIA_NIM_API_KEY": "real", "NVIDIA_NIM_API_KEYS": "bad=x,good=real"}
    )
    assert [k.label for k in provider_keys(NIM, settings)] == ["bad", "good"]


def test_primary_label_collision_is_suffixed() -> None:
    settings = Settings.model_validate(
        {"NVIDIA_NIM_API_KEY": "single", "NVIDIA_NIM_API_KEYS": "primary=other"}
    )
    assert [k.label for k in provider_keys(NIM, settings)] == ["primary", "primary-2"]


def test_single_key_only_keeps_working() -> None:
    settings = Settings.model_validate({"NVIDIA_NIM_API_KEY": "only"})
    assert [(k.label, k.key) for k in provider_keys(NIM, settings)] == [
        ("primary", "only")
    ]
    assert provider_credential(NIM, settings) == "only"


def test_plural_only_configures_the_provider() -> None:
    settings = Settings.model_validate({"NVIDIA_NIM_API_KEYS": "a=one"})
    assert has_provider_configuration(NIM, settings)
    assert provider_credential(NIM, settings) == "one"
    assert not has_provider_configuration(NIM, Settings())


def test_every_key_based_provider_has_a_plural_setting_and_admin_secret() -> None:
    for descriptor in PROVIDER_CATALOG.values():
        if descriptor.credential_env is None:
            assert descriptor.credential_pool_env is None
            continue
        attr = descriptor.credential_pool_attr
        env = descriptor.credential_pool_env
        assert attr is not None and env == f"{descriptor.credential_env}S"
        assert Settings.model_fields[attr].validation_alias == env
        spec = FIELD_BY_KEY[env]
        assert spec.secret and spec.field_type == "secret"
        assert spec.settings_attr == attr


def test_plural_env_is_loaded_through_managed_config() -> None:
    snapshot = compose_settings_snapshot({"OPENROUTER_API_KEYS": "a=1,b=2"}, {})
    assert snapshot.settings.open_router_api_keys == "a=1,b=2"
