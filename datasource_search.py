import json
import re
from typing import Callable, Optional


def extract_datasource_name(question: str) -> Optional[str]:
    """Try to extract a datasource name from a natural language question."""
    if not question:
        return None

    text = question.strip()

    quoted_patterns = [
        r'(?:datasource|data source)\s*(?:ชื่อ|name)?\s*["“”\']([^"“”\']+)["“”\']',
        r'(?:หา|ค้นหา|search|find)\s+(?:datasource|data source)\s*(?:ชื่อ|name)?\s*["“”\']([^"“”\']+)["“”\']',
    ]
    for pattern in quoted_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()

    # Fallback: look for a token after the word "ชื่อ"
    fallback = re.search(r'(?:ชื่อ|name)\s*[:\-]?\s*([A-Za-z0-9_.()\/\-\s]+)', text, flags=re.IGNORECASE)
    if fallback:
        candidate = fallback.group(1).strip().strip('"').strip("'")
        if candidate:
            return candidate

    return None


def build_datasource_name_filter(name: str) -> str:
    """Build a Tableau list-datasources filter that searches by name substring."""
    cleaned = name.strip().strip('"').strip("'")
    return f"name:eq:*{cleaned}*"


def resolve_datasource_luid(
    question: str,
    list_datasources_fn: Callable[[str], tuple[str, bool]],
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a datasource name from the question into a Tableau datasource LUID."""
    name = extract_datasource_name(question)
    if not name:
        return None, None

    filter_expr = build_datasource_name_filter(name)
    result_text, is_error = list_datasources_fn(filter_expr)
    if is_error or not result_text:
        return None, None

    try:
        payload = json.loads(result_text)
    except (TypeError, ValueError):
        return None, None

    items = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("dataSources", "datasources", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break

    exact_match = None
    for item in items:
        if not isinstance(item, dict):
            continue
        item_name = item.get("name") or item.get("displayName") or item.get("contentUrl")
        item_luid = item.get("id") or item.get("luid") or item.get("contentUrl")
        if not item_name or not item_luid:
            continue
        if item_name.lower() == name.lower():
            exact_match = str(item_luid)
            break
        if name.lower() in item_name.lower():
            return str(item_luid), item_name

    if exact_match:
        return exact_match, name

    return None, None


def resolve_effective_datasource(
    question: str,
    selected_luid: Optional[str],
    default_luid: Optional[str],
    list_datasources_fn: Callable[[str], tuple[str, bool]],
) -> tuple[Optional[str], Optional[str], bool]:
    """Choose the datasource to use for the question, preferring explicit mentions and remembering prior selections."""
    if not question:
        return selected_luid or default_luid, None, False

    name = extract_datasource_name(question)
    if name:
        resolved_luid, resolved_name = resolve_datasource_luid(question, list_datasources_fn)
        if resolved_luid:
            return resolved_luid, resolved_name or name, True

    return selected_luid or default_luid, None, False
