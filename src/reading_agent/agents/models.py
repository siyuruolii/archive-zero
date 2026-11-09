"""模型输入与临时输出契约，不是数据库表。"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from reading_agent.domain.models import Identity, StrictModel, Text

Role = Literal["route", "deep_read", "discuss", "consolidate"]


class HistoryMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(min_length=1, max_length=128000)]


class Task(StrictModel):
    role: Role
    material_id: Identity
    topic_ids: list[Identity] = Field(min_length=1, max_length=20)
    question: str = ""
    short_summary: str = ""
    deep_read_guideline: str = ""
    tag_ids: list[Identity] = Field(default_factory=list, max_length=30)
    session_root: str = ""
    # 调用方只传入当前分支路径；持久化、分叉和压缩在第三阶段实现。
    # 可压缩历史交给 Loop 判断，不在到达旧 100 条阈值时提前挡住压缩服务。
    history: list[HistoryMessage] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def role_scope(self):
        if len(set(self.topic_ids)) != len(self.topic_ids):
            raise ValueError("duplicate Topic ID")
        if self.role != "route" and len(self.topic_ids) != 1:
            raise ValueError("deep_read/discuss require one Topic")
        if self.role == "discuss" and not self.question.strip():
            raise ValueError("discussion requires a question")
        if self.role != "discuss" and (self.history or self.session_root):
            raise ValueError("only discussion accepts conversation history")
        return self


class RouteItem(StrictModel):
    topic_id: Identity
    tag_ids: list[Identity] = Field(max_length=30)
    short_summary: Text
    match_reason: Text
    is_recommended: bool
    recommendation_reason: Text
    needs_deep_read: bool
    deep_read_guideline: str

    @model_validator(mode="after")
    def guideline_required(self):
        if self.needs_deep_read and not self.deep_read_guideline.strip():
            raise ValueError("deep reading requires a guideline")
        if len(set(self.tag_ids)) != len(self.tag_ids):
            raise ValueError("duplicate tags")
        return self


class RouteFinal(StrictModel):
    status: Literal["succeeded", "failed"]
    reason: Text
    routes: list[RouteItem] = Field(max_length=20)

    @model_validator(mode="after")
    def no_partial_failure(self):
        if self.status == "failed" and self.routes:
            raise ValueError("failed routing cannot contain partial results")
        return self


class Coverage(StrictModel):
    material_id: Identity
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("empty reading coverage")
        return self


class DeepFinal(StrictModel):
    status: Literal["succeeded", "failed"]
    reason: Text
    deep_summary: str = Field(max_length=16000)
    coverage: list[Coverage] = Field(max_length=32)
    limitations: str = Field(max_length=4000)

    @model_validator(mode="after")
    def actual_result(self):
        if self.status == "succeeded" and (not self.deep_summary.strip() or not self.coverage):
            raise ValueError("deep reading success needs summary and coverage")
        if self.status == "failed" and (self.deep_summary or self.coverage):
            raise ValueError("failure cannot impersonate completed reading")
        return self


class DiscussFinal(StrictModel):
    status: Literal["succeeded", "failed"]
    reason: Text
    answer: str = Field(max_length=16000)

    @model_validator(mode="after")
    def actual_answer(self):
        if self.status == "succeeded" and not self.answer.strip():
            raise ValueError("discussion success requires an answer")
        if self.status == "failed" and self.answer:
            raise ValueError("failure answer must be empty; use reason")
        return self


class ConsolidationFinal(StrictModel):
    status: Literal["succeeded", "failed"]
    reason: Text
    summary: str = Field(max_length=4000)


FINAL_MODELS = {
    "route": RouteFinal,
    "deep_read": DeepFinal,
    "discuss": DiscussFinal,
    "consolidate": ConsolidationFinal,
}
