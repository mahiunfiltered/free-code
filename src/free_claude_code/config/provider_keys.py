"""Multi-key credential pools for key-based providers.

A provider's single key (e.g. ``NVIDIA_NIM_API_KEY``) keeps working. A plural
companion (e.g. ``NVIDIA_NIM_API_KEYS``) adds a pool:
``"label1=key1,label2=key2"`` or a plain comma list (labels default key1..N).
Keys are never part of ``repr``; only labels and fingerprints leave this module.
"""

import hashlib
import re
from dataclasses import dataclass, field

from .provider_catalog import ProviderDescriptor
from .settings import Settings

PRIMARY_KEY_LABEL = "primary"
_LABELED_ENTRY = re.compile(r"^([A-Za-z0-9_.-]{1,40})\s*=\s*([^=\s].*)$")


@dataclass(frozen=True, slots=True)
class ProviderKey:
    """One labeled credential; the secret never appears in repr or JSON."""

    label: str
    key: str = field(repr=False)

    @property
    def fingerprint(self) -> str:
        """Short non-reversible identity used to detect a replaced key."""
        return hashlib.sha256(self.key.encode()).hexdigest()[:12]


def parse_provider_keys(raw: str | None) -> tuple[ProviderKey, ...]:
    """Parse a plural key setting; duplicate keys keep their first entry."""
    if not raw:
        return ()
    keys: list[ProviderKey] = []
    seen_keys: set[str] = set()
    seen_labels: set[str] = set()
    entries = [part.strip() for part in re.split(r"[,\n]", raw)]
    for index, entry in enumerate((part for part in entries if part), start=1):
        match = _LABELED_ENTRY.match(entry)
        label, key = (
            (match.group(1), match.group(2).strip())
            if match
            else (f"key{index}", entry)
        )
        if not key or key in seen_keys:
            continue
        if label in seen_labels:
            label = f"{label}-{index}"
        seen_keys.add(key)
        seen_labels.add(label)
        keys.append(ProviderKey(label=label, key=key))
    return tuple(keys)


def provider_keys(
    descriptor: ProviderDescriptor, settings: Settings
) -> tuple[ProviderKey, ...]:
    """Return the ordered key pool: plural entries first, then a distinct single key."""
    pool = parse_provider_keys(_string_attr(settings, descriptor.credential_pool_attr))
    single = _string_attr(settings, descriptor.credential_attr)
    if single and all(item.key != single for item in pool):
        label = PRIMARY_KEY_LABEL
        if any(item.label == label for item in pool):
            label = f"{label}-{len(pool) + 1}"
        pool = (*pool, ProviderKey(label=label, key=single))
    return pool


def _string_attr(settings: Settings, attr: str | None) -> str | None:
    if attr is None:
        return None
    value = getattr(settings, attr, None)
    return value if isinstance(value, str) and value else None
