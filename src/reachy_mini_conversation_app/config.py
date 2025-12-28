import os
import logging

from dotenv import find_dotenv, load_dotenv


logger = logging.getLogger(__name__)

# Locate .env file (search upward from current working directory)
dotenv_path = find_dotenv(usecwd=True)

if dotenv_path:
    # Load .env and override environment variables
    load_dotenv(dotenv_path=dotenv_path, override=True)
    logger.info(f"Configuration loaded from {dotenv_path}")
else:
    logger.warning("No .env file found, using environment variables")


class Config:
    """Configuration class for the conversation app."""

    # API Keys (at least one required)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # The key is downloaded in console.py if needed
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

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

    REACHY_MINI_CUSTOM_PROFILE = os.getenv("REACHY_MINI_CUSTOM_PROFILE")
    logger.debug(f"Custom Profile: {REACHY_MINI_CUSTOM_PROFILE}")


config = Config()


def set_custom_profile(profile: str | None) -> None:
    """Update the selected custom profile at runtime and expose it via env.

    This ensures modules that read `config` and code that inspects the
    environment see a consistent value.
    """
    try:
        config.REACHY_MINI_CUSTOM_PROFILE = profile
    except Exception:
        pass
    try:
        import os as _os

        if profile:
            _os.environ["REACHY_MINI_CUSTOM_PROFILE"] = profile
        else:
            # Remove to reflect default
            _os.environ.pop("REACHY_MINI_CUSTOM_PROFILE", None)
    except Exception:
        pass
