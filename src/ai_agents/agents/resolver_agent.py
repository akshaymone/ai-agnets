"""
ResolverAgent — orchestrates a single chain's resolution through the LangGraph loop.

This is the public interface used by main.py. It:
  1. Builds the initial state for a chain
  2. Runs the LangGraph graph
  3. Extracts and returns the resolved ApiCall
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List

from langchain_core.language_models import BaseChatModel

from ..index.symbol_index import SymbolIndex
from ..models.api_call import ApiCall
from ..scanner.chain_detector import RawChain
from .graph import MAX_HOPS_DEFAULT, build_initial_state, build_resolver_graph, extract_result
from .tools.resolver_tools import make_tools

logger = logging.getLogger(__name__)


class ResolverAgent:
    """
    Resolves a single RawChain into a fully specified ApiCall via LLM + tools.

    Usage:
        agent = ResolverAgent(llm, index, max_hops=5)
        api_call = agent.resolve(chain)
    """

    def __init__(
        self,
        llm: BaseChatModel,
        index: SymbolIndex,
        max_hops: int = MAX_HOPS_DEFAULT,
    ) -> None:
        logger.info(
            "[ResolverAgent] ══════════════════════════════════════════"
        )
        logger.info(
            "[ResolverAgent] Initializing ResolverAgent"
        )
        logger.info(
            "[ResolverAgent]   LLM type    : %s", type(llm).__name__
        )
        logger.info(
            "[ResolverAgent]   Max hops    : %d", max_hops
        )
        logger.info(
            "[ResolverAgent]   Index stats : %s", index.stats()
        )

        self._index = index
        self._max_hops = max_hops

        # Step 1: Build tools bound to this index
        logger.debug(
            "[ResolverAgent] STEP 1 — Creating tools (closures over SymbolIndex)..."
        )
        tools = make_tools(index)
        logger.debug(
            "[ResolverAgent]   Tools created: %s", [t.name for t in tools]
        )

        # Step 2: Bind tools to the LLM (enables tool-calling / function-calling)
        logger.debug(
            "[ResolverAgent] STEP 2 — Binding tools to LLM..."
        )
        logger.debug(
            "[ResolverAgent]   bind_tools() tells the LLM about the available tools "
            "so it can request them by name in its response."
        )
        llm_with_tools = llm.bind_tools(tools)
        logger.debug(
            "[ResolverAgent]   LLM with tools type: %s", type(llm_with_tools).__name__
        )

        # Step 3: Compile the LangGraph StateGraph
        logger.debug(
            "[ResolverAgent] STEP 3 — Compiling LangGraph StateGraph..."
        )
        logger.debug(
            "[ResolverAgent]   The graph defines the agentic loop: "
            "resolver_node ↔ tools_node with conditional routing"
        )
        self._graph = build_resolver_graph(llm_with_tools, tools)
        logger.info(
            "[ResolverAgent] ✓ ResolverAgent ready. Graph compiled with %d tool(s).",
            len(tools),
        )
        logger.info(
            "[ResolverAgent] ══════════════════════════════════════════"
        )

    def resolve(self, chain: RawChain) -> ApiCall:
        """
        Resolve a single chain synchronously.

        Full flow:
        ──────────
        1. build_initial_state → creates [SystemMessage, HumanMessage] with all chain context
        2. graph.invoke(state) → runs the LangGraph loop:
              resolver_node (LLM call)
                → should_continue?
                  → if tool_calls: tools_node → back to resolver_node (hop++)
                  → if no tool_calls: END
        3. extract_result(final_state) → parse last AIMessage JSON → ApiCall
        """
        logger.info(
            "[ResolverAgent] ──────────────────────────────────────────"
        )
        logger.info(
            "[ResolverAgent] resolve() START: %s", chain.summary()
        )
        logger.info(
            "[ResolverAgent]   file          : %s", chain.file
        )
        logger.info(
            "[ResolverAgent]   line          : %d", chain.line
        )
        logger.info(
            "[ResolverAgent]   class         : %s", chain.class_name
        )
        logger.info(
            "[ResolverAgent]   uri hint      : '%s'", chain.raw_uri_expr or "<none>"
        )
        logger.info(
            "[ResolverAgent]   method hint   : '%s'", chain.suspected_method or "GET"
        )
        logger.debug(
            "[ResolverAgent]   chain_text preview: %s",
            chain.chain_text[:200].replace("\n", " "),
        )

        logger.debug("[ResolverAgent] Building initial LangGraph state...")
        initial_state = build_initial_state(chain, self._max_hops)

        t0 = time.perf_counter()
        try:
            logger.info(
                "[ResolverAgent] Invoking LangGraph (max_hops=%d)...", self._max_hops
            )
            logger.debug(
                "[ResolverAgent]   graph.invoke() will block until the LLM produces "
                "a final response (no more tool calls) or max_hops is hit."
            )
            final_state = self._graph.invoke(initial_state)
            elapsed = time.perf_counter() - t0

            logger.debug(
                "[ResolverAgent] graph.invoke() returned. Elapsed: %.2fs. "
                "hop_count=%d. Extracting result...",
                elapsed,
                final_state.get("hop_count", 0),
            )
            result = extract_result(final_state)
            elapsed_total = time.perf_counter() - t0

            logger.info(
                "[ResolverAgent] ✓ resolve() DONE in %.2fs: %s %s [%s] (%d hop(s))",
                elapsed_total,
                result.method,
                result.url,
                result.resolution_status.value,
                final_state.get("hop_count", 0),
            )
            logger.info(
                "[ResolverAgent] ──────────────────────────────────────────"
            )
            return result

        except Exception as exc:
            elapsed = time.perf_counter() - t0
            logger.error(
                "[ResolverAgent] ✗ Error resolving %s after %.2fs: %s",
                chain.summary(),
                elapsed,
                exc,
                exc_info=True,
            )
            from ..models.api_call import ResolutionStatus
            fallback = ApiCall(
                method=chain.suspected_method or "GET",
                url=chain.raw_uri_expr or "",
                resolution_status=ResolutionStatus.UNRESOLVED,
                source_file=chain.file,
                source_line=chain.line,
                library="java.net.http.HttpClient",
                raw_url_expression=chain.raw_uri_expr,
                notes=[f"Agent error: {exc}"],
            )
            logger.warning(
                "[ResolverAgent] Returning FALLBACK ApiCall for %s", chain.summary()
            )
            return fallback

    def resolve_all(self, chains: List[RawChain]) -> List[ApiCall]:
        """Resolve multiple chains sequentially."""
        total = len(chains)
        logger.info(
            "[ResolverAgent] resolve_all() START: %d chain(s) to resolve (sequential mode)",
            total,
        )
        results: List[ApiCall] = []
        for i, chain in enumerate(chains, 1):
            logger.info(
                "[ResolverAgent] ════ Chain %d/%d ════ %s",
                i,
                total,
                chain.summary(),
            )
            result = self.resolve(chain)
            results.append(result)
            logger.info(
                "[ResolverAgent] Progress: %d/%d done (latest: %s %s [%s])",
                i,
                total,
                result.method,
                result.url,
                result.resolution_status.value,
            )

        resolved_count = sum(
            1 for r in results if r.resolution_status.value != "unresolved"
        )
        logger.info(
            "[ResolverAgent] resolve_all() COMPLETE: %d/%d resolved successfully",
            resolved_count,
            total,
        )
        return results

    async def resolve_async(self, chain: RawChain) -> ApiCall:
        """Resolve a single chain asynchronously."""
        logger.info(
            "[ResolverAgent] resolve_async() START: %s", chain.summary()
        )
        initial_state = build_initial_state(chain, self._max_hops)
        t0 = time.perf_counter()
        try:
            final_state = await self._graph.ainvoke(initial_state)
            elapsed = time.perf_counter() - t0
            result = extract_result(final_state)
            logger.info(
                "[ResolverAgent] resolve_async() DONE in %.2fs: %s %s [%s] (%d hop(s))",
                elapsed,
                result.method,
                result.url,
                result.resolution_status.value,
                final_state.get("hop_count", 0),
            )
            return result
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            logger.error(
                "[ResolverAgent] ✗ Async error for %s after %.2fs: %s",
                chain.summary(),
                elapsed,
                exc,
                exc_info=True,
            )
            from ..models.api_call import ResolutionStatus
            return ApiCall(
                method=chain.suspected_method or "GET",
                url=chain.raw_uri_expr or "",
                resolution_status=ResolutionStatus.UNRESOLVED,
                source_file=chain.file,
                source_line=chain.line,
                library="java.net.http.HttpClient",
                raw_url_expression=chain.raw_uri_expr,
                notes=[f"Agent error: {exc}"],
            )

    async def resolve_all_async(self, chains: List[RawChain]) -> List[ApiCall]:
        """Resolve multiple chains concurrently using asyncio."""
        logger.info(
            "[ResolverAgent] resolve_all_async() START: %d chain(s) to resolve (concurrent mode)",
            len(chains),
        )
        logger.debug(
            "[ResolverAgent]   asyncio.gather() will run all chains in parallel. "
            "Each chain gets its own async graph invocation."
        )
        tasks = [self.resolve_async(chain) for chain in chains]
        results = await asyncio.gather(*tasks)
        resolved_count = sum(
            1 for r in results if r.resolution_status.value != "unresolved"
        )
        logger.info(
            "[ResolverAgent] resolve_all_async() COMPLETE: %d/%d resolved successfully",
            resolved_count,
            len(chains),
        )
        return list(results)
