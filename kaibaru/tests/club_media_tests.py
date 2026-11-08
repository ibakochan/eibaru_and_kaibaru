from django.test import SimpleTestCase

from kaibaru.club_media import scrub_stored_file, sync_video_url


class ClubMediaPageTests(SimpleTestCase):
    def test_scrub_clears_matching_urls_and_storage_names(self):
        page = {
            "sections": {
                "1": {
                    "type": "slideshow",
                    "slides": [{"image": "https://storage.googleapis.com/kaibaru_public/gy/slate/a.jpg"}],
                    "style": {"backgroundImage": "https://cdn.example/gy/slate/a.jpg"},
                }
            }
        }
        changed = scrub_stored_file(
            page,
            "https://storage.googleapis.com/kaibaru_public/gy/slate/a.jpg",
            "gy/slate/a.jpg",
        )
        self.assertTrue(changed)
        slide = page["sections"]["1"]["slides"][0]
        self.assertIsNone(slide["image"])
        self.assertIsNone(page["sections"]["1"]["style"]["backgroundImage"])

    def test_sync_video_url_updates_only_video_sections(self):
        page = {
            "sections": {
                "1": {"type": "slideshow", "video_url": "old"},
                "2": {"type": "video", "video_url": "old"},
            }
        }
        self.assertTrue(sync_video_url(page, "https://cdn.example/video.mp4"))
        self.assertEqual(page["sections"]["1"]["video_url"], "old")
        self.assertEqual(page["sections"]["2"]["video_url"], "https://cdn.example/video.mp4")
        self.assertFalse(sync_video_url(page, "https://cdn.example/video.mp4"))
        self.assertTrue(sync_video_url(page, None))
        self.assertIsNone(page["sections"]["2"]["video_url"])
