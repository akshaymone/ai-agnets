"""
Data model for a single detected outbound REST API call.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ResolutionStatus(str, Enum):
    """Indicates how confidently the URL was resolved."""
    LITERAL = "literal"           # URL is a hardcoded string literal
    DYNAMIC = "dynamic"           # URL contains variables / concatenation
    LLM_RESOLVED = "llm_resolved" # URL was resolved/inferred by LLM
    UNRESOLVED = "unresolved"     # Could not determine URL


class ApiCall(BaseModel):
    """Represents a single outbound REST API call detected in source code."""

    # ── HTTP basics ────────────────────────────────────────────────────────────
    method: str = Field(description="HTTP method: GET, POST, PUT, DELETE, PATCH, HEAD")
    url: str = Field(description="Raw URL as seen in source (may contain variable placeholders)")
    url_template: Optional[str] = Field(
        default=None,
        description="Normalized URL path with {paramName} placeholders, e.g. /users/{id}",
    )
    resolution_status: ResolutionStatus = Field(
        default=ResolutionStatus.UNRESOLVED,
        description="How reliably the URL was extracted",
    )

    # ── Parameters ────────────────────────────────────────────────────────────
    path_params: List[str] = Field(
        default_factory=list,
        description="Detected path parameter names, e.g. ['id', 'orderId']",
    )
    query_params: Dict[str, str] = Field(
        default_factory=dict,
        description="Query parameters extracted from URL or code, name → example_value",
    )

    # ── Headers & cookies ─────────────────────────────────────────────────────
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description="Request headers, name → value (or variable expression)",
    )
    cookies: Dict[str, str] = Field(
        default_factory=dict,
        description="Cookie values from the Cookie header, name → value",
    )

    # ── Body ──────────────────────────────────────────────────────────────────
    body: Optional[str] = Field(
        default=None,
        description="Request body content or the source expression used to produce it",
    )
    body_content_type: Optional[str] = Field(
        default=None,
        description="Inferred or declared Content-Type of the request body",
    )

    # ── Provenance ────────────────────────────────────────────────────────────
    source_file: str = Field(default="", description="Absolute path to the source file")
    source_line: int = Field(default=0, description="1-based line number where the call starts")
    library: str = Field(default="", description="Java HTTP client library that was used")

    # ── Raw extraction context ────────────────────────────────────────────────
    raw_url_expression: str = Field(
        default="",
        description="The exact source expression used for the URI argument before resolution",
    )
    notes: List[str] = Field(
        default_factory=list,
        description="Analyzer notes, warnings, or LLM inferences about this call",
    )
    llm_metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Structured output from LLM enhancement, if used",
    )
