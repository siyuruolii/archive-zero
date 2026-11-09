"""阶段到模型配置的解析；模型循环保留已选配置，不在运行途中切换主模型。"""

from typing import Literal

from pydantic import Field, model_validator

from reading_agent.domain.models import StrictModel

from .provider import ChatProvider, Settings

Stage = Literal[
    "route",
    "deep_read",
    "discuss",
    "compact",
    "extract",
    "consolidate",
    "session_recall",
    "history_select",
]
STAGES = tuple(Stage.__args__)


class ModelCatalog(StrictModel):
    version: Literal[1] = 1
    default: str
    profiles: dict[str, Settings] = Field(min_length=1, max_length=20)
    stages: dict[Stage, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def references_exist(self):
        if self.default not in self.profiles or any(
            p not in self.profiles for p in self.stages.values()
        ):
            raise ValueError("unknown model profile")
        if any(
            not k or len(k) > 64 or not k.replace("-", "").replace("_", "").isalnum()
            for k in self.profiles
        ):
            raise ValueError("invalid model profile identity")
        return self


class ModelRouter:
    def __init__(self, catalog, client):
        self.catalog = catalog.model_copy(deep=True)
        self.providers = {k: ChatProvider(v, client) for k, v in self.catalog.profiles.items()}

    @property
    def model_name(self):
        return self.providers[self.catalog.default].model_name

    def for_stage(self, stage):
        if stage not in STAGES:
            raise ValueError("unknown model stage")
        return self.providers[self.catalog.stages.get(stage, self.catalog.default)]


def stage_provider(provider, stage):
    return provider.for_stage(stage) if isinstance(provider, ModelRouter) else provider
