"""Incremental repository indexer for fast, low-token file and symbol discovery.

Prevents repeated full-tree filesystem traversals and excessive context reloading.
Tracks file mtimes and only updates modified or new files.
"""

import ast
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class FileIndexEntry:
    path: str
    rel_path: str
    size_bytes: int
    mtime: float
    language: str
    symbols: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


class IncrementalRepoIndexer:
    """Maintains an in-memory cached index of the repository structure and symbols."""

    def __init__(self, root_dir: str = "D:\\Claude code") -> None:
        self.root_dir = os.path.abspath(root_dir)
        self._index: dict[str, FileIndexEntry] = {}
        self._last_scan_time: float = 0.0

    def _detect_language(self, path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        mapping = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".jsx": "javascript_react",
            ".tsx": "typescript_react",
            ".json": "json",
            ".md": "markdown",
            ".toml": "toml",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".ps1": "powershell",
            ".bat": "batch",
            ".cmd": "batch",
            ".sh": "bash",
            ".html": "html",
            ".css": "css",
        }
        return mapping.get(ext, "unknown")

    def _extract_python_symbols(self, content: str) -> tuple[list[str], list[str]]:
        symbols: list[str] = []
        imports: list[str] = []
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    symbols.append(node.name)
                elif isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
        except Exception:
            pass
        return symbols, imports

    def scan(
        self, force: bool = False, max_files: int = 5000
    ) -> dict[str, FileIndexEntry]:
        """Scans repository incrementally, only parsing changed files."""
        now = time.time()
        # Scan if forced or if more than 5 seconds elapsed since last scan
        if not force and (now - self._last_scan_time < 5.0) and self._index:
            return self._index

        ignore_dirs = {
            ".git",
            ".venv",
            "venv",
            "__pycache__",
            "node_modules",
            ".gemini",
            ".idea",
            ".vscode",
            "dist",
            "build",
            ".pytest_cache",
        }

        current_paths = set()
        file_count = 0

        for root, dirs, files in os.walk(self.root_dir):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for f in files:
                file_count += 1
                if file_count > max_files:
                    break

                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, self.root_dir)
                current_paths.add(full_path)

                try:
                    stat = os.stat(full_path)
                    mtime = stat.st_mtime
                    size = stat.st_size
                except OSError:
                    continue

                cached = self._index.get(full_path)
                if cached and cached.mtime == mtime and cached.size_bytes == size:
                    continue  # Unmodified, skip parsing

                lang = self._detect_language(full_path)
                symbols, imports = [], []

                if lang == "python" and size < 500_000:
                    try:
                        with open(full_path, encoding="utf-8", errors="ignore") as fp:
                            symbols, imports = self._extract_python_symbols(fp.read())
                    except Exception:
                        pass

                self._index[full_path] = FileIndexEntry(
                    path=full_path,
                    rel_path=rel_path,
                    size_bytes=size,
                    mtime=mtime,
                    language=lang,
                    symbols=symbols,
                    imports=imports,
                )

        # Remove deleted files from index
        deleted = set(self._index.keys()) - current_paths
        for p in deleted:
            self._index.pop(p, None)

        self._last_scan_time = now
        return self._index

    def find_relevant_files(
        self, keywords: Sequence[str], limit: int = 10
    ) -> list[FileIndexEntry]:
        """Finds files relevant to a task by query keywords, symbols, or paths."""
        self.scan()
        scores: list[tuple[float, FileIndexEntry]] = []
        lower_keywords = [k.lower() for k in keywords if k]

        for entry in self._index.values():
            score = 0.0
            path_lower = entry.rel_path.lower()

            for kw in lower_keywords:
                if kw in path_lower:
                    score += 5.0
                for sym in entry.symbols:
                    if kw in sym.lower():
                        score += 3.0
                for imp in entry.imports:
                    if kw in imp.lower():
                        score += 1.0

            if score > 0:
                scores.append((score, entry))

        scores.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scores[:limit]]


_GLOBAL_REPO_INDEXER = IncrementalRepoIndexer()


def get_repo_indexer(root_dir: str = "D:\\Claude code") -> IncrementalRepoIndexer:
    return _GLOBAL_REPO_INDEXER
