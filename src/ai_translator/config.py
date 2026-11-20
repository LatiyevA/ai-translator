import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

STAGES = ("glossary", "translate", "edit", "review")
DEFAULT_MODELS = {
    "codex": ("gpt-6.2-sol",) * 4,
    "openai-api": ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol", "gpt-6.2-sol"),
    "claude-code": ("haiku", "sonnet", "sonnet", "sonnet"),
    "antigravity": ("gemini-3.8-flash",) * 4,
}


@dataclass(frozen=True)
class Model:
    name: str
    reasoning: str = ""


@dataclass(frozen=True)
class Settings:
    provider: str
    models: dict[str, Model]
    batch_chars: int
    timeout: int
    fandom: str
    style: str


class ModelSettingError(ValueError):
    def __init__(self, stage: str, field: str, message: str):
        super().__init__(f"[{stage}] {field} {message}")
        self.stage = stage
        self.field = field


def load_config(path: Path | None, provider: str | None) -> Settings:
    return parse_config(path.read_text(encoding="utf-8") if path else "", provider)


def parse_config(content: str, provider: str | None = None) -> Settings:
    data = tomllib.loads(content)
    if unknown := data.keys() - {"provider", "batch_chars", "timeout", "fandom", "style", *STAGES}:
        raise ValueError(f"Неизвестные настройки: {', '.join(sorted(unknown))}")

    provider = provider or data.get("provider", "codex")
    if not isinstance(provider, str) or provider not in DEFAULT_MODELS:
        raise ValueError(f"Допустимые провайдеры: {', '.join(DEFAULT_MODELS)}.")
    models = {}
    for stage, default_model in zip(STAGES, DEFAULT_MODELS[provider]):
        table = data.get(stage, {})
        if not isinstance(table, dict) or table.keys() - {"model", "reasoning"}:
            raise ValueError(f"[{stage}] принимает только model и reasoning.")
        name = table.get("model", default_model)
        default_effort = "medium" if stage in ("edit", "review") else "low"
        reasoning = table.get("reasoning", default_effort)
        if not isinstance(name, str) or not name.strip():
            raise ModelSettingError(stage, "model", "должен быть непустой строкой.")
        if not isinstance(reasoning, str):
            raise ModelSettingError(stage, "reasoning", "должен быть строкой.")
        name, reasoning = name.strip(), reasoning.strip()
        if name.startswith("-") or any(char.isspace() or not char.isprintable() for char in name):
            raise ModelSettingError(stage, "model", "должен быть идентификатором без пробелов и начального дефиса.")
        if reasoning and not re.fullmatch(r"[a-z][a-z0-9_-]*", reasoning):
            raise ModelSettingError(stage, "reasoning", "должен быть одним значением, например low, или пустой строкой.")
        models[stage] = Model(name, reasoning)

    batch_chars = data.get("batch_chars", 2400)
    timeout = data.get("timeout", 300)
    if type(batch_chars) is not int or not 400 <= batch_chars <= 12000:
        raise ValueError("batch_chars должен быть целым числом от 400 до 12000.")
    if type(timeout) is not int or timeout < 1:
        raise ValueError("timeout должен быть положительным целым числом.")
    fandom, style = data.get("fandom", ""), data.get("style", "")
    if not isinstance(fandom, str) or not isinstance(style, str):
        raise ValueError("fandom и style должны быть строками.")
    return Settings(provider, models, batch_chars, timeout, fandom, style)
