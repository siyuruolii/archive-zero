"""只有本阶段受控样例使用内存；不构成另一套产品存储。"""

import json
from importlib.resources import files

from reading_agent.agents.tool import Corpus
from reading_agent.domain.models import HistoricalMaterial, Material, Topic


def sample_corpus() -> Corpus:
    data = json.loads(files("reading_agent").joinpath("fixtures.json").read_text(encoding="utf-8"))
    return Corpus(
        topics={t["id"]: Topic.model_validate(t) for t in data["topics"]},
        materials={m["id"]: Material.model_validate(m) for m in data["materials"]},
        history={h["id"]: HistoricalMaterial.model_validate(h) for h in data["history"]},
    )
