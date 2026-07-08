"""
LLM client — provider-agnostic factory for LangChain chat models.

Supported providers
───────────────────
  ollama      ChatOllama (default, local)
  openai      ChatOpenAI
  anthropic   ChatAnthropic
  google      ChatGoogleGenerativeAI

Usage
─────
    llm = get_llm("ollama", "qwen2.5-coder")
    llm = get_llm("openai", "gpt-4o")
    llm = get_llm("anthropic", "claude-3-5-sonnet-20241022")
    llm = get_llm("google", "gemini-2.0-flash")
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


def get_llm(
    provider: str = "ollama",
    model: str = "qwen2.5-coder",
    temperature: float = 0.0,
    **kwargs: Any,
) -> BaseChatModel:
    """
    Return a configured LangChain chat model for the given provider.

    Parameters
    ----------
    provider    : "ollama" | "openai" | "anthropic" | "google"
    model       : Model name (provider-specific)
    temperature : Sampling temperature (0.0 = deterministic, good for code analysis)
    **kwargs    : Extra kwargs forwarded to the model constructor
    """
    provider = provider.lower().strip()
    logger.info("[LLMClient] Initialising %s / %s (temp=%.1f)", provider, model, temperature)

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model, temperature=temperature, **kwargs)

    elif provider == "openai":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise ImportError(
                "langchain-openai is not installed. Run: pip install langchain-openai"
            )
        return ChatOpenAI(model=model, temperature=temperature, **kwargs)

    elif provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            raise ImportError(
                "langchain-anthropic is not installed. Run: pip install langchain-anthropic"
            )
        return ChatAnthropic(model=model, temperature=temperature, **kwargs)

    elif provider == "google":
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            raise ImportError(
                "langchain-google-genai is not installed. Run: pip install langchain-google-genai"
            )
        return ChatGoogleGenerativeAI(model=model, temperature=temperature, **kwargs)

    else:
        raise ValueError(
            f"Unknown LLM provider: '{provider}'. "
            "Choose from: ollama, openai, anthropic, google"
        )
