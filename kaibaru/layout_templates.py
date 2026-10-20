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


def _as_page(page_content):
    data = page_content
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return None
    if not isinstance(data, dict):
        return None
    return data


def merge_carried_sections(template_page, old_page_content):
    """Keep event and past sections when a template replaces the page.

    Those sections are how saved events stay on the page. Their ids are kept
    when the new template does not already use them. A clash returns a map of
    old id to new id so the event rows can be pointed at the new section.
    """
    page = json.loads(json.dumps(template_page))
    sections = page.setdefault("sections", {})
    old = _as_page(old_page_content) or {}
    old_sections = old.get("sections") or {}
    if not isinstance(old_sections, dict):
        old_sections = {}

    carried = [
        section
        for section in old_sections.values()
        if isinstance(section, dict) and section.get("type") in {"event", "past"}
    ]
    carried.sort(key=lambda section: (section.get("order") or 0, section.get("id") or 0))

    used_slugs = set()
    used_ids = set()
    orders = []
    for section in sections.values():
        if not isinstance(section, dict):
            continue
        if section.get("slug"):
            used_slugs.add(section["slug"])
        used_slugs.update(section.get("previous_slugs") or [])
        if section.get("id") is not None:
            used_ids.add(int(section["id"]))
        orders.append(int(section.get("order") or 0))

    next_order = (max(orders) if orders else 0) + 1
    highest_id = max(used_ids or [0])
    id_remap = {}

    for section in carried:
        copied = json.loads(json.dumps(section))
        old_id = int(copied["id"])
        new_id = old_id
        if new_id in used_ids:
            new_id = highest_id + 1
            id_remap[old_id] = new_id
        highest_id = max(highest_id, new_id)
        used_ids.add(new_id)
        copied["id"] = new_id
        copied["order"] = next_order
        next_order += 1

        slug = copied.get("slug") or ""
        if slug and slug in used_slugs:
            previous = list(copied.get("previous_slugs") or [])
            if slug not in previous:
                previous.append(slug)
            base = slug
            suffix = 2
            while f"{base}-{suffix}" in used_slugs:
                suffix += 1
            copied["slug"] = f"{base}-{suffix}"
            copied["previous_slugs"] = previous
        if copied.get("slug"):
            used_slugs.add(copied["slug"])
        used_slugs.update(copied.get("previous_slugs") or [])

        sections[str(new_id)] = copied

    page["next_section_id"] = max(int(page.get("next_section_id") or 1), highest_id + 1)
    return page, id_remap


def page_content_for_club(layout):
    """Return page_content in the format stored on Club.page_content.

    The site editor stores the document as a JSON *string* inside the
    JSONField and json.loads() it again when sections are edited, so the
    template is serialised the same way.
    """
    return json.dumps(load_page_content(layout), ensure_ascii=False)
