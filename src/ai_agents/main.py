"""
analyze-apis — CLI entry point for the Java REST API Analyzer (v2 agentic).

Pipeline
────────
1. FileScanner       → collect .java + .properties files
2. SymbolIndexBuilder → pre-scan all symbols (one-time, fast)
3. ChainDetector     → detect all HttpClient builder chains
4. ResolverAgent     → LLM loop resolves each chain into an ApiCall
5. OpenAPIGenerator  → emit OpenAPI 3.1.0 JSON

Usage
─────
    analyze-apis /path/to/java/project
    analyze-apis /path/to/project -o openapi.json
    analyze-apis /path/to/project --llm-provider ollama --llm-model qwen2.5-coder
    analyze-apis /path/to/project --llm-provider openai --llm-model gpt-4o
    analyze-apis /path/to/project --max-hops 8 -v
    analyze-apis /path/to/project --async   (parallel resolution)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import List

from .agents.resolver_agent import ResolverAgent
from .index.symbol_index import SymbolIndexBuilder
from .llm.client import get_llm
from .models.api_call import ApiCall
from .output.openapi import OpenAPIGenerator
from .parsers.java_parser import JavaParser
from .scanner.chain_detector import ChainDetector, RawChain
from .scanner.file_scanner import FileScanner


# ── CLI argument parser ───────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="analyze-apis",
        description="Detect outbound REST API calls in Java code → OpenAPI 3.1.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  analyze-apis ./my-java-project
  analyze-apis ./my-java-project -o api-spec.json
  analyze-apis ./my-java-project --llm-provider openai --llm-model gpt-4o
  analyze-apis ./my-java-project --max-hops 8 --async -v
        """,
    )

    p.add_argument("project_path", help="Root directory of the Java project to scan")
    p.add_argument("-o", "--output", help="Write OpenAPI JSON to this file (default: stdout)")

    # LLM options
    llm = p.add_argument_group("LLM options")
    llm.add_argument(
        "--llm-provider",
        default="ollama",
        choices=["ollama", "openai", "anthropic", "google"],
        help="LLM provider (default: ollama)",
    )
    llm.add_argument(
        "--llm-model",
        default="qwen2.5-coder",
        help="Model name for the chosen provider (default: qwen2.5-coder)",
    )
    llm.add_argument(
        "--max-hops",
        type=int,
        default=6,
        help="Max tool-call iterations per chain (default: 6)",
    )
    llm.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="LLM sampling temperature (default: 0.0)",
    )

    # Performance options
    perf = p.add_argument_group("Performance options")
    perf.add_argument(
        "--async",
        dest="run_async",
        action="store_true",
        help="Resolve chains concurrently using asyncio (faster for large codebases)",
    )

    # Output options
    out = p.add_argument_group("Output options")
    out.add_argument(
        "--title",
        default="Detected REST APIs",
        help="OpenAPI spec title (default: 'Detected REST APIs')",
    )
    out.add_argument(
        "--api-version",
        default="1.0.0",
        help="OpenAPI spec version string (default: 1.0.0)",
    )

    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    return p


# ── Pipeline steps ────────────────────────────────────────────────────────────

def step_scan(project_path: Path, java_parser: JavaParser) -> tuple:
    """Step 1+2: Scan files and build symbol index."""
    logger = logging.getLogger(__name__)

    scanner = FileScanner(project_path)
    java_files, config_files = scanner.scan()

    if not java_files:
        logger.error("No .java files found under %s", project_path)
        sys.exit(1)

    logger.info("⚙️  Building symbol index from %d Java file(s)...", len(java_files))
    t0 = time.perf_counter()
    index_builder = SymbolIndexBuilder(java_parser)
    index = index_builder.build(java_files, config_files)
    elapsed = time.perf_counter() - t0
    stats = index.stats()
    logger.info(
        "✅ Symbol index built in %.2fs — %d symbols, %d classes, %d properties",
        elapsed, stats["symbols"], stats["classes"], stats["properties"],
    )
    return java_files, config_files, index


