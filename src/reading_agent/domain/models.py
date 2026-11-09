"""业务实体与基础校验，不依赖 Agent 执行层。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]
Identity = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Tag(StrictModel):
    id: Identity
    name: Text
    description: str = ""


class Topic(StrictModel):
    id: Identity
    name: Text
    description: str = ""
    tags: list[Tag] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_tags(self):
        if len({tag.id for tag in self.tags}) != len(self.tags):
            raise ValueError("duplicate tag ID")
        return self


class Material(StrictModel):
    id: Identity
    title: Text
    url: str = ""
    source_summary: str = ""
    # 阶段一正文由固定资料集提供；不在初始上下文偷塞全文。
    body: str | None = None


class HistoricalMaterial(StrictModel):
    id: Identity  # 未来对应 TopicMaterial ID
    topic_id: Identity
    material_id: Identity
    title: Text
    short_summary: Text
    deep_summary: str = ""
    published_at: str
