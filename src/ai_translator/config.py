import tomllib
from dataclasses import dataclass
from pathlib import Path

STAGES = ("glossary", "translate", "edit")
DEFAULT_MODELS = {
    "codex": ("gpt-6.2-sol",) * 3,
    "openai-api": ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol"),
    "claude-code": ("haiku", "sonnet", "sonnet"),
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


def load_config(path: Path | None, provider: str | None) -> Settings:
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path else {}
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
        default_effort = "medium" if stage == "edit" else "low"
        reasoning = table.get("reasoning", default_effort)
        if not isinstance(name, str) or not name.strip() or not isinstance(reasoning, str):
            raise ValueError(f"[{stage}] model и reasoning должны быть строками.")
        models[stage] = Model(name.strip(), reasoning.strip())

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
