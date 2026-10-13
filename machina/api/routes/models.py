"""Request/response models for the Machina HTTP API (moved verbatim from app.py)."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    provider: str | None = Field(default=None, max_length=50)
    goal: str | None = Field(default=None, max_length=10_000)
    emotion: str | None = None


class UpdateSessionRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, max_length=10_000)
    emotion: str | None = None


class CreateJobRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    kind: str = "interval"
    prompt: str | None = Field(default=None, max_length=5000)
    intervalSeconds: int = Field(default=300, ge=10, le=86_400)
    agent: str | None = Field(default=None, min_length=1, max_length=100)
    cronExpression: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    provider: str | None = Field(default=None, min_length=1, max_length=100)
    noAgent: bool = False
    scriptPath: str | None = Field(default=None, min_length=1, max_length=500)


class PromptRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)
    verbose: bool = False
    protocolVersion: int = Field(default=1, ge=1, le=2)


class CreateMemoryRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=20_000)
    category: str = "fact"
    importance: float = Field(default=0.5, ge=0, le=1)
    sessionId: uuid.UUID | None = None
    agent: str = "harness"


class CreatePersonaMemoryRequest(BaseModel):
    factId: uuid.UUID
    personaId: str = Field(..., min_length=1, max_length=100)
    interpretation: str = Field(..., min_length=1, max_length=20_000)
    emotionalValence: float = Field(default=0.0, ge=-1, le=1)
    emotionalArousal: float = Field(default=0.0, ge=0, le=1)
    confidence: float = Field(default=0.5, ge=0, le=1)


class CreateDocumentRequest(BaseModel):
    sessionId: uuid.UUID
    source: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1, max_length=1_000_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None


class ResolveApprovalRequest(BaseModel):
    verdict: str = Field(..., min_length=1, max_length=16)
    sessionId: uuid.UUID | None = None
    grant: str = Field(default="once", min_length=1, max_length=16)


class ResumeApprovalRequest(BaseModel):
    protocolVersion: int = Field(default=1, ge=1, le=2)
    sessionId: uuid.UUID
    turnId: str = Field(..., max_length=100)
    toolArgs: dict[str, Any] = Field(default_factory=dict)


class RpcRequest(BaseModel):
    method: str = Field(..., min_length=1, max_length=100)
    params: dict[str, Any] = Field(default_factory=dict)
