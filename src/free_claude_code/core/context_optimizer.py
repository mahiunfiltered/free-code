from __future__ import annotations

"""Context optimizer for task-scoped prompt generation.

Reduces model latency, prompt bloat, and token overhead by extracting only
the relevant interfaces, signatures, and file snippets required for a specific task.
"""

import os
from dataclasses import dataclass
from typing import Sequence

from free_claude_code.core.repo_indexer import FileIndexEntry, get_repo_indexer


@dataclass
class OptimizedContext:
    task_id: str
    relevant_files: list[str]
    context_text: str
    estimated_tokens: int
    compression_ratio: float


class ContextOptimizer:
    """Extracts task-scoped context and minimizes token consumption."""

    def __init__(self, root_dir: str = "D:\\Claude code") -> None:
        self.indexer = get_repo_indexer(root_dir)

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def build_task_context(
        self,
        task_id: str,
        task_description: str,
        scope_paths: Sequence[str] = (),
        keywords: Sequence[str] = (),
        max_tokens: int = 4000,
    ) -> OptimizedContext:
        """Builds a compact, task-focused context snippet."""
        # 1. Gather relevant files from scopes or keyword search
        entries: list[FileIndexEntry] = []
        if scope_paths:
            self.indexer.scan()
            for sp in scope_paths:
                norm = os.path.abspath(sp)
                if norm in self.indexer._index:
                    entries.append(self.indexer._index[norm])

        if not entries and keywords:
            entries = self.indexer.find_relevant_files(keywords, limit=5)

        # 2. Extract compact summaries / interfaces
        context_parts: list[str] = [f"### Task: {task_id}\n{task_description}\n"]
        total_chars = len(context_parts[0])
        relevant_file_paths = []

        for entry in entries:
            relevant_file_paths.append(entry.rel_path)
            snippet = f"\n#### File: {entry.rel_path} ({entry.language})\n"
            if entry.symbols:
                snippet += f"Symbols: {', '.join(entry.symbols[:15])}\n"
            if entry.imports:
                snippet += f"Imports: {', '.join(entry.imports[:10])}\n"

            # Read head of file if small
            if entry.size_bytes < 50_000 and (total_chars + len(snippet) < max_tokens * 3):
                try:
                    with open(entry.path, "r", encoding="utf-8", errors="ignore") as fp:
                        lines = fp.readlines()
                        head = "".join(lines[:30])
                        snippet += f"```\n{head}\n```\n"
                except Exception:
                    pass

            context_parts.append(snippet)
            total_chars += len(snippet)
            if total_chars > max_tokens * 4:
                break

        full_context = "\n".join(context_parts)
        est_tokens = self._estimate_tokens(full_context)

        # Compression ratio compared to full repo (~500k chars)
        baseline_chars = 500_000
        compression_ratio = baseline_chars / max(1, len(full_context))

        return OptimizedContext(
            task_id=task_id,
            relevant_files=relevant_file_paths,
            context_text=full_context,
            estimated_tokens=est_tokens,
            compression_ratio=compression_ratio,
        )


_GLOBAL_CONTEXT_OPTIMIZER = ContextOptimizer()


def get_context_optimizer(root_dir: str = "D:\\Claude code") -> ContextOptimizer:
    return _GLOBAL_CONTEXT_OPTIMIZER
