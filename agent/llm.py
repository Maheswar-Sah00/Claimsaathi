"""Thin model wrapper. The model only extracts or rephrases; it never decides anything.

Without GEMINI_API_KEY the agent runs in deterministic mode and nothing here is called.
"""

from __future__ import annotations

from typing import Any, Protocol


class LlmClient(Protocol):
    """Structured-output model interface (swap providers by implementing this)."""

    model: str

    def generate_json(self, prompt: str, json_schema: dict[str, Any]) -> str:
        """Return the model's raw text, which should be JSON matching `json_schema`."""

    def generate_text(self, prompt: str) -> str:
        """Return plain text (used only to rephrase or translate trusted facts)."""

    def generate_json_from_document(self, prompt: str, data: bytes, mime_type: str,
                                    json_schema: dict[str, Any]) -> str:
        """Like generate_json, with a document (PDF or image) attached."""


class GeminiClient:
    """Gemini Flash through google-genai, temperature 0, schema-constrained JSON output."""

    def __init__(self, api_key: str, model: str) -> None:
        from google import genai

        self.model = model
        self._client = genai.Client(api_key=api_key)

    def generate_json(self, prompt: str, json_schema: dict[str, Any]) -> str:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0, response_mime_type="application/json", response_json_schema=json_schema,
            ),
        )
        return response.text or ""

    def generate_text(self, prompt: str) -> str:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self.model, contents=prompt, config=types.GenerateContentConfig(temperature=0),
        )
        return response.text or ""

    def generate_json_from_document(self, prompt: str, data: bytes, mime_type: str,
                                    json_schema: dict[str, Any]) -> str:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self.model,
            contents=[types.Part.from_bytes(data=data, mime_type=mime_type), prompt],
            config=types.GenerateContentConfig(
                temperature=0, response_mime_type="application/json", response_json_schema=json_schema,
            ),
        )
        return response.text or ""


def llm_from_settings(settings: Any) -> LlmClient | None:
    """A Gemini client when a key is configured, else None (deterministic mode)."""
    if not getattr(settings, "gemini_api_key", None):
        return None
    return GeminiClient(settings.gemini_api_key, settings.gemini_model)
