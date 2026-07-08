"""
FileScanner — collects .java and .properties files from a project directory.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)


class FileScanner:
    """Walks a directory tree and returns Java + properties file paths."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def java_files(self) -> List[Path]:
        """Return all .java files under root."""
        files = sorted(self.root.rglob("*.java"))
        logger.info("[FileScanner] %d .java file(s) found under %s", len(files), self.root)
        return files

    def properties_files(self) -> List[Path]:
        """Return all .properties and .yml/.yaml files under root."""
        props = sorted(self.root.rglob("*.properties"))
        ymls = sorted(self.root.rglob("*.yml")) + sorted(self.root.rglob("*.yaml"))
        all_files = props + ymls
        logger.info(
            "[FileScanner] %d config file(s) found under %s", len(all_files), self.root
        )
        return all_files

    def scan(self) -> Tuple[List[Path], List[Path]]:
        """Return (java_files, config_files) tuple."""
        return self.java_files(), self.properties_files()
