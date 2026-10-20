"""Ready-made page_content templates, one JSON file per Club.layout.

The files live in ``kaibaru/layout/`` and are named after the layout key with
"/" replaced by "_" (``bjj/grappling`` -> ``bjj_grappling.json``).  Each file
holds a full ``page_content`` document (header, slideshow, custom sections,
schedule, plans, teacher, join and member sections).
"""
import json
from functools import lru_cache
from pathlib import Path

from .models import Club

LAYOUT_DIR = Path(__file__).resolve().parent / "layout"

TEMPLATE_KEYS = [key for key, _label in Club.LAYOUT_CHOICES]


def is_template_key(value):
    return value in TEMPLATE_KEYS


def template_path(layout):
    if not is_template_key(layout):
        raise ValueError(f"Unknown layout template: {layout!r}")
    return LAYOUT_DIR / f"{layout.replace('/', '_')}.json"


@lru_cache(maxsize=None)
def _load(layout):
    with template_path(layout).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_page_content(layout):
    """Return a fresh page_content dict for the layout template."""
    return json.loads(json.dumps(_load(layout)))


def section_ids_of_type(page_content, section_type):
    """Ids of sections of one type, from a page_content dict or JSON string."""
    data = page_content
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return []
    if not isinstance(data, dict):
        return []
    sections = data.get("sections") or {}
    if not isinstance(sections, dict):
        return []
    ids = []
    for section in sections.values():
        if (
            isinstance(section, dict)
            and section.get("type") == section_type
            and section.get("id") is not None
        ):
            ids.append(section["id"])
    return ids


def page_content_for_club(layout):
    """Return page_content in the format stored on Club.page_content.

    The site editor stores the document as a JSON *string* inside the
    JSONField and json.loads() it again when sections are edited, so the
    template is serialised the same way.
    """
    return json.dumps(load_page_content(layout), ensure_ascii=False)
