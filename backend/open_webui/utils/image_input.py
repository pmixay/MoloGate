"""Whether each model can read images, and keeping images away from models that can't.

Every model in the listing gets a ``reads_images`` flag for the chat composer, resolved in
this order:

1. The admin turned the model's Vision capability off: it can't read images.
2. The provider's ``/models`` metadata lists the model's input modalities and images aren't
   among them: it can't read images.
3. Otherwise it can, as before.

Custom models built on a base model inherit the base model's provider metadata.

Requests to a model that can't read images have their image parts replaced with a short
note, so earlier images in the chat, or images a tool returned, never reach it.
"""

from typing import Any

FLAG_KEY = 'reads_images'

IMAGE_PART_TYPES = {'image_url', 'input_image'}
IMAGE_OMITTED_NOTE = '[Image omitted: the selected model cannot read images.]'


def _modalities(value: Any) -> list[str] | None:
    if isinstance(value, str):
        # OpenRouter's summary form, e.g. "text+image->text".
        value = value.split('->')[0].split('+')
    if not isinstance(value, list):
        return None
    modalities = [item.strip().lower() for item in value if isinstance(item, str) and item.strip()]
    return modalities or None


def get_provider_reads_images(model: dict) -> bool | None:
    """What a provider's /models entry says about image input, or None when it says nothing."""
    raw = model.get('openai') if isinstance(model.get('openai'), dict) else model

    architecture = raw.get('architecture') if isinstance(raw.get('architecture'), dict) else {}
    modalities_info = raw.get('modalities') if isinstance(raw.get('modalities'), dict) else {}
    for candidate in (
        raw.get('input_modalities'),  # Codex / ChatGPT style
        architecture.get('input_modalities'),  # OpenRouter
        modalities_info.get('input'),
        architecture.get('modality'),
    ):
        modalities = _modalities(candidate)
        if modalities is not None:
            return 'image' in modalities

    if isinstance(raw.get('supports_vision'), bool):
        return raw['supports_vision']
    return None


def _admin_vision(model: dict) -> bool | None:
    info = model.get('info') if isinstance(model.get('info'), dict) else {}
    meta = info.get('meta') if isinstance(info.get('meta'), dict) else {}
    capabilities = meta.get('capabilities') if isinstance(meta.get('capabilities'), dict) else {}
    vision = capabilities.get('vision')
    return vision if isinstance(vision, bool) else None


def _resolve_provider(model: dict, models_by_id: dict, seen: set) -> bool | None:
    seen.add(model.get('id'))
    detected = get_provider_reads_images(model)
    if detected is not None:
        return detected

    info = model.get('info') if isinstance(model.get('info'), dict) else {}
    base_model_id = info.get('base_model_id')
    if base_model_id:
        base_model = models_by_id.get(base_model_id) or models_by_id.get(f'{base_model_id}:latest')
        if base_model and base_model.get('id') not in seen:
            return _resolve_provider(base_model, models_by_id, seen)
    return None


def apply_image_input_support(models: list[dict]) -> None:
    """Set ``reads_images`` on each model dict, in place."""
    models_by_id = {model.get('id'): model for model in models}
    resolved = {}
    for model in models:
        if _admin_vision(model) is False:
            resolved[model.get('id')] = False
        else:
            resolved[model.get('id')] = _resolve_provider(model, models_by_id, set()) is not False
    for model in models:
        model[FLAG_KEY] = resolved.get(model.get('id'), True)


def model_reads_images(model: dict | None) -> bool:
    if not isinstance(model, dict):
        return True
    if isinstance(model.get(FLAG_KEY), bool):
        return model[FLAG_KEY]
    # Direct connections and other models that skipped the listing.
    return _admin_vision(model) is not False and get_provider_reads_images(model) is not False


def _strip_parts(parts: list) -> list | None:
    """The parts without images, with a note where they were; None when there were none."""
    removed = [part for part in parts if isinstance(part, dict) and part.get('type') in IMAGE_PART_TYPES]
    if not removed:
        return None

    kept = [part for part in parts if not (isinstance(part, dict) and part.get('type') in IMAGE_PART_TYPES)]
    # Responses-style parts use input_text; Chat Completions parts use text.
    text_type = 'input_text' if any(part.get('type') == 'input_image' for part in removed) else 'text'
    kept.append({'type': text_type, 'text': IMAGE_OMITTED_NOTE})
    return kept


def strip_image_inputs(form_data: dict) -> dict:
    """Replace image parts in ``messages`` (Chat Completions) and ``input`` (Responses), in place."""
    for key in ('messages', 'input'):
        items = form_data.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in ('content', 'output'):
                parts = item.get(field)
                if isinstance(parts, list):
                    stripped = _strip_parts(parts)
                    if stripped is not None:
                        item[field] = stripped
    return form_data
