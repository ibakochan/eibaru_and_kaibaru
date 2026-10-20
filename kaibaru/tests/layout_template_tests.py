"""Tests for the ready-made site templates in kaibaru/layout/*.json."""
import json
from datetime import time

from django.db.models.signals import post_save
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from kaibaru.layout_templates import (
    LAYOUT_DIR,
    TEMPLATE_KEYS,
    load_page_content,
    merge_carried_sections,
    page_content_for_club,
    template_path,
)
from kaibaru.models import Club, Lesson
from kaibaru.signals import club_created_signal

EXAMPLE_PIC_PREFIX = "https://storage.googleapis.com/sitepicsforkaibaru/layouts/"
CANVAS_WIDTH = 1205
ALLOWED_TYPES = {
    "custom", "schedule", "join", "member", "teacher",
    "slideshow", "header", "memberplans",
}
ICONS = {
    "home", "schedule", "access", "contact", "phone", "time", "join",
    "members", "memberCard", "teacher", "payment", "cash", "login",
    "featured", "check", "shield", "info", "award", "heart", "thumbsUp",
    "pointRight", "smile",
}


def walk(nodes):
    for node in nodes:
        yield node
        children = node.get("children") if isinstance(node, dict) else None
        if children:
            yield from walk(children)


class LayoutTemplateFileTests(SimpleTestCase):
    def test_one_file_per_layout(self):
        expected = {template_path(key).name for key in TEMPLATE_KEYS}
        actual = {path.name for path in LAYOUT_DIR.glob("*.json")}
        self.assertEqual(expected, actual)

    def test_structure_of_every_template(self):
        for key in TEMPLATE_KEYS:
            with self.subTest(layout=key):
                self.check_template(key)

    def check_template(self, key):
        page = load_page_content(key)
        sections = page["sections"]
        self.assertEqual(page["version"], 1)
        ids = sorted(int(i) for i in sections)
        self.assertEqual(page["next_section_id"], max(ids) + 1)

        ordered = sorted(sections.values(), key=lambda s: s["order"])
        self.assertEqual([s["order"] for s in ordered], list(range(1, len(ordered) + 1)))
        self.assertEqual(ordered[0]["type"], "header")

        slugs = [s["slug"] for s in ordered]
        self.assertEqual(len(slugs), len(set(slugs)))

        types = [s["type"] for s in ordered]
        for unique in ("header", "join", "member", "teacher", "memberplans", "slideshow", "schedule"):
            self.assertEqual(types.count(unique), 1, unique)
        self.assertTrue(set(types) <= ALLOWED_TYPES)

        join = next(s for s in ordered if s["type"] == "join")
        self.assertEqual(join["slug"], "join")
        self.assertLess(join["order"], next(s for s in ordered if s["type"] == "member")["order"])

        for sid, section in sections.items():
            self.assertEqual(section["id"], int(sid))
            self.assertTrue(section["title"])
            if "icon" in section:
                self.assertIn(section["icon"], ICONS)

        header = next(s for s in ordered if s["type"] == "header")["header"]
        self.assertTrue(header["title"])

        slideshow = next(s for s in ordered if s["type"] == "slideshow")
        self.assertGreaterEqual(len(slideshow["slides"]), 3)
        slide_ids = [slide["id"] for slide in slideshow["slides"]]
        self.assertEqual(len(slide_ids), len(set(slide_ids)))
        for slide in slideshow["slides"]:
            self.assertTrue(slide["image"].startswith(EXAMPLE_PIC_PREFIX))
            self.assertIn(str(slide["buttonSectionId"]), sections)

        customs = [s for s in ordered if s["type"] == "custom"]
        self.assertGreaterEqual(len(customs), 5)
        for section in customs:
            self.check_canvas(sections, section)

    def check_canvas(self, sections, section):
        layout = section["layout"]
        self.assertEqual(layout["version"], 2)
        boxes = layout["boxes"]
        self.assertTrue(boxes)
        self.assertEqual([b["order"] for b in boxes], list(range(1, len(boxes) + 1)))
        self.assertEqual(len({b["id"] for b in boxes}), len(boxes))

        images = captions = 0
        for box in boxes:
            self.assertGreaterEqual(box["x"], 0)
            self.assertLessEqual(box["x"] + box["width"], CANVAS_WIDTH)
            self.assertGreater(box["height"], 0)
            self.assertTrue(box["content"])
            for node in walk(box["content"]):
                if node.get("type") == "image":
                    images += 1
                    self.assertTrue(node["url"].startswith(EXAMPLE_PIC_PREFIX))
                elif node.get("type") == "button":
                    self.assertIn(str(node["sectionId"]), sections)
                for child in node.get("children", []):
                    if "text" in child and "この写真を変更" in child["text"]:
                        captions += 1
        self.assertEqual(images, captions, "every picture needs a caption")

        # boxes are absolutely positioned on desktop: they must not overlap
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                overlap = (
                    a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
                    and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"]
                )
                self.assertFalse(overlap, f"{section['title']}: {a['id']} overlaps {b['id']}")

    def test_load_returns_independent_copy(self):
        first = load_page_content("default")
        first["sections"].clear()
        self.assertTrue(load_page_content("default")["sections"])

    def test_club_format_is_json_string(self):
        value = page_content_for_club("yoga")
        self.assertIsInstance(value, str)
        self.assertEqual(json.loads(value), load_page_content("yoga"))

    def test_unknown_layout_rejected(self):
        with self.assertRaises(ValueError):
            load_page_content("nope")


