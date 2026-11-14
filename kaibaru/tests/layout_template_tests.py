"""Tests for the ready-made site templates in kaibaru/layout/*.json."""
import json
from datetime import time
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import CustomUser
from kaibaru.layout_templates import (
    LAYOUT_DIR,
    TEMPLATE_KEYS,
    apply_hero,
    clean_sticky_nav,
    load_page_content,
    merge_carried_sections,
    page_content_for_club,
    template_path,
)
from kaibaru.models import Club, Event, Lesson
from kaibaru.signals import club_created_signal

EXAMPLE_PIC_PREFIX = "https://storage.googleapis.com/sitepicsforkaibaru/layouts/"
RESERVATION_TEMPLATE_KEYS = {"hair_salon", "nail_salon"}
MEMBER_ONLY_TYPES = ("join", "member", "teacher", "memberplans")
CANVAS_WIDTH = 1205
ALLOWED_TYPES = {
    "custom", "schedule", "service_schedule", "join", "member", "teacher",
    "slideshow", "header", "memberplans", "inquiry", "video",
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
        if key in RESERVATION_TEMPLATE_KEYS:
            required = ["header", "slideshow", "service_schedule", "inquiry", "video"]
            for banned in MEMBER_ONLY_TYPES:
                self.assertNotIn(banned, types, banned)
            self.assertNotIn("schedule", types)
        else:
            required = ["header", "slideshow", "schedule", "inquiry", "video"]
            required.extend(MEMBER_ONLY_TYPES)
        for unique in required:
            self.assertEqual(types.count(unique), 1, unique)
        self.assertTrue(set(types) <= ALLOWED_TYPES)

        if key not in RESERVATION_TEMPLATE_KEYS:
            join = next(s for s in ordered if s["type"] == "join")
            self.assertEqual(join["slug"], "join")
            member = next(s for s in ordered if s["type"] == "member")
            self.assertEqual(member["profile_title"], "プロフィール")
            self.assertLess(join["order"], member["order"])
        else:
            staff = next(s for s in ordered if s["title"] == "スタッフ紹介")
            self.assertEqual(staff["type"], "custom")

        for sid, section in sections.items():
            self.assertEqual(section["id"], int(sid))
            self.assertTrue(section["title"])
            if "icon" in section:
                self.assertIn(section["icon"], ICONS)

        header = next(s for s in ordered if s["type"] == "header")["header"]
        self.assertTrue(header["title"])

        self.assertEqual(ordered[1]["type"], "slideshow")
        video = ordered[2]
        self.assertEqual(video["type"], "video")
        self.assertEqual(video["title"], "動画")
        self.assertTrue(video["show_in_menu"])
        self.assertTrue(video["video_url"].startswith(EXAMPLE_PIC_PREFIX))
        self.assertTrue(video["video_url"].endswith("/hero.mp4"))

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

        self.check_inquiry(ordered, page)

    def check_inquiry(self, ordered, page):
        inquiry = next(section for section in ordered if section["type"] == "inquiry")
        self.assertEqual(inquiry["title"], "問い合わせフォーム")
        self.assertFalse(inquiry["show_in_menu"])
        after_hero = [
            section for section in ordered
            if section["type"] not in ("header", "slideshow", "video")
        ]
        sticky = page["sticky_nav"]
        self.assertEqual(sticky["hide_from_section_id"], after_hero[0]["id"])
        self.assertNotEqual(sticky["hide_from_section_id"], ordered[1]["id"])
        labels = [button["label"] for button in sticky["buttons"]]
        self.assertEqual(labels[0], "問い合わせフォーム")
        self.assertEqual(labels[1], "アクセス")
        self.assertIn(labels[2], {"予約はレッスンから", "予約する"})
        by_label = {button["label"]: button for button in sticky["buttons"]}
        access = next(section for section in ordered if section["title"] == "アクセス")
        schedule = next(
            section
            for section in ordered
            if section["type"] in ("schedule", "service_schedule")
        )
        reserve = by_label[labels[2]]
        self.assertEqual(by_label["問い合わせフォーム"]["section_id"], inquiry["id"])
        self.assertEqual(by_label["アクセス"]["section_id"], access["id"])
        self.assertEqual(reserve["section_id"], schedule["id"])
        self.assertEqual(by_label["問い合わせフォーム"]["icon"], "contact")
        self.assertEqual(by_label["アクセス"]["icon"], "access")
        self.assertEqual(reserve["icon"], "schedule")
        self.assertEqual(by_label["問い合わせフォーム"]["icon_color"], "#2563eb")
        self.assertEqual(by_label["アクセス"]["icon_color"], "#dc2626")
        self.assertEqual(reserve["icon_color"], "#7c3aed")
        style = sticky["style"]
        self.assertIn(style["shape"], {"pill", "rounded", "square"})
        self.assertRegex(style["button_color"], r"^#[0-9a-f]{6}$")
        self.assertRegex(style["text_color"], r"^#[0-9a-f]{6}$")
        self.assertRegex(style["tray_color"], r"^#[0-9a-f]{6}$")
        found = False
        for box in access["layout"]["boxes"]:
            for node in walk(box["content"]):
                if node.get("type") == "button" and node.get("label") == "問い合わせフォーム":
                    self.assertEqual(node["sectionId"], inquiry["id"])
                    found = True
        self.assertTrue(found, "access section needs an inquiry button")

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

    def test_hero_both_is_the_template_unchanged(self):
        for key in TEMPLATE_KEYS:
            with self.subTest(layout=key):
                page = load_page_content(key)
                self.assertEqual(apply_hero(page, "both"), page)
                self.assertEqual(
                    json.loads(page_content_for_club(key)), page
                )

    def test_hero_slideshow_drops_the_video(self):
        for key in TEMPLATE_KEYS:
            with self.subTest(layout=key):
                template = load_page_content(key)
                page = apply_hero(template, "slideshow")
                ordered = sorted(page["sections"].values(), key=lambda s: s["order"])
                types = [s["type"] for s in ordered]
                self.assertEqual(types[:2], ["header", "slideshow"])
                self.assertNotIn("video", types)
                self.assertEqual(
                    [s["order"] for s in ordered], list(range(1, len(ordered) + 1))
                )
                self.assertEqual(ordered[1]["slug"], "home")
                self.assertEqual(len(page["sections"]), len(template["sections"]) - 1)
                self.assertEqual(page["next_section_id"], template["next_section_id"])

    def test_hero_video_drops_the_slideshow_and_takes_over_home(self):
        for key in TEMPLATE_KEYS:
            with self.subTest(layout=key):
                template = load_page_content(key)
                page = apply_hero(template, "video")
                ordered = sorted(page["sections"].values(), key=lambda s: s["order"])
                types = [s["type"] for s in ordered]
                self.assertEqual(types[:2], ["header", "video"])
                self.assertNotIn("slideshow", types)
                self.assertEqual(
                    [s["order"] for s in ordered], list(range(1, len(ordered) + 1))
                )
                video = ordered[1]
                self.assertEqual(video["title"], "ホーム")
                self.assertEqual(video["slug"], "home")
                self.assertEqual(video["icon"], "home")
                self.assertTrue(video["video_url"].endswith("/hero.mp4"))
                slugs = [s["slug"] for s in ordered]
                self.assertEqual(len(slugs), len(set(slugs)))

    def test_hero_keeps_sticky_nav_valid(self):
        for key in TEMPLATE_KEYS:
            for hero in ("slideshow", "video"):
                with self.subTest(layout=key, hero=hero):
                    page = apply_hero(load_page_content(key), hero)
                    ordered = sorted(page["sections"].values(), key=lambda s: s["order"])
                    flow = [s for s in ordered if s["type"] != "header"]
                    sticky = page["sticky_nav"]
                    self.assertIsNotNone(sticky["hide_from_section_id"])
                    self.assertNotEqual(sticky["hide_from_section_id"], flow[0]["id"])
                    clean_sticky_nav(page, sticky)

    def test_hero_does_not_change_the_loaded_template(self):
        before = load_page_content("yoga")
        apply_hero(before, "video")
        self.assertEqual(before, load_page_content("yoga"))

    def test_unknown_hero_rejected(self):
        with self.assertRaises(ValueError):
            apply_hero(load_page_content("yoga"), "nope")

    def test_sticky_styles_differ_by_template(self):
        colors = set()
        shapes = set()
        for key in TEMPLATE_KEYS:
            style = load_page_content(key)["sticky_nav"]["style"]
            colors.add(style["button_color"])
            shapes.add(style["shape"])
        self.assertGreaterEqual(len(colors), 8)
        self.assertEqual(shapes, {"pill", "rounded", "square"})

    def test_clean_sticky_nav_keeps_style(self):
        page = load_page_content("yoga")
        cleaned = clean_sticky_nav(page, {
            "hide_from_section_id": page["sticky_nav"]["hide_from_section_id"],
            "buttons": page["sticky_nav"]["buttons"],
            "style": {
                "shape": "square",
                "button_color": "#ABCDEF",
                "text_color": "nope",
                "tray_color": "transparent",
            },
        })
        self.assertEqual(cleaned["style"]["shape"], "square")
        self.assertEqual(cleaned["style"]["button_color"], "#abcdef")
        self.assertEqual(cleaned["style"]["text_color"], "#ffffff")
        self.assertIsNone(cleaned["style"]["tray_color"])
        self.assertEqual(
            [button["icon"] for button in cleaned["buttons"]],
            ["contact", "access", "schedule"],
        )
        self.assertEqual(
            [button["icon_color"] for button in cleaned["buttons"]],
            ["#2563eb", "#dc2626", "#7c3aed"],
        )
        broken = dict(page["sticky_nav"]["buttons"][0])
        broken["icon"] = "not-an-icon"
        with self.assertRaises(ValueError):
            clean_sticky_nav(page, {
                "hide_from_section_id": page["sticky_nav"]["hide_from_section_id"],
                "buttons": [broken],
            })

    def test_sticky_nav_rejects_the_opening_section(self):
        page = load_page_content("yoga")
        ordered = sorted(page["sections"].values(), key=lambda section: section["order"])
        opening = next(section for section in ordered if section["type"] != "header")
        with self.assertRaises(ValueError):
            clean_sticky_nav(page, {
                "hide_from_section_id": opening["id"],
                "buttons": [],
            })


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
            self.URL,
            {"subdomain": "tplyoga", "template": "yoga", "terms_accepted": True},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        club = Club.objects.get(subdomain="tplyoga")
        self.assertEqual(club.layout, "yoga")
        self.assertEqual(json.loads(club.page_content), load_page_content("yoga"))

    def test_slash_layout_key(self):
        response = self.client.post(
            self.URL,
            {"subdomain": "tplbjj", "template": "bjj/grappling", "terms_accepted": True},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Club.objects.get(subdomain="tplbjj").layout, "bjj/grappling")

    def test_without_template_keeps_old_behaviour(self):
        response = self.client.post(
            self.URL,
            {"subdomain": "tplblank", "terms_accepted": True},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        club = Club.objects.get(subdomain="tplblank")
        self.assertEqual(club.layout, "default")
        self.assertIsNone(club.page_content)

    def test_apply_template_replaces_page_and_keeps_lessons(self):
        created = self.client.post(
            self.URL,
            {"subdomain": "tplswap", "template": "yoga", "terms_accepted": True},
            format="json",
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

    def test_create_with_hero_choice(self):
        for hero, expected in (
            ("slideshow", ["header", "slideshow"]),
            ("video", ["header", "video"]),
            ("both", ["header", "slideshow", "video"]),
        ):
            with self.subTest(hero=hero):
                response = self.client.post(
                    self.URL,
                    {
                        "subdomain": f"tplhero{hero}",
                        "template": "yoga",
                        "hero": hero,
                        "terms_accepted": True,
                    },
                    format="json",
                )
                self.assertEqual(response.status_code, 201, response.content)
                club = Club.objects.get(subdomain=f"tplhero{hero}")
                ordered = sorted(
                    json.loads(club.page_content)["sections"].values(),
                    key=lambda s: s["order"],
                )
                self.assertEqual([s["type"] for s in ordered][: len(expected)], expected)
                Club.objects.filter(pk=club.pk).delete()

    def test_create_rejects_unknown_hero(self):
        response = self.client.post(
            self.URL,
            {
                "subdomain": "tplherobad",
                "template": "yoga",
                "hero": "nope",
                "terms_accepted": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Club.objects.filter(subdomain="tplherobad").exists())

    def test_apply_template_with_hero_choice(self):
        created = self.client.post(
            self.URL,
            {"subdomain": "tplheroswap", "template": "yoga", "terms_accepted": True},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.content)
        club = Club.objects.get(subdomain="tplheroswap")
        response = self.client.post(
            f"/api/clubs/{club.id}/apply-template/",
            {"template": "swimming", "hero": "video"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        club.refresh_from_db()
        types = [s["type"] for s in json.loads(club.page_content)["sections"].values()]
        self.assertIn("video", types)
        self.assertNotIn("slideshow", types)

        bad = self.client.post(
            f"/api/clubs/{club.id}/apply-template/",
            {"template": "swimming", "hero": "nope"},
            format="json",
        )
        self.assertEqual(bad.status_code, 400)

    def test_apply_template_moves_the_live_event_off_the_destination_first(self):
        created = self.client.post(
            self.URL,
            {"subdomain": "tplchain", "template": "yoga", "terms_accepted": True},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.content)
        club = Club.objects.get(subdomain="tplchain")
        club.page_content = json.dumps({
            "version": 1,
            "next_section_id": 29,
            "sections": {
                "26": {"id": 26, "order": 1, "type": "event", "slug": "net", "title": "予約"},
                "15": {"id": 15, "order": 2, "type": "event", "slug": "costco", "title": "costco"},
                "27": {"id": 27, "order": 3, "type": "event", "slug": "bjj", "title": "BJJ"},
                "28": {"id": 28, "order": 4, "type": "event", "slug": "one", "title": "One"},
            },
        })
        club.save(update_fields=["page_content"])
        starts = timezone.now()
        held = Event.objects.create(
            club=club, section_id=28, live_section_id=28, title="One match", starts_at=starts,
        )
        moving = Event.objects.create(
            club=club, section_id=27, live_section_id=27, title="BJJ tournament", starts_at=starts,
        )

        response = self.client.post(
            f"/api/clubs/{club.id}/apply-template/",
            {"template": "swimming"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        held.refresh_from_db()
        moving.refresh_from_db()
        self.assertEqual(held.section_id, 29)
        self.assertEqual(held.live_section_id, 29)
        self.assertEqual(moving.section_id, 28)
        self.assertEqual(moving.live_section_id, 28)

    def test_apply_template_rejects_unknown_and_strangers(self):
        created = self.client.post(
            self.URL,
            {"subdomain": "tplkeep", "template": "yoga", "terms_accepted": True},
            format="json",
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

    def test_terms_are_required(self):
        response = self.client.post(
            self.URL, {"subdomain": "tplterms", "template": "yoga"}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Club.objects.filter(subdomain="tplterms").exists())

    def test_inquiry_emails_the_owner(self):
        created = self.client.post(
            self.URL,
            {"subdomain": "tplinquiry", "template": "yoga", "terms_accepted": True},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.content)
        club = Club.objects.get(subdomain="tplinquiry")
        visitor = APIClient()
        with patch("kaibaru.tasks_emails.send_inquiry_email.delay") as send:
            response = visitor.post(
                f"/api/clubs/{club.id}/inquiry/",
                {
                    "name": "山田 太郎",
                    "furigana": "やまだ たろう",
                    "phone_number": "090-0000-0000",
                    "email": "guest@example.com",
                    "header": "体験について",
                    "message": "初めてです。",
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        send.assert_called_once_with(
            club.id,
            "山田 太郎",
            "やまだ たろう",
            "090-0000-0000",
            "guest@example.com",
            "体験について",
            "初めてです。",
        )

    def test_unknown_template_rejected(self):
        response = self.client.post(
            self.URL,
            {
                "subdomain": "tplbad",
                "template": "../etc",
                "terms_accepted": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Club.objects.filter(subdomain="tplbad").exists())
