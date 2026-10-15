"""Pydantic models for the REST API."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class ItemCreate(BaseModel):
    """Payload for POST /api/items."""

    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    content_type: Literal["text", "markdown", "json"] = "text"
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    callback_url: Optional[str] = None
    callback_secret: Optional[str] = None


class Decision(BaseModel):
    """Payload for POST /api/items/{id}/decision."""

    action: Literal["approve", "reject", "edit"]
    content: Optional[str] = None
    reason: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def edit_requires_content(self) -> "Decision":
        if self.action == "edit" and (
            self.content is None or not self.content.strip()
        ):
            raise ValueError("'content' is required when action is 'edit'")
        return self


class WebhookInfo(BaseModel):
    status: str
    attempts: int
    last_error: Optional[str] = None
    delivered_at: Optional[str] = None


class ItemSummary(BaseModel):
    id: int
    title: str
    status: str
    content_type: str
    tags: List[str]
    content_preview: str
    created_at: str
    decided_at: Optional[str] = None


class ItemDetail(ItemSummary):
    content: str
    final_content: Optional[str] = None
    reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    callback_url: Optional[str] = None
    webhook: Optional[WebhookInfo] = None
