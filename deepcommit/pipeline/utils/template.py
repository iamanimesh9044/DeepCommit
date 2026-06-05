"""Template rendering utilities for string and JSON template substitution."""

from __future__ import annotations

import re
from typing import Any, Dict


def render_template_string(template: str, variables: Dict[str, str]) -> str:
    """Render a template string by replacing {{variable}} placeholders.

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