class MergeCarriedSectionsTests(SimpleTestCase):
    def test_event_and_past_sections_keep_their_ids(self):
        template = {
            "version": 1,
            "next_section_id": 3,
            "sections": {
                "1": {"id": 1, "order": 1, "type": "header", "slug": "header", "title": "ヘッダー"},
            },
        }
        old = {
            "sections": {
                "9": {
                    "id": 9,
                    "order": 4,
                    "type": "event",
                    "slug": "大会",
                    "title": "春の大会",
                },
                "10": {
                    "id": 10,
                    "order": 5,
                    "type": "past",
                    "slug": "過去の試合",
                    "title": "過去の試合",
                    "bucket": "competition",
                },
            }
        }
        page, remap = merge_carried_sections(template, json.dumps(old))
        self.assertEqual(remap, {})
        self.assertEqual(page["sections"]["1"]["type"], "header")
        self.assertEqual(page["sections"]["9"]["title"], "春の大会")
        self.assertEqual(page["sections"]["9"]["id"], 9)
        self.assertEqual(page["sections"]["10"]["bucket"], "competition")
        self.assertGreater(page["sections"]["9"]["order"], page["sections"]["1"]["order"])
        self.assertEqual(page["next_section_id"], 11)

    def test_clashing_event_id_is_moved(self):
        template = {
            "version": 1,
            "next_section_id": 15,
            "sections": {
                "14": {"id": 14, "order": 14, "type": "member", "slug": "member", "title": "会員"},
            },
        }
        old = {
            "sections": {
                "14": {"id": 14, "order": 8, "type": "event", "slug": "大会", "title": "大会"},
            }
        }
        page, remap = merge_carried_sections(template, old)
        self.assertEqual(remap, {14: 15})
        self.assertEqual(page["sections"]["14"]["type"], "member")
        self.assertEqual(page["sections"]["15"]["type"], "event")
        self.assertEqual(page["sections"]["15"]["id"], 15)
        self.assertEqual(page["next_section_id"], 16)


class CreateTrialTemplateTests(TestCase):
    URL = "/api/clubs/create-trial/"

    def setUp(self):
        post_save.disconnect(club_created_signal, sender=Club)
        self.addCleanup(lambda: post_save.connect(club_created_signal, sender=Club))
        self.owner = CustomUser.objects.create_user(
            username="tplowner", email="tpl@example.com", password="pass123"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_template_fills_layout_and_page_content(self):
        response = self.client.post(
            self.URL, {"subdomain": "tplyoga", "template": "yoga"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        club = Club.objects.get(subdomain="tplyoga")
        self.assertEqual(club.layout, "yoga")
        self.assertEqual(json.loads(club.page_content), load_page_content("yoga"))

    def test_slash_layout_key(self):
        response = self.client.post(
            self.URL, {"subdomain": "tplbjj", "template": "bjj/grappling"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Club.objects.get(subdomain="tplbjj").layout, "bjj/grappling")

    def test_without_template_keeps_old_behaviour(self):
        response = self.client.post(self.URL, {"subdomain": "tplblank"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        club = Club.objects.get(subdomain="tplblank")
        self.assertEqual(club.layout, "default")
        self.assertIsNone(club.page_content)

    def test_apply_template_replaces_page_and_keeps_lessons(self):
        created = self.client.post(
            self.URL, {"subdomain": "tplswap", "template": "yoga"}, format="json"
        )
        self.assertEqual(created.status_code, 201, created.content)
        club = Club.objects.get(subdomain="tplswap")
        lesson = Lesson.objects.create(
            club=club,
            section_id=6,
            title="朝のクラス",
            weekday=0,
            start_time=time(9, 0),
            end_time=time(10, 0),
        )
        response = self.client.post(
            f"/api/clubs/{club.id}/apply-template/",
            {"template": "swimming", "default_background_color": "#f2fbfc"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        club.refresh_from_db()
        lesson.refresh_from_db()
        self.assertEqual(club.layout, "swimming")
        self.assertEqual(club.default_background_color, "#f2fbfc")
        self.assertEqual(json.loads(club.page_content), load_page_content("swimming"))
        self.assertEqual(lesson.section_id, 7)
        self.assertEqual(lesson.title, "朝のクラス")

    def test_apply_template_rejects_unknown_and_strangers(self):
        created = self.client.post(
            self.URL, {"subdomain": "tplkeep", "template": "yoga"}, format="json"
        )
        club = Club.objects.get(subdomain="tplkeep")
        bad = self.client.post(
            f"/api/clubs/{club.id}/apply-template/",
            {"template": "nope"},
            format="json",
        )
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(Club.objects.get(pk=club.pk).layout, "yoga")

        stranger = CustomUser.objects.create_user(
            username="tplother", email="other@example.com", password="pass123"
        )
        self.client.force_authenticate(stranger)
        denied = self.client.post(
            f"/api/clubs/{club.id}/apply-template/",
            {"template": "dance"},
            format="json",
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(Club.objects.get(pk=club.pk).layout, "yoga")

    def test_unknown_template_rejected(self):
        response = self.client.post(
            self.URL, {"subdomain": "tplbad", "template": "../etc"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Club.objects.filter(subdomain="tplbad").exists())
