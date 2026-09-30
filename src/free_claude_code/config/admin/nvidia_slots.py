"""NVIDIA model slots: rows of (model id, key, default) <-> CHAT_MODELS/MODEL*/pool."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from free_claude_code.config.loader import (
    VAULT_REF_PREFIX,
    clear_settings_cache,
    managed_vault,
)
from free_claude_code.config.provider_keys import parse_provider_keys
from free_claude_code.core.json_types import JsonObject
from free_claude_code.core.vault import VaultError

MAX_SLOTS = 8
PROVIDER_PREFIX = "nvidia_nim/"
POOL_ENV = "NVIDIA_NIM_API_KEYS"
POOL_SECRET = "nvidia_pool"
_ROUTED_MODEL_KEYS = (
    "MODEL",
    "MODEL_FABLE",
    "MODEL_OPUS",
    "MODEL_SONNET",
    "MODEL_HAIKU",
)


@dataclass(frozen=True, slots=True)
class SlotInput:
    model: str
    key: str | None = None
    default: bool = False


def normalize_model(raw: str) -> str:
    """Strip whitespace and an optional ``nvidia_nim/`` prefix; reject bad ids."""

    model = raw.strip().removeprefix(PROVIDER_PREFIX)
    if not model or re.search(r"\s", model) or model.startswith("/"):
        raise ValueError(f"Invalid model id: {raw.strip()!r}")
    return model


def slot_labels(models: Sequence[str]) -> list[str]:
    """Unique pool labels ([a-z0-9_], <=40 chars) from each model's last segment."""

    labels: list[str] = []
    for model in models:
        base = re.sub(r"[^a-z0-9_]+", "_", model.rsplit("/", 1)[-1].lower()).strip("_")
        base = (base or "model")[:36]
        label, n = base, 2
        while label in labels:
            label = f"{base}_{n}"
            n += 1
        labels.append(label)
    return labels


def current_slots(
    chat_models: Sequence[str] | None,
    model: str,
    any_key: bool,
) -> list[JsonObject]:
    """Slots derived from CHAT_MODELS (nvidia only) plus MODEL when it is nvidia.

    NVIDIA keys are account-wide and pooled, so any saved key connects every slot.
    """

    ids = [
        ref.removeprefix(PROVIDER_PREFIX)
        for ref in (chat_models or ())
        if ref.startswith(PROVIDER_PREFIX)
    ]
    default = (
        model.removeprefix(PROVIDER_PREFIX)
        if model.startswith(PROVIDER_PREFIX)
        else None
    )
    if default and default not in ids:
        ids.append(default)
    ids = list(dict.fromkeys(ids))
    default = default if default in ids else (ids[0] if ids else None)
    return [
        {
            "model": model_id,
            "label": label,
            "has_key": any_key,
            "default": model_id == default,
        }
        for model_id, label in zip(ids, slot_labels(ids), strict=True)
    ]


def parse_pool(raw: str | None) -> dict[str, str]:
    return {item.label: item.key for item in parse_provider_keys(raw)}


def serialize_pool(pool: Mapping[str, str]) -> str:
    return ",".join(f"{label}={key}" for label, key in pool.items())


def plan_update(
    slots: Sequence[SlotInput], pool: Mapping[str, str]
) -> tuple[dict[str, str | None], dict[str, str]]:
    """Return (config updates, new pool). Blank keys keep existing pool entries."""

    rows = [
        SlotInput(normalize_model(s.model), (s.key or "").strip() or None, s.default)
        for s in slots
        if s.model.strip()
    ]
    if not rows:
        raise ValueError("Add at least one model.")
    if len(rows) > MAX_SLOTS:
        raise ValueError(f"At most {MAX_SLOTS} models are supported.")
    if len({r.model for r in rows}) != len(rows):
        raise ValueError("Each model may appear only once.")
    for r in rows:
        if r.key and re.search(r"[\s,=]", r.key):
            raise ValueError("API keys cannot contain spaces, commas or '='.")
    default = next((r for r in rows if r.default), rows[0])
    ordered = [default, *(r for r in rows if r is not default)]
    refs = [PROVIDER_PREFIX + r.model for r in ordered]
    updates: dict[str, str | None] = {
        "CHAT_MODELS": ",".join(refs),
        "MODEL_FALLBACKS": ",".join(refs[1:]) or None,
    }
    updates.update(dict.fromkeys(_ROUTED_MODEL_KEYS, refs[0]))
    new_pool = dict(pool)
    # labels follow saved order (default first) so GET derives the same ones
    for r, label in zip(ordered, slot_labels([r.model for r in ordered]), strict=True):
        if r.key:
            new_pool[label] = r.key
    return updates, new_pool


def store_pool(pool: Mapping[str, str], current_env_value: str | None) -> str:
    """Persist the pool; return the value for NVIDIA_NIM_API_KEYS (vault ref if usable)."""

    raw = serialize_pool(pool)
    current = (current_env_value or "").strip()
    name = (
        current.removeprefix(VAULT_REF_PREFIX).strip()
        if current.startswith(VAULT_REF_PREFIX)
        else POOL_SECRET
    )
    try:
        managed_vault().set(name, raw)
    except VaultError, OSError:
        return raw  # ponytail: no usable vault on this OS -> plain .env value
    clear_settings_cache()
    return f"{VAULT_REF_PREFIX}{name}"
