"""Club pictures and the single header video stored in cloud storage."""

SLATE_IMAGE_LIMIT = 100
NEWS_KEEP = 20
IMAGE_MAX_BYTES = 5 * 1024 * 1024
VIDEO_MAX_BYTES = 20 * 1024 * 1024
VIDEO_CONTENT_TYPES = {"video/mp4", "video/webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif"}

IMAGE_LIMIT_MESSAGE = "このサイトの画像は100枚までです。"
IMAGE_SIZE_MESSAGE = "画像は5MBまでです。"
VIDEO_SECTION_MESSAGE = "動画セクションは1つまでです。"
VIDEO_SIZE_MESSAGE = "動画は20MBまでです。"
VIDEO_TYPE_MESSAGE = "動画は mp4 か webm にしてください。"


def uploaded_image_too_large(uploaded):
    size = getattr(uploaded, "size", 0) or 0
    if size <= IMAGE_MAX_BYTES:
        return False
    content_type = (getattr(uploaded, "content_type", "") or "").split(";")[0].strip().lower()
    if content_type.startswith("image/"):
        return True
    name = (getattr(uploaded, "name", "") or "").lower()
    return any(name.endswith(ext) for ext in IMAGE_EXTENSIONS)


class ImageSizeLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in {"POST", "PUT", "PATCH"} and request.FILES:
            for uploaded in request.FILES.values():
                if uploaded_image_too_large(uploaded):
                    from django.http import JsonResponse
                    return JsonResponse(
                        {"detail": IMAGE_SIZE_MESSAGE, "code": "image_size"},
                        status=400,
                    )
        return self.get_response(request)


def scrub_stored_file(page_content, url, name):
    """Clear a deleted cloud file everywhere the page still points at it."""
    if not isinstance(page_content, dict):
        return False
    changed = False

    def matches(value):
        if not isinstance(value, str) or not value:
            return False
        if url and value == url:
            return True
        return bool(name and name in value)

    def walk(node):
        nonlocal changed
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if matches(value):
                    node[key] = None
                    changed = True
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(page_content)
    return changed


def sync_video_url(page_content, url):
    """Point every video section at the club's one video file."""
    if not isinstance(page_content, dict):
        return False
    sections = page_content.get("sections") or {}
    if not isinstance(sections, dict):
        return False
    changed = False
    for section in sections.values():
        if isinstance(section, dict) and section.get("type") == "video":
            if section.get("video_url") != url:
                section["video_url"] = url
                changed = True
    return changed
