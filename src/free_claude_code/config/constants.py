"""Shared defaults used by config models and provider adapters."""

# HTTP client connect timeout (seconds). Keep aligned with README.md and .env.example.
HTTP_CONNECT_TIMEOUT_DEFAULT = 10.0

# Provider stream stall guards (seconds). Unlike the socket-level read timeout,
# these count parsed stream chunks, so SSE keep-alive comments do not reset them.
# NIM reasoning models can take minutes to their first token.
HTTP_FIRST_BYTE_TIMEOUT_DEFAULT = 120.0
HTTP_STREAM_IDLE_TIMEOUT_DEFAULT = 90.0

# Anthropic Messages API default when the client omits max_tokens.
ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS = 81920
