"""集中登记各业务模块的工具定义。"""

from . import discovery, filesystem, material, memory, session, shell

SPECS = (
    material.TOOLS + memory.TOOLS + session.TOOLS + filesystem.TOOLS + shell.TOOLS + discovery.TOOLS
)
REGISTRY = {spec.name: spec for spec in SPECS}
if len(REGISTRY) != len(SPECS):
    raise ValueError("duplicate tool name")
