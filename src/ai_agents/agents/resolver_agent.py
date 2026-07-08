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
        self._index = index
        self._max_hops = max_hops

        # Build tools bound to this index
        tools = make_tools(index)

        # Bind tools to the LLM (enables tool-calling)
        llm_with_tools = llm.bind_tools(tools)

        # Compile the LangGraph
        self._graph = build_resolver_graph(llm_with_tools, tools)

    def resolve(self, chain: RawChain) -> ApiCall:
        """
        Resolve a single chain synchronously.

        Runs the LangGraph loop until the LLM produces a final JSON response
        or max_hops is exceeded.
        """
        logger.info("[ResolverAgent] Resolving: %s", chain.summary())
        initial_state = build_initial_state(chain, self._max_hops)

        try:
            final_state = self._graph.invoke(initial_state)
            result = extract_result(final_state)
            logger.info(
                "[ResolverAgent] Done: %s %s (%s, %d hop(s))",
                result.method,
                result.url,
                result.resolution_status.value,
                final_state.get("hop_count", 0),
            )
            return result
        except Exception as exc:
            logger.error("[ResolverAgent] Error resolving %s: %s", chain.summary(), exc, exc_info=True)
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

    def resolve_all(self, chains: List[RawChain]) -> List[ApiCall]:
        """Resolve multiple chains sequentially."""
        results: List[ApiCall] = []
        for i, chain in enumerate(chains, 1):
            logger.info("[ResolverAgent] Chain %d/%d: %s", i, len(chains), chain.summary())
            results.append(self.resolve(chain))
        return results

    async def resolve_async(self, chain: RawChain) -> ApiCall:
        """Resolve a single chain asynchronously."""
        logger.info("[ResolverAgent] Async resolving: %s", chain.summary())
        initial_state = build_initial_state(chain, self._max_hops)
        try:
            final_state = await self._graph.ainvoke(initial_state)
            return extract_result(final_state)
        except Exception as exc:
            logger.error("[ResolverAgent] Async error: %s", exc, exc_info=True)
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
        tasks = [self.resolve_async(chain) for chain in chains]
        return await asyncio.gather(*tasks)
