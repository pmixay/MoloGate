"""Which model generations the model list shows.

Providers keep listing models long after newer ones replace them. For the labs in ``CURRENT``,
only the current generation is shown: older ones are flagged hidden (``info.meta.hidden``), the
way an admin hides a model. They leave the selectors but stay in the list, so chats and presets
that already use one keep working, and an admin can bring one back from Admin > Models.

Labs without a rule here are left alone, and so are ids a rule doesn't judge (tool models,
aliases without a version). To follow a new release, change the rule for its lab.
"""

import re
from dataclasses import dataclass, field

from open_webui.utils.model_ids import model_version, normalize_model_name
from open_webui.utils.model_labs import detect_lab_from_text


@dataclass(frozen=True)
class Generation:
    # The ids this rule judges, matched after any organization prefix.
    scope: re.Pattern
    # The version a model must start with: (5, 3) takes 5.3 and 5.3.1, (6,) takes 6 and 6.1.
    prefix: tuple[int, ...]
    # Tiers that only count from a later version within the generation, e.g. Sol from 6.1.
    tier_minimum: dict[str, tuple[int, ...]] = field(default_factory=dict)


CURRENT: dict[str, Generation] = {
    # GLM-5.3 family: glm-5.3, glm-5.3-flash, ...
    'zai': Generation(scope=re.compile(r'glm'), prefix=(5, 3)),
    # GPT-6 generation: gpt-6, gpt-luna-6, ...; Sol only from GPT-6.1.
    'openai': Generation(
        scope=re.compile(r'(gpt|chatgpt|o\d)'),
        prefix=(6,),
        tier_minimum={'sol': (6, 1)},
    ),
}


def is_current_generation(model_id: str) -> bool:
    """False for a model of an older generation than its lab's rule allows."""
    rule = CURRENT.get(detect_lab_from_text(model_id))
    if rule is None:
        return True

    name = normalize_model_name(model_id).rsplit('/', 1)[-1]
    version = model_version(name)
    if not rule.scope.match(name) or not version:
        return True
    if version[: len(rule.prefix)] != rule.prefix:
        return False

    tiers = set(re.split(r'[-:.]+', name))
    return all(tier not in tiers or version[: len(minimum)] >= minimum for tier, minimum in rule.tier_minimum.items())
