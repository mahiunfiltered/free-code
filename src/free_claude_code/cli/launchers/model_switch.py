from __future__ import annotations

"""Interactive model switcher for Free Claude Code (registered as ``fcc-model-switch``)."""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TypedDict

ADMIN_URL = os.environ.get("FCC_ADMIN_URL", "http://127.0.0.1:8082")
AUTH_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN", "freecc")


class ModelInfo(TypedDict):
    wire_slug: str
    provider_model_ref: str
    display_name: str
    allows_reasoning: bool


@dataclass(slots=True)
class Model:
    slug: str
    provider_ref: str
    display_name: str
    allows_reasoning: bool

    @property
    def short_name(self) -> str:
        return self.provider_ref.split("/")[-1]


def _request(url: str, method: str = "GET", body: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {AUTH_TOKEN}",
        "Content-Type": "application/json",
        "Origin": ADMIN_URL,
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Connection failed: {e}") from e


def fetch_models() -> list[Model]:
    resp = _request(f"{ADMIN_URL}/admin/api/models")
    models_data = resp.get("models", [])
    return [
        Model(slug=m, provider_ref=m, display_name=m, allows_reasoning=False)
        for m in models_data
        if isinstance(m, str)
    ]


def fetch_current_model() -> str:
    resp = _request(f"{ADMIN_URL}/admin/api/status")
    return resp.get("model", "")


def apply_model(model_ref: str) -> dict:
    return _request(
        f"{ADMIN_URL}/admin/api/config/apply",
        method="POST",
        body={"values": {"MODEL": model_ref}},
    )


def _clear() -> None:
    subprocess.run(["cls"] if os.name == "nt" else ["clear"], check=False)


def _print_header(current: str) -> None:
    print(
        "\033[1;36m╔══════════════════════════════════════════════════════════════╗\033[0m"
    )
    print(
        "\033[1;36m║           FREE CLAUDE CODE - MODEL SWITCHER                  ║\033[0m"
    )
    print(
        "\033[1;36m╠══════════════════════════════════════════════════════════════╣\033[0m"
    )
    print(f"\033[1;36m║\033[0m  Current: \033[1;32m{current:<52}\033[1;36m║\033[0m")
    print(
        "\033[1;36m╚══════════════════════════════════════════════════════════════╝\033[0m"
    )
    print()


def _print_models(models: list[Model], current: str, selected_idx: int) -> None:
    for i, model in enumerate(models):
        is_current = model.provider_ref == current
        is_selected = i == selected_idx

        prefix = "\033[1;33m► \033[0m" if is_selected else "  "
        current_marker = " \033[1;32m✓ current\033[0m" if is_current else ""
        reasoning = " \033[2m(reasoning)\033[0m" if model.allows_reasoning else ""

        style = "\033[1;37m" if is_selected else "\033[0m"
        reset = "\033[0m"

        print(f"{prefix}{style}{model.display_name}{reset}{current_marker}{reasoning}")
        print(f"    \033[2m{model.provider_ref}\033[0m")
        print()


def _print_help() -> None:
    print("\033[2m↑/↓ or j/k: navigate   Enter: select   q: quit   r: refresh\033[0m")


def run_interactive() -> int:
    try:
        current = fetch_current_model()
        models = fetch_models()
    except RuntimeError as e:
        print(f"\033[1;31mError: {e}\033[0m")
        print("\033[2mMake sure FCC server is running (fcc-server)\033[0m")
        return 1

    if not models:
        print("\033[1;31mNo models available\033[0m")
        return 1

    selected_idx = 0
    for i, m in enumerate(models):
        if m.provider_ref == current:
            selected_idx = i
            break

    while True:
        _clear()
        _print_header(current)
        _print_models(models, current, selected_idx)
        _print_help()

        try:
            key = sys.stdin.read(1)
        except KeyboardInterrupt:
            return 0

        if key in ("\x1b", "\x00", "\xe0"):
            key += sys.stdin.read(1)
            if key in ("\x1b[A", "H", "\xe0H"):
                selected_idx = (selected_idx - 1) % len(models)
            elif key in ("\x1b[B", "P", "\xe0P"):
                selected_idx = (selected_idx + 1) % len(models)
        elif key in ("\r", "\n", " "):
            model = models[selected_idx]
            if model.provider_ref == current:
                print(f"\n\033[1;33mAlready using {model.display_name}\033[0m")
            else:
                try:
                    result = apply_model(model.provider_ref)
                    if result.get("applied"):
                        print(f"\n\033[1;32m✓ Switched to {model.display_name}\033[0m")
                        if result.get("restart", {}).get("automatic"):
                            print("\033[2mServer restarting...\033[0m")
                        current = model.provider_ref
                    else:
                        print(
                            f"\n\033[1;31mFailed: {result.get('errors', 'Unknown error')}\033[0m"
                        )
                except RuntimeError as e:
                    print(f"\n\033[1;31mError: {e}\033[0m")
            input("\n\033[2mPress Enter to continue...\033[0m")
        elif key.lower() == "q":
            return 0
        elif key.lower() == "r":
            try:
                models = fetch_models()
                current = fetch_current_model()
                selected_idx = 0
                for i, m in enumerate(models):
                    if m.provider_ref == current:
                        selected_idx = i
                        break
            except RuntimeError as e:
                print(f"\n\033[1;31mRefresh failed: {e}\033[0m")
                input("\n\033[2mPress Enter to continue...\033[0m")
        elif key.lower() in ("j", "k"):
            selected_idx = (selected_idx + (1 if key == "j" else -1)) % len(models)


def run_cli(model_arg: str | None = None) -> int:
    if model_arg:
        try:
            models = fetch_models()
            model_map = {m.provider_ref: m for m in models}
            model_map.update({m.slug: m for m in models})
            model_map.update({m.short_name: m for m in models})

            if model_arg not in model_map:
                print(f"\033[1;31mUnknown model: {model_arg}\033[0m")
                print("\033[2mAvailable:\033[0m")
                for m in models:
                    print(f"  {m.provider_ref} ({m.short_name})")
                return 1

            model = model_map[model_arg]
            current = fetch_current_model()
            if model.provider_ref == current:
                print(f"\033[1;33mAlready using {model.display_name}\033[0m")
                return 0

            result = apply_model(model.provider_ref)
            if result.get("applied"):
                print(f"\033[1;32m✓ Switched to {model.display_name}\033[0m")
                if result.get("restart", {}).get("automatic"):
                    print("\033[2mServer restarting...\033[0m")
                return 0
            print(f"\033[1;31mFailed: {result.get('errors', 'Unknown error')}\033[0m")
            return 1
        except RuntimeError as e:
            print(f"\033[1;31mError: {e}\033[0m")
            return 1

    return run_interactive()


def launch(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    if "--help" in args or "-h" in args:
        print("Usage: fcc-model-switch [MODEL]")
        print(
            "  MODEL  - provider/model ref, slug, or short name (e.g. nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b)"
        )
        print("  No args: interactive TUI")
        return
    sys.exit(run_cli(args[0] if args else None))


if __name__ == "__main__":
    launch()
