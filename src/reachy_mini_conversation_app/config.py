import os
import logging
from pathlib import Path

from dotenv import load_dotenv


logger = logging.getLogger(__name__)

# Check if .env file exists
env_file = Path(".env")
if not env_file.exists():
    raise RuntimeError(
        ".env file not found. Please create one based on .env.example:\n"
        "  cp .env.example .env\n"
        "Then add your OPENAI_API_KEY or GEMINI_API_KEY to the .env file.",
    )

# Load .env and verify it was loaded successfully
if not load_dotenv():
    raise RuntimeError(
        "Failed to load .env file. Please ensure the file is readable and properly formatted.",
    )

logger.info("Configuration loaded from .env file")


class Config:
    """Configuration class for the conversation app."""

    # API Keys (at least one required)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

    # Validate at least one API key is present
    if not OPENAI_API_KEY and not GEMINI_API_KEY:
        raise RuntimeError(
            "No AI provider API key found in .env file. Please add one:\n"
            "  OPENAI_API_KEY=your_openai_key_here\n"
            "  OR\n"
            "  GEMINI_API_KEY=your_gemini_key_here",
        )

    # Validate non-empty keys
    if OPENAI_API_KEY is not None and not OPENAI_API_KEY.strip():
        raise RuntimeError("OPENAI_API_KEY is empty in .env file. Please provide a valid API key.")
    if GEMINI_API_KEY is not None and not GEMINI_API_KEY.strip():
        raise RuntimeError("GEMINI_API_KEY is empty in .env file. Please provide a valid API key.")

    # Model names
    MODEL_NAME = os.getenv("MODEL_NAME", "gpt-realtime")  # For OpenAI
    GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "models/gemini-2.5-flash-native-audio-preview-09-2025")

    # Other optional configs
    HF_HOME = os.getenv("HF_HOME", "./cache")
    LOCAL_VISION_MODEL = os.getenv("LOCAL_VISION_MODEL", "HuggingFaceTB/SmolVLM2-2.2B-Instruct")
    HF_TOKEN = os.getenv("HF_TOKEN")  # Optional, falls back to hf auth login if not set

    @staticmethod
    def get_provider() -> str:
        """
        Auto-detect which AI provider to use based on available API keys.

        Returns:
            str: "gemini" if GEMINI_API_KEY is set, "openai" if OPENAI_API_KEY is set.

        Raises:
            RuntimeError: If no API key is found (should not happen due to validation above).
        """
        if Config.GEMINI_API_KEY:
            logger.info("Using Gemini as AI provider (GEMINI_API_KEY detected)")
            return "gemini"
        elif Config.OPENAI_API_KEY:
            logger.info("Using OpenAI as AI provider (OPENAI_API_KEY detected)")
            return "openai"
        else:
            raise RuntimeError("No AI provider API key found")

    logger.debug(f"Model: {MODEL_NAME}, Gemini Model: {GEMINI_MODEL_NAME}, HF_HOME: {HF_HOME}, Vision Model: {LOCAL_VISION_MODEL}")


config = Config()
