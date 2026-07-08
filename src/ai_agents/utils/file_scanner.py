"""
Utility: Scan a directory and yield Java source file paths.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, List

logger = logging.getLogger(__name__)

# Directories to skip when scanning
_DEFAULT_IGNORE_DIRS = {
    ".git", ".svn", ".hg",
    "target", "build", "out", "dist", ".gradle",
    "node_modules", ".idea", ".vscode",
    "__pycache__",
}


class JavaFileScanner:
    """Recursively finds *.java files under a root directory."""

    def __init__(self, ignore_dirs: set[str] | None = None) -> None:
        self.ignore_dirs: set[str] = ignore_dirs or _DEFAULT_IGNORE_DIRS

    def scan(self, directory: str | Path) -> List[str]:
        """Return a sorted list of absolute paths to all .java files found."""
        root = Path(directory).resolve()
        if not root.exists():
            raise FileNotFoundError(f"Directory not found: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {root}")

        results = sorted(str(p) for p in self._iter_java_files(root))
        logger.info("Found %d Java file(s) under %s", len(results), root)
        return results

    def _iter_java_files(self, path: Path) -> Iterator[Path]:
        """Recursively yield .java files, skipping ignored directories."""
        try:
            entries = list(path.iterdir())
        except PermissionError:
            logger.warning("Permission denied: %s — skipping", path)
            return

        for entry in entries:
            if entry.is_dir():
                if entry.name not in self.ignore_dirs:
                    yield from self._iter_java_files(entry)
            elif entry.is_file() and entry.suffix == ".java":
                yield entry
