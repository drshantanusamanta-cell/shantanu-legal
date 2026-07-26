"""Provider-agnostic LLM interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResponse:
    """Normalised response shape across providers."""

    text: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    # each citation: {"url","title","cited_text","source"}
    searches_performed: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    provider: str = ""
    model: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class Attachment:
    """A file to send to the model (PDF or image)."""

    data_b64: str
    media_type: str          # "application/pdf" | "image/png" | "image/jpeg"
    filename: str = ""

    @property
    def is_pdf(self) -> bool:
        return self.media_type == "application/pdf"


class LLMProvider(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def generate(
        self,
        system: str,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 8000,
        temperature: float = 0.2,
        enable_search: bool = True,
        allowed_domains: list[str] | None = None,
        max_searches: int = 8,
        attachments: list[Attachment] | None = None,
    ) -> LLMResponse: ...
