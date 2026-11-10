from django.test import SimpleTestCase

from types import SimpleNamespace

from kaibaru.club_media import IMAGE_MAX_BYTES, scrub_stored_file, sync_video_url, uploaded_image_too_large


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

    def test_pictures_over_five_megabytes_are_rejected(self):
        big = SimpleNamespace(size=IMAGE_MAX_BYTES + 1, content_type="image/jpeg", name="photo.jpg")
        exact = SimpleNamespace(size=IMAGE_MAX_BYTES, content_type="image/png", name="photo.png")
        video = SimpleNamespace(size=IMAGE_MAX_BYTES + 1, content_type="video/mp4", name="clip.mp4")
        unnamed = SimpleNamespace(size=IMAGE_MAX_BYTES + 1, content_type="", name="scan.HEIC")
        self.assertTrue(uploaded_image_too_large(big))
        self.assertFalse(uploaded_image_too_large(exact))
        self.assertFalse(uploaded_image_too_large(video))
        self.assertTrue(uploaded_image_too_large(unnamed))
