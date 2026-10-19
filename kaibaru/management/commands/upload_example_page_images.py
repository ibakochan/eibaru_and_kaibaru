"""Upload shared layout photos into the existing sitepicsforkaibaru bucket.

The photos are shared by every club that starts from a layout template.
They are not SlateImage rows, and this storage never deletes an object.
The bucket already serves the public 例写真 file, so this command only
uploads objects. It does not create the bucket or change who can read it.

    python manage.py upload_example_page_images /path/to/images
    python manage.py upload_example_page_images /path/to/images --apply-layouts

Files keep their path relative to the directory. A file at
``layouts/yoga/slide-class.jpg`` becomes
``https://storage.googleapis.com/sitepicsforkaibaru/layouts/yoga/slide-class.jpg``.
``--apply-layouts`` writes those URLs into ``kaibaru/layout/*.json``, in the
order listed in ``kaibaru.example_page_images.LAYOUT_IMAGES``.
"""
import json
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from google.cloud import storage

from kaibaru.example_page_images import BUCKET_NAME, LAYOUT_IMAGES, public_url
from kaibaru.layout_templates import LAYOUT_DIR, load_page_content, template_path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def use_application_credentials():
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    path = getattr(settings, "GOOGLE_APPLICATION_CREDENTIALS", "") or ""
    if path and os.path.isfile(path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path


def iter_image_files(directory):
    root = Path(directory)
    if not root.is_dir():
        raise CommandError(f"Not a directory: {directory}")
    files = [
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    if not files:
        raise CommandError(f"No images found in {directory}")
    return root, sorted(files)


def content_type_for(path):
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }[path.suffix.lower()]


def get_bucket(client):
    bucket = client.lookup_bucket(BUCKET_NAME)
    if bucket is None:
        raise CommandError(f"Bucket {BUCKET_NAME} was not found.")
    return bucket


def image_urls_in_order(page):
    urls = []
    sections = sorted(page["sections"].values(), key=lambda section: section["order"])
    for section in sections:
        if section.get("type") == "slideshow":
            for slide in section.get("slides") or []:
                if slide.get("image"):
                    urls.append(slide)
        layout = section.get("layout") or {}
        for box in sorted(layout.get("boxes") or [], key=lambda item: item["order"]):
            for node in box.get("content") or []:
                if isinstance(node, dict) and node.get("type") == "image" and node.get("url"):
                    urls.append(node)
    return urls


def apply_layouts():
    updated = []
    for layout, names in LAYOUT_IMAGES.items():
        page = load_page_content(layout)
        nodes = image_urls_in_order(page)
        if len(nodes) != len(names):
            raise CommandError(
                f"{layout} has {len(nodes)} images, manifest has {len(names)}"
            )
        for node, name in zip(nodes, names):
            if node.get("image"):
                node["image"] = public_url(name)
            else:
                node["url"] = public_url(name)
        path = template_path(layout)
        path.write_text(
            json.dumps(page, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        updated.append(path.name)
    return updated


class Command(BaseCommand):
    help = "Upload layout photos into the sitepicsforkaibaru bucket."

    def add_arguments(self, parser):
        parser.add_argument(
            "directory",
            nargs="?",
            help="Folder of images. Relative paths become object names.",
        )
        parser.add_argument(
            "--apply-layouts",
            action="store_true",
            help="Write the uploaded URLs into kaibaru/layout/*.json.",
        )

    def handle(self, *args, **options):
        use_application_credentials()
        directory = options["directory"]
        if not directory and not options["apply_layouts"]:
            raise CommandError("Pass an image directory, --apply-layouts, or both.")

        if directory:
            client = storage.Client()
            bucket = get_bucket(client)
            self.stdout.write(f"Uploading into {BUCKET_NAME}.")
            root, files = iter_image_files(directory)
            for path in files:
                object_name = path.relative_to(root).as_posix()
                blob = bucket.blob(object_name)
                blob.cache_control = "public, max-age=86400"
                blob.upload_from_filename(str(path), content_type=content_type_for(path))
                self.stdout.write(public_url(object_name))
            self.stdout.write(self.style.SUCCESS(f"Uploaded {len(files)} images."))

        if options["apply_layouts"]:
            names = apply_layouts()
            self.stdout.write(self.style.SUCCESS(
                f"Updated {len(names)} layout files in {LAYOUT_DIR}."
            ))
