"""Template rendering utilities for string and JSON template substitution."""

from __future__ import annotations

import re
from typing import Any, Dict

_FALSY = {"", "0", "false", "no", "none", "null"}

# {{#if var}}...{{/if}}. Blocks do not nest. The newline right after the opening tag and right after the
# closing tag is swallowed, so a block whose tags sit on their own lines renders to exactly nothing when false.
_IF_BLOCK_RE = re.compile(r"\{\{#if\s+([a-zA-Z0-9_]+)\s*\}\}\n?(.*?)\{\{/if\}\}\n?", re.DOTALL)


def _is_truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSY


def render_template_string(template: str, variables: Dict[str, str]) -> str:
    """Render a template string by replacing {{variable}} placeholders.

    Optional conditional blocks ``{{#if name}}...{{/if}}`` are kept only when ``name`` is set and truthy
    (not missing, empty, "0", "false", "no", "none"). Templates without the syntax render exactly as before.

    Args:
        template: Template string with {{variable}} placeholders
        variables: Dict mapping variable names to replacement values

    Returns:
        Rendered string with placeholders replaced

    Example:
        >>> render_template_string("Hello {{name}}!", {"name": "World"})
        'Hello World!'
    """

    def replace_var(match):
        var_name = match.group(1).strip()
        return str(variables.get(var_name, match.group(0)))

    if "{{#if" in template:
        template = _IF_BLOCK_RE.sub(lambda m: m.group(2) if _is_truthy(variables.get(m.group(1))) else "", template)

    # Replace {{variable}} with actual values
    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", replace_var, template)


def render_template_recursive(obj: Any, variables: Dict[str, str]) -> Any:
    """Recursively render templates in a nested data structure.

    Walks through dicts, lists, and strings, replacing {{variable}} placeholders
    in all string values.

    Args:
        obj: Data structure to render (can be dict, list, str, or primitive types)
        variables: Dict mapping variable names to replacement values

    Returns:
        Rendered data structure with all placeholders replaced

    Example:
        >>> data = {
        ...     "path": "{{BASE}}/data",
        ...     "config": {
        ...         "name": "{{NAME}}",
        ...         "items": ["{{ITEM1}}", "{{ITEM2}}"]
        ...     }
        ... }
        >>> vars = {"BASE": "/root", "NAME": "test", "ITEM1": "a", "ITEM2": "b"}
        >>> render_template_recursive(data, vars)
        {
            'path': '/root/data',
            'config': {
                'name': 'test',
                'items': ['a', 'b']
            }
        }
    """
    if isinstance(obj, dict):
        return {k: render_template_recursive(v, variables) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [render_template_recursive(item, variables) for item in obj]
    elif isinstance(obj, str):
        return render_template_string(obj, variables)
    else:
        # Return primitives (int, float, bool, None) as-is
        return obj
