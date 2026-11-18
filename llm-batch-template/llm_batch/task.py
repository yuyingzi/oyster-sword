import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Task:
    definition: dict[str, object]
    system_prompt: str
    is_valid: Callable[[object], bool]


def load_task(path: Path) -> Task:
    try:
        definition = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, RecursionError) as error:
        raise ValueError(f"{path}: invalid task JSON") from error
    if not isinstance(definition, dict) or definition.keys() - {"prompt", "labels", "output_schema"}:
        raise ValueError(f"{path}: expected a task object with prompt, labels and output_schema")

    prompt = definition.get("prompt")
    labels = definition.get("labels", [])
    schema = definition.get("output_schema")
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(schema, dict) or not schema:
        raise ValueError(f"{path}: fill in prompt and output_schema before using a real provider")
    if not isinstance(labels, list) or any(not isinstance(label, str) or not label.strip() for label in labels):
        raise ValueError(f"{path}: labels must be a list of non-empty strings")

    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
        from referencing import Registry
    except ModuleNotFoundError as error:
        raise ValueError("install the task dependencies with: python -m pip install -r requirements.txt") from error

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ValueError(f"{path}: invalid output_schema: {error.message}") from error

    validator = Draft202012Validator(schema, registry=Registry())
    schema_text = json.dumps(schema, ensure_ascii=False, sort_keys=True, allow_nan=False)
    sections = [prompt.strip()]
    if labels:
        sections.append("Label vocabulary: " + json.dumps(labels, ensure_ascii=False))
    sections.append("Return one JSON object matching this output schema, with no surrounding text:\n" + schema_text)
    system_prompt = "\n\n".join(sections)
    system_prompt.encode("utf-8")
    return Task({"prompt": prompt.strip(), "labels": labels, "output_schema": schema}, system_prompt, validator.is_valid)
