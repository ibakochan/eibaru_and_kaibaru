"""Club pictures and the single header video stored in cloud storage."""

SLATE_IMAGE_LIMIT = 100
VIDEO_MAX_BYTES = 20 * 1024 * 1024
VIDEO_CONTENT_TYPES = {"video/mp4", "video/webm"}

IMAGE_LIMIT_MESSAGE = "このサイトの画像は100枚までです。"
VIDEO_SECTION_MESSAGE = "動画セクションは1つまでです。"
VIDEO_SIZE_MESSAGE = "動画は20MBまでです。"
VIDEO_TYPE_MESSAGE = "動画は mp4 か webm にしてください。"


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
