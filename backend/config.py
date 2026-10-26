"""Settings shared by every agent: model, Portkey connection, data paths, and run limits."""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
OUTPUT_DIR = ROOT / "output"
STATIC_DIR = ROOT / "static"

# The .env holds PORTKEY_API_KEY: first this project folder, then the folder above it.
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env")

# The course key allows exactly one model, for every agent.
MODEL_NAME = "gpt-6-luna"
PORTKEY_BASE_URL = os.environ.get("PORTKEY_BASE_URL", "https://api.portkey.ai/v1")
PORTKEY_PROVIDER = os.environ.get("PORTKEY_PROVIDER", "openai")

# USD per 1M tokens for gpt-6-luna, the rates used in the course's Lecture 11 tokenomics code.
# OpenAI's input_tokens include cached tokens, which are billed at the cached rate.
PRICE_INPUT_PER_M = 0.10
PRICE_CACHED_INPUT_PER_M = 0.01
PRICE_OUTPUT_PER_M = 0.50
# Hard spend cap for one plan run (a whole plan normally costs well under a cent).
RUN_BUDGET_USD = 0.05

# Limits that keep one plan (or one chat answer) bounded.
REQUEST_LIMIT = 25
TOTAL_TOKENS_LIMIT = 120_000
CHAT_TOTAL_TOKENS_LIMIT = 40_000
MAX_TOOL_RESULT_CHARS = 6_000
MAX_LIST_ITEMS = 25
MAX_INPUT_CHARS = 1_500
MAX_RUNS_PER_HOUR = 30

# Live data can be switched off (for example, when offline) to use only the saved snapshot.
USE_LIVE_DATA = os.environ.get("USE_LIVE_DATA", "1") != "0"
HTTP_TIMEOUT = 12.0


def build_model() -> OpenAIResponsesModel:
    """Build the one allowed model, called through Portkey with PORTKEY_API_KEY."""
    api_key = os.environ.get("PORTKEY_API_KEY")
    if not api_key:
        raise RuntimeError("PORTKEY_API_KEY is not set (put it in .env here or in the folder above).")
    client = AsyncOpenAI(
        base_url=PORTKEY_BASE_URL,
        api_key="portkey",  # placeholder; Portkey authenticates with the headers below
        default_headers={
            "x-portkey-api-key": api_key,
            "x-portkey-provider": PORTKEY_PROVIDER,
        },
    )
    # gpt-6-luna refuses function tools together with reasoning on /v1/chat/completions,
    # so the agents talk to the Responses API.
    return OpenAIResponsesModel(MODEL_NAME, provider=OpenAIProvider(openai_client=client))
