"""环境变量优先；Windows 本地配置使用当前用户 DPAPI 加密密钥。"""

import ctypes
import json
import os
import sys
import uuid
from pathlib import Path


def data_dir():
    default = (
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ArchiveZero" / "data"
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[3] / ".dev-data"
    )
    return Path(os.environ.get("AGENT_DATA_DIR", str(default))).resolve()


class Blob(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def crypt(data, decrypt=False):
    if os.name != "nt":
        raise ValueError(
            "DPAPI configuration requires Windows; use environment variables elsewhere"
        )
    source = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    if decrypt:
        success = library.CryptUnprotectData(
            ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)
        )
    else:
        success = library.CryptProtectData(
            ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)
        )
    if not success:
        raise ValueError("local credential protection failed")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    try:
        return ctypes.string_at(outgoing.data, outgoing.length)
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def save_model(key, model="deepseek-flash", base_url="https://api.deepseek.com"):
    from reading_agent.agents.provider import Settings

    settings = Settings(api_key=key, model=model, base_url=base_url)
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": settings.model,
        "base_url": settings.base_url,
        "encrypted_key": crypt(settings.api_key.get_secret_value().encode()).hex(),
    }
    temporary = directory / "model.json.tmp"
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(directory / "model.json")


def _legacy_model(directory=None):
    from reading_agent.agents.provider import Settings

    if os.environ.get("AGENT_API_KEY"):
        return Settings.from_env()
    config = json.loads((Path(directory or data_dir()) / "model.json").read_text(encoding="utf-8"))
    return Settings(
        api_key=crypt(bytes.fromhex(config["encrypted_key"]), decrypt=True).decode(),
        model=config["model"],
        base_url=config["base_url"],
    )


def load_catalog(directory=None):
    from reading_agent.agents.model_router import ModelCatalog
    from reading_agent.agents.provider import Settings

    path = Path(directory or data_dir()) / "models.json"
    if not path.exists():
        return _memory_overrides(
            ModelCatalog(default="default", profiles={"default": _legacy_model(directory)})
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for profile in payload["profiles"].values():
            profile["api_key"] = crypt(
                bytes.fromhex(profile.pop("encrypted_key")), decrypt=True
            ).decode()
        catalog = ModelCatalog.model_validate(payload)
        if os.environ.get("AGENT_API_KEY"):
            catalog.profiles["environment"] = Settings.from_env()
            catalog.default = "environment"
        return _memory_overrides(catalog)
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid model catalog") from None


def _memory_overrides(catalog):
    from reading_agent.agents.provider import Settings

    for stage in ("extract", "consolidate"):
        prefix = "AGENT_MEMORY_" + stage.upper() + "_"
        overrides = {
            field: os.environ[prefix + suffix]
            for field, suffix in (
                ("model", "MODEL"),
                ("base_url", "BASE_URL"),
                ("api_key", "API_KEY"),
            )
            if os.environ.get(prefix + suffix)
        }
        if overrides and stage not in catalog.stages:
            values = catalog.profiles[catalog.default].model_dump()
            try:
                catalog.profiles["legacy_" + stage] = Settings.model_validate(values | overrides)
            except ValueError:
                raise ValueError("invalid memory model configuration") from None
            catalog.stages[stage] = "legacy_" + stage
    return catalog


def load_model():
    catalog = load_catalog()
    return catalog.profiles[catalog.default]


def save_catalog(catalog, directory=None):
    from reading_agent.infrastructure.locking import FileLock

    directory = Path(directory or data_dir())
    directory.mkdir(parents=True, exist_ok=True)
    payload = catalog.model_dump(mode="json", exclude={"profiles"})
    payload["profiles"] = {}
    for name, settings in catalog.profiles.items():
        value = settings.model_dump(mode="json", exclude={"api_key"})
        value["encrypted_key"] = crypt(settings.api_key.get_secret_value().encode()).hex()
        payload["profiles"][name] = value
    temporary = directory / ("models-" + uuid.uuid4().hex + ".tmp")
    with FileLock(directory / ".models.lock"):
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(directory / "models.json")
        finally:
            temporary.unlink(missing_ok=True)


def public_catalog(catalog):
    from reading_agent.agents.model_router import STAGES

    return {
        "version": 1,
        "default": catalog.default if catalog else "",
        "profiles": {
            name: {**settings.model_dump(mode="json", exclude={"api_key"}), "has_api_key": True}
            for name, settings in (catalog.profiles.items() if catalog else [])
        },
        "stages": dict(catalog.stages) if catalog else {},
        "available_stages": list(STAGES),
        "environment_override": bool(os.environ.get("AGENT_API_KEY")),
    }


def catalog_from_input(payload, previous=None):
    """空密钥仅复用同名旧配置，不把密钥返回到前端。"""
    from reading_agent.agents.model_router import ModelCatalog

    try:
        payload = {**payload, "profiles": {k: dict(v) for k, v in payload["profiles"].items()}}
        payload.pop("available_stages", None)
        payload.pop("environment_override", None)
        for name, profile in payload["profiles"].items():
            profile.pop("has_api_key", None)
            if not profile.get("api_key"):
                if previous is None or name not in previous.profiles:
                    raise ValueError("missing credentials")
                profile["api_key"] = previous.profiles[name].api_key
        return ModelCatalog.model_validate(payload)
    except (KeyError, TypeError, ValueError):
        raise ValueError(
            "invalid model settings; check profiles, credentials and stage assignments"
        ) from None


def memory_provider(provider, purpose):
    """提取/沉淀可独立选模型，未指定项复用现有供应商配置。"""
    from reading_agent.agents.provider import ChatProvider, Settings

    if purpose not in {"extract", "consolidate"}:
        raise ValueError("invalid memory model purpose")
    if not isinstance(provider, ChatProvider):
        return None
    prefix = "AGENT_MEMORY_" + purpose.upper()
    values = provider.settings.model_dump()
    changed = False
    for field, suffix in (("model", "MODEL"), ("base_url", "BASE_URL"), ("api_key", "API_KEY")):
        if os.environ.get(prefix + "_" + suffix):
            values[field] = os.environ[prefix + "_" + suffix]
            changed = True
    if not changed:
        return None
    try:
        settings = Settings.model_validate(values)
    except ValueError:
        raise ValueError("invalid memory model configuration") from None
    return ChatProvider(settings, provider.client)


def memory_policy():
    from reading_agent.services.memory_consolidation import ConsolidationPolicy

    values = {}
    fields = {
        "min_new": int,
        "cooldown_seconds": float,
        "interval_seconds": float,
        "batch_size": int,
        "context_size": int,
        "max_model_calls": int,
        "max_seconds": float,
    }
    for field, convert in fields.items():
        value = os.environ.get("AGENT_MEMORY_CONSOLIDATE_" + field.upper())
        if value is not None:
            values[field] = convert(value)
    enabled = os.environ.get("AGENT_MEMORY_CONSOLIDATE_ENABLED")
    if enabled is not None:
        if enabled.lower() not in {"true", "false"}:
            raise ValueError("invalid memory consolidation enabled setting")
        values["enabled"] = enabled.lower() == "true"
    return ConsolidationPolicy.model_validate(values)
