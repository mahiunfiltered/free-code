from __future__ import annotations

"""Provider construction from declarative profiles and exceptional adapters."""

from collections.abc import Callable, Mapping
from dataclasses import replace

from free_claude_code.application.errors import (
    ApplicationUnavailableError,
    UnknownProviderError,
)
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.provider_keys import provider_keys
from free_claude_code.config.settings import Settings
from free_claude_code.providers.admission import (
    UPSTREAM_TRANSIENT_TOTAL_ATTEMPTS,
    ProviderAdmissionController,
)
from free_claude_code.providers.base import BaseProvider, ProviderConfig
from free_claude_code.providers.key_pool import EndpointPool, PooledProvider, PoolMember
from free_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)

from .config import build_provider_config

ProviderFactory = Callable[
    [ProviderConfig, Settings, ProviderAdmissionController], BaseProvider
]


def _create_nvidia_nim(
    config: ProviderConfig,
    settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.nvidia_nim import NvidiaNimProvider

    return NvidiaNimProvider(
        config,
        nim_settings=settings.nim,
        admission=admission,
    )


def _create_open_router(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.open_router import OpenRouterProvider

    return OpenRouterProvider(config, admission=admission)


def _create_mistral(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.mistral import MistralProvider

    return MistralProvider(config, admission=admission)


def _create_kilo(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.kilo import KiloProvider

    return KiloProvider(config, admission=admission)


def _create_deepseek(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.deepseek import DeepSeekProvider

    return DeepSeekProvider(config, admission=admission)


def _create_lmstudio(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.lmstudio import LMStudioProvider

    return LMStudioProvider(config, admission=admission)


def _create_cloudflare(
    config: ProviderConfig,
    settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.cloudflare import CloudflareProvider

    return CloudflareProvider(
        config,
        account_id=_required_setting(settings, "cloudflare_account_id"),
        admission=admission,
    )


def _create_gemini(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.gemini import GeminiProvider

    return GeminiProvider(config, admission=admission)


def _create_vertex(
    config: ProviderConfig,
    settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.vertex import VertexProvider

    return VertexProvider(
        config,
        project_id=_required_setting(settings, "vertex_project_id"),
        location=settings.vertex_location,
        admission=admission,
    )


def _create_github_models(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.github_models import GitHubModelsProvider

    return GitHubModelsProvider(config, admission=admission)


def _create_groq(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.groq import GroqProvider

    return GroqProvider(config, admission=admission)


def _create_opencode_zen(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.opencode import create_opencode_provider

    return create_opencode_provider("opencode_zen", config, admission)


def _create_opencode_go(
    config: ProviderConfig,
    _settings: Settings,
    admission: ProviderAdmissionController,
) -> BaseProvider:
    from free_claude_code.providers.opencode import create_opencode_provider

    return create_opencode_provider("opencode_go", config, admission)


_SPECIAL_PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    "nvidia_nim": _create_nvidia_nim,
    "open_router": _create_open_router,
    "mistral": _create_mistral,
    "kilo": _create_kilo,
    "deepseek": _create_deepseek,
    "lmstudio": _create_lmstudio,
    "cloudflare": _create_cloudflare,
    "gemini": _create_gemini,
    "vertex": _create_vertex,
    "github_models": _create_github_models,
    "groq": _create_groq,
    "opencode_zen": _create_opencode_zen,
    "opencode_go": _create_opencode_go,
}
_INJECTED_PROVIDER_IDS = {"openai"}


def _required_setting(settings: Settings, attr_name: str) -> str:
    value = getattr(settings, attr_name, None)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"Provider config did not validate {attr_name!r}")
    return value


_profiled_ids = set(OPENAI_CHAT_PROFILES)
_special_ids = set(_SPECIAL_PROVIDER_FACTORIES)
_construction_ids = _profiled_ids | _special_ids | _INJECTED_PROVIDER_IDS
if (
    _profiled_ids & _special_ids
    or _profiled_ids & _INJECTED_PROVIDER_IDS
    or _special_ids & _INJECTED_PROVIDER_IDS
    or _construction_ids != set(PROVIDER_CATALOG)
):
    raise AssertionError(
        "Every provider must have exactly one construction owner: "
        f"profiles={_profiled_ids!r} special={_special_ids!r} "
        f"injected={_INJECTED_PROVIDER_IDS!r} catalog={set(PROVIDER_CATALOG)!r}"
    )


def create_provider(
    provider_id: str,
    settings: Settings,
    *,
    injected_factories: Mapping[str, ProviderFactory] | None = None,
    endpoint_pool: EndpointPool | None = None,
) -> BaseProvider:
    """Create a provider instance for a supported provider id.

    With an ``endpoint_pool``, key-based providers become a :class:`PooledProvider`
    with one inner client per configured key (health, failover, usage rows).
    """
    descriptor = PROVIDER_CATALOG.get(provider_id)
    if descriptor is None:
        raise UnknownProviderError.for_provider(provider_id, PROVIDER_CATALOG)

    config = build_provider_config(descriptor, settings)
    factory = (injected_factories or {}).get(provider_id)
    if provider_id in _INJECTED_PROVIDER_IDS and factory is None:
        raise ApplicationUnavailableError(
            f"Provider {provider_id!r} is unavailable in this runtime."
        )
    factory = factory or _SPECIAL_PROVIDER_FACTORIES.get(provider_id)

    def build(
        member_config: ProviderConfig, name: str, max_attempts: int
    ) -> BaseProvider:
        admission = ProviderAdmissionController(
            provider_name=name,
            rate_limit=member_config.rate_limit,
            rate_window=member_config.rate_window,
            max_concurrency=member_config.max_concurrency,
            max_attempts=max_attempts,
        )
        if factory is not None:
            return factory(member_config, settings, admission)
        return create_openai_chat_provider(provider_id, member_config, admission)

    keys = provider_keys(descriptor, settings) if descriptor.credential_attr else ()
    if endpoint_pool is None or not keys:
        return build(config, provider_id, UPSTREAM_TRANSIENT_TOTAL_ATTEMPTS)
    # ponytail: with several keys the pool owns retries (fail over instead of
    # backing off on a limited key), so each key client makes one attempt.
    per_key_attempts = 1 if len(keys) > 1 else UPSTREAM_TRANSIENT_TOTAL_ATTEMPTS
    members = [
        PoolMember(
            key=key,
            provider=build(
                replace(config, api_key=key.key),
                f"{provider_id}[{key.label}]",
                per_key_attempts,
            ),
        )
        for key in keys
    ]
    return PooledProvider(
        config, provider_id=provider_id, members=members, pool=endpoint_pool
    )