def step_detect(java_files: list, java_parser: JavaParser) -> List[RawChain]:
    """Step 3: Detect all HttpClient chains."""
    logger = logging.getLogger(__name__)
    detector = ChainDetector(java_parser)
    all_chains: List[RawChain] = []
    for jf in java_files:
        chains = detector.detect_file(jf)
        all_chains.extend(chains)
    logger.info("🔍 %d HttpClient chain(s) detected across %d file(s)", len(all_chains), len(java_files))
    return all_chains


def step_resolve(
    chains: List[RawChain],
    agent: ResolverAgent,
    run_async: bool,
) -> List[ApiCall]:
    """Step 4: Resolve chains to ApiCall objects via LLM agent."""
    logger = logging.getLogger(__name__)
    logger.info("🤖 Resolving %d chain(s) via LLM...", len(chains))
    t0 = time.perf_counter()

    if run_async:
        api_calls = asyncio.run(agent.resolve_all_async(chains))
    else:
        api_calls = agent.resolve_all(chains)

    elapsed = time.perf_counter() - t0
    logger.info("✅ Resolution complete in %.2fs", elapsed)
    return api_calls


def step_generate(api_calls: List[ApiCall], title: str, api_version: str) -> str:
    """Step 5: Generate OpenAPI 3.1.0 JSON."""
    generator = OpenAPIGenerator()
    spec = generator.generate(api_calls, title=title, version=api_version)
    return generator.to_json(spec)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger(__name__)

    project_path = Path(args.project_path).resolve()
    if not project_path.exists():
        logger.error("Project path does not exist: %s", project_path)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("🚀 analyze-apis v2 (agentic)")
    logger.info("   Project : %s", project_path)
    logger.info("   LLM     : %s / %s", args.llm_provider, args.llm_model)
    logger.info("   Max hops: %d", args.max_hops)
    logger.info("   Async   : %s", args.run_async)
    logger.info("=" * 60)

    # Shared parser instance
    java_parser = JavaParser()

    # Step 1+2: Scan + index
    java_files, config_files, index = step_scan(project_path, java_parser)

    # Step 3: Detect chains
    chains = step_detect(java_files, java_parser)
    if not chains:
        logger.warning("⚠️  No HttpClient chains detected. Is this a java.net.http.HttpClient project?")
        # Emit empty spec
        spec_json = step_generate([], args.title, args.api_version)
        _output(spec_json, args.output)
        return

    # Step 4: Build LLM + agent
    logger.info("🔌 Connecting to LLM (%s/%s)...", args.llm_provider, args.llm_model)
    try:
        llm = get_llm(
            provider=args.llm_provider,
            model=args.llm_model,
            temperature=args.temperature,
        )
    except Exception as exc:
        logger.error("Failed to initialise LLM: %s", exc)
        sys.exit(1)

    agent = ResolverAgent(llm, index, max_hops=args.max_hops)

    # Step 4: Resolve
    api_calls = step_resolve(chains, agent, args.run_async)

    # Step 5: Generate
    spec_json = step_generate(api_calls, args.title, args.api_version)

    # Summary
    resolved = sum(1 for c in api_calls if c.resolution_status.value not in ("unresolved",))
    logger.info("=" * 60)
    logger.info("📋 Results: %d/%d calls resolved", resolved, len(api_calls))
    for call in api_calls:
        status_icon = "✅" if call.resolution_status.value not in ("unresolved",) else "❌"
        logger.info(
            "  %s %s %s  [%s]",
            status_icon,
            call.method,
            call.url or call.raw_url_expression,
            call.resolution_status.value,
        )
    logger.info("=" * 60)

    _output(spec_json, args.output)


def _output(spec_json: str, output_path: str | None) -> None:
    """Write spec to file or stdout."""
    if output_path:
        Path(output_path).write_text(spec_json, encoding="utf-8")
        logging.getLogger(__name__).info("📄 OpenAPI spec written to %s", output_path)
    else:
        print(spec_json)


if __name__ == "__main__":
    main()
