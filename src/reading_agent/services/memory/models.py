"""记忆业务的持久化和提取契约。"""

from typing import Literal

from pydantic import Field

from reading_agent.domain.models import StrictModel, Text


class Candidate(StrictModel):
    content: Text
    quote: Text
    explicit_global: bool


class Extraction(StrictModel):
    profiles: list[Candidate] = Field(max_length=3)


class MemoryDocument(StrictModel):
    """MD 正文和元数据共同构成权威记录，SQLite 仅是派生索引。"""

    version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    category: Literal["profile", "user_reading", "agent_reading"]
    topic_id: str | None = None
    content: str = Field(max_length=16000)
    origin: str = "user_statement"
    evidence: list[dict] = Field(default_factory=list, max_length=30)
    created_at: str
    updated_at: str
    replaces: str | None = None
    superseded_by: str | None = None
    deleted: bool = False
    details: dict = Field(default_factory=dict)


class SessionQuote(StrictModel):
    entry_id: str
    quote: str = Field(min_length=1, max_length=1200)


class SessionSummary(StrictModel):
    summary: str = Field(min_length=1, max_length=4000)
    evidence: list[SessionQuote] = Field(min_length=1, max_length=8)


READING_FOLDERS = {"user_reading": "user-reading", "agent_reading": "agent-reading"}
