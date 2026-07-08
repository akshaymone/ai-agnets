"""
CLI entry point for the Java REST API Analyzer.

Usage
─────
    # Basic scan (tree-sitter only)
    analyze-apis /path/to/java/project

    # With LLM enhancement for dynamic URIs
    analyze-apis /path/to/java/project --llm

    # Use a specific Ollama model
    analyze-apis /path/to/java/project --llm --llm-model codellama

    # Save output to file
    analyze-apis /path/to/java/project -o openapi.json

    # Use a different LLM provider (requires env vars / API keys)
    analyze-apis /path/to/java/project --llm --llm-provider openai --llm-model gpt-4o
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List

from .analyzers.java_net_http.analyzer import JavaNetHttpClientAnalyzer
from .models.api_call import ApiCall, ResolutionStatus
from .output.openapi import OpenAPIGenerator
from .parsers.java_parser import JavaParser
from .utils.file_scanner import JavaFileScanner


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(levelname)s | %(name)s | %(message)s",
        level=level,
        stream=sys.stderr,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="analyze-apis",
        description="Scan a Java project and extract REST API calls as OpenAPI 3.1.0.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "directory",
        help="Root directory of the Java project to scan.",
    )
    p.add_argument(
        "-o", "--output",
        metavar="FILE",
        help="Write OpenAPI JSON to FILE instead of stdout.",
    )
    p.add_argument(
        "--llm",
        action="store_true",
        default=False,
        help="Enable LLM enhancement for dynamic / unresolved URIs.",
    )
    p.add_argument(
        "--llm-provider",
        default="ollama",
        metavar="PROVIDER",
        help="LLM provider: ollama (default), openai, anthropic.",
    )
    p.add_argument(
        "--llm-model",
        default="llama3.2",
        metavar="MODEL",
        help="Model name to use (default: llama3.2).",
    )
    p.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        metavar="URL",
        help="Ollama server base URL (default: http://localhost:11434).",
    )
    p.add_argument(
        "--title",
        default=None,
        help="OpenAPI info.title (default: derived from directory name).",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )
    return p


def _enhance_with_llm(calls: List[ApiCall], llm_client) -> None:
    """Optionally use LLM to resolve dynamic URIs in-place."""
    from .models.api_call import ResolutionStatus

    for call in calls:
        if call.resolution_status not in (
            ResolutionStatus.DYNAMIC, ResolutionStatus.UNRESOLVED
        ):
            continue
        if not call.raw_url_expression:
            continue

        logging.getLogger(__name__).info(
            "LLM resolving URI in %s:%d — %s",
            call.source_file, call.source_line, call.raw_url_expression,
        )
        result = llm_client.resolve_dynamic_uri(call.raw_url_expression)
        if not result:
            continue

        if result.get("url_template"):
            call.url_template = result["url_template"]
            call.resolution_status = ResolutionStatus.LLM_RESOLVED
        if result.get("path_params"):
            call.path_params = result["path_params"]
        if result.get("query_params"):
            call.query_params.update(result["query_params"])
        if result.get("notes"):
            call.notes.append(f"[LLM] {result['notes']}")
        call.llm_metadata = result


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    _setup_logging(args.verbose)
    log = logging.getLogger(__name__)

    # ── Initialise components ─────────────────────────────────────────────────
    java_parser = JavaParser()
    scanner = JavaFileScanner()
    generator = OpenAPIGenerator()

    # Active analyzers (MVP 1: only java.net.http.HttpClient)
    analyzers = [JavaNetHttpClientAnalyzer(java_parser)]

    # ── Scan files ────────────────────────────────────────────────────────────
    try:
        java_files = scanner.scan(args.directory)
    except (FileNotFoundError, NotADirectoryError) as exc:
        log.error("%s", exc)
        sys.exit(1)

    if not java_files:
        log.warning("No Java files found in %s", args.directory)
        sys.exit(0)

    log.info("Scanning %d Java file(s)…", len(java_files))

    # ── Analyse ───────────────────────────────────────────────────────────────
    all_calls: List[ApiCall] = []
    for file_path in java_files:
        for analyzer in analyzers:
            try:
                calls = analyzer.analyze_file(file_path)
                all_calls.extend(calls)
            except Exception as exc:
                log.error("Error analysing %s: %s", file_path, exc, exc_info=args.verbose)

    log.info("Total API calls detected: %d", len(all_calls))

    # ── LLM enhancement (optional) ────────────────────────────────────────────
    if args.llm and all_calls:
        try:
            from .llm.client import LLMClient
            llm_client = LLMClient(
                provider=args.llm_provider,
                model=args.llm_model,
                base_url=args.ollama_url,
            )
            _enhance_with_llm(all_calls, llm_client)
        except Exception as exc:
            log.warning("LLM enhancement failed: %s — continuing without it.", exc)

    # ── Generate OpenAPI spec ─────────────────────────────────────────────────
    title = args.title or f"APIs detected in {Path(args.directory).name}"
    spec = generator.generate(all_calls, title=title)
    output_json = generator.to_json(spec)

    # ── Output ────────────────────────────────────────────────────────────────
    if args.output:
        out_path = Path(args.output)
        out_path.write_text(output_json, encoding="utf-8")
        log.info("OpenAPI spec written to %s", out_path)
    else:
        print(output_json)


if __name__ == "__main__":
    main()
