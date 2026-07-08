"""
Abstract base class for all library-specific Java REST API analyzers.

Every analyzer (one per Java HTTP client library) must implement this
interface so the top-level runner can treat them uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from ..models.api_call import ApiCall


class BaseAnalyzer(ABC):
    """
    Contract for a library-specific REST API call detector.

    Each concrete subclass targets ONE Java HTTP client library
    (e.g. java.net.http.HttpClient, RestTemplate, OkHttp, …) and
    knows the AST patterns that library produces.
    """

    # Human-readable name shown in logs and output
    library_name: str = "unknown"

    @abstractmethod
    def can_handle(self, source: bytes) -> bool:
        """
        Fast pre-screen check.

        Return True if the source file *might* use this library
        (e.g. by checking for a class name in the raw bytes).
        This avoids a full AST walk on files that clearly don't apply.

        Parameters
        ----------
        source : raw bytes of the Java source file
        """

    @abstractmethod
    def analyze_file(self, file_path: str) -> List[ApiCall]:
        """
        Parse *file_path* and return every detected outbound REST call.

        Parameters
        ----------
        file_path : absolute path to the Java source file
        """
