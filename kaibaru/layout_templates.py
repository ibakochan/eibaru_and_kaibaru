"""Ready-made page_content templates, one JSON file per Club.layout.

The files live in ``kaibaru/layout/`` and are named after the layout key with
"/" replaced by "_" (``bjj/grappling`` -> ``bjj_grappling.json``).  Each file
holds a full ``page_content`` document (header, slideshow, custom sections,
schedule, plans, teacher, join and member sections).
"""
import json
import re
import uuid
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


def clean_sticky_nav(page_content, payload):
    """Validate the bottom buttons stored on a page.

    The first section after the header cannot be the hide point. Hiding
    starts there, so picking it would hide the buttons on the opening screen.
    """
    if not isinstance(payload, dict):
        raise ValueError("下のボタンの内容が正しくありません。")
    sections = page_content.get("sections") or {}
    if not isinstance(sections, dict):
        sections = {}
    ordered = sorted(
        (section for section in sections.values() if isinstance(section, dict)),
        key=lambda section: (section.get("order") or 0, section.get("id") or 0),
    )
    flow = [section for section in ordered if section.get("type") != "header"]
    first_id = flow[0].get("id") if flow else None

    hide_raw = payload.get("hide_from_section_id")
    hide_id = None
    if hide_raw not in (None, ""):
        try:
            hide_id = int(hide_raw)
        except (TypeError, ValueError):
            raise ValueError("隠すセクションが正しくありません。")
        target = sections.get(str(hide_id))
        if not isinstance(target, dict) or target.get("type") == "header":
            raise ValueError("そのセクションからは隠せません。")
        if first_id is not None and hide_id == int(first_id):
            raise ValueError("先頭のセクションは、下のボタンを隠す位置にできません。")

    raw_buttons = payload.get("buttons") if "buttons" in payload else []
    if raw_buttons is None:
        raw_buttons = []
    if not isinstance(raw_buttons, list):
        raise ValueError("下のボタンの内容が正しくありません。")
    if len(raw_buttons) > 8:
        raise ValueError("下のボタンは8個までです。")

    cleaned = []
    seen = set()
    for button in raw_buttons:
        if not isinstance(button, dict):
            raise ValueError("下のボタンの内容が正しくありません。")
        label = str(button.get("label") or "").strip()
        if not label or len(label) > 40:
            raise ValueError("ボタンの文字を40字以内で入力してください。")
        kind = button.get("kind")
        raw_id = str(button.get("id") or "").strip()
        if not raw_id or len(raw_id) > 64:
            raw_id = str(uuid.uuid4())
        if raw_id in seen:
            raw_id = str(uuid.uuid4())
        seen.add(raw_id)
        item = {"id": raw_id, "label": label, "kind": kind}
        if kind == "section":
            try:
                section_id = int(button.get("section_id"))
            except (TypeError, ValueError):
                raise ValueError("移動先のセクションを選んでください。")
            if str(section_id) not in sections:
                raise ValueError("移動先のセクションがありません。")
            item["section_id"] = section_id
        elif kind == "link":
            url = str(button.get("url") or "").strip()
            if not (url.startswith("http://") or url.startswith("https://")) or len(url) > 500:
                raise ValueError("リンクは http:// か https:// で始めてください。")
            item["url"] = url
        else:
            raise ValueError("ボタンの種類が正しくありません。")
        cleaned.append(item)
    return {
        "hide_from_section_id": hide_id,
        "buttons": cleaned,
        "style": clean_sticky_style(payload.get("style")),
    }


_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_STICKY_SHAPES = {"pill", "rounded", "square"}


def clean_sticky_style(raw):
    """Keep a small shared look for the bottom buttons and their tray.

    A missing or invalid value falls back to the plain dark buttons on a
    white tray. The tray color may be empty when the owner hides it.
    """
    if not isinstance(raw, dict):
        raw = {}
    shape = raw.get("shape")
    if shape not in _STICKY_SHAPES:
        shape = "pill"

    def color(key, default):
        value = raw.get(key)
        if isinstance(value, str) and _HEX_COLOR.fullmatch(value.strip()):
            return value.strip().lower()
        return default

    if "tray_color" not in raw:
        tray_color = "#ffffff"
    else:
        tray = raw.get("tray_color")
        if tray in (None, "", "transparent"):
            tray_color = None
        elif isinstance(tray, str) and _HEX_COLOR.fullmatch(tray.strip()):
            tray_color = tray.strip().lower()
        else:
            tray_color = "#ffffff"
    return {
        "shape": shape,
        "button_color": color("button_color", "#1c1917"),
        "text_color": color("text_color", "#ffffff"),
        "tray_color": tray_color,
    }


def scrub_sticky_nav(page_content):
    """Drop bottom buttons that point at a section which no longer exists."""
    sticky = page_content.get("sticky_nav")
    if not isinstance(sticky, dict):
        return
    sections = page_content.get("sections") or {}
    ids = set()
    if isinstance(sections, dict):
        for section in sections.values():
            if isinstance(section, dict) and section.get("id") is not None:
                ids.add(int(section["id"]))
    buttons = []
    for button in sticky.get("buttons") or []:
        if not isinstance(button, dict):
            continue
        if button.get("kind") == "section":
            try:
                section_id = int(button.get("section_id"))
            except (TypeError, ValueError):
                continue
            if section_id not in ids:
                continue
        buttons.append(button)
    hide = sticky.get("hide_from_section_id")
    if hide is not None:
        try:
            hide = int(hide)
        except (TypeError, ValueError):
            hide = None
        if hide not in ids:
            hide = None
    ordered = sorted(
        (
            section
            for section in (sections.values() if isinstance(sections, dict) else [])
            if isinstance(section, dict)
        ),
        key=lambda section: (section.get("order") or 0, section.get("id") or 0),
    )
    flow = [section for section in ordered if section.get("type") != "header"]
    if hide is not None and flow and hide == int(flow[0]["id"]):
        hide = None
    sticky["buttons"] = buttons
    sticky["hide_from_section_id"] = hide
