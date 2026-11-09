"""版本化评测数据；参考要求与模型输入分离。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from reading_agent.agents.harness import Budget
from reading_agent.agents.models import Task
from reading_agent.domain.models import StrictModel


class Expected(StrictModel):
    status: Literal["succeeded", "failed"] = "succeeded"
    end_reason: str | None = None
    topic_ids: list[str] | None = None
    recommended: dict[str, bool] = Field(default_factory=dict)
    must_read: bool = False
    tool_errors: list[str] = Field(default_factory=list)
    files: dict[str, str | None] = Field(default_factory=dict)
    required_points: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    forbidden_output_literals: list[str] = Field(default_factory=list)


class Case(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    category: Literal["route", "deep_read", "discuss", "memory", "tools"]
    execution: Literal["agent", "fault_contract"] = "agent"
    task: Task
    title: str
    body: str | None
    summary: str = ""
    memories: list[dict] = Field(default_factory=list, max_length=10)
    workspace: bool = False
    cancel_on_text: bool = False
    expected: Expected
    budget: Budget = Field(
        default_factory=lambda: Budget(max_model_calls=6, max_seconds=30, retry_delay=0)
    )


class Dataset(StrictModel):
    version: Literal[1] = 1
    reference_status: Literal["draft", "reviewed"] = "draft"
    as_of: str
    cases: list[Case] = Field(min_length=1, max_length=500)

    @field_validator("as_of")
    @classmethod
    def aware_date(cls, value):
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("as_of requires a timezone")
        return value

    @model_validator(mode="after")
    def unique(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate eval case ID")
        return self
