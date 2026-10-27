import json
from collections import defaultdict

from .models import JoinRequestLevel, LevelSystem, MemberLevel


def as_dict(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return {}
    if not isinstance(value, dict):
        return {}
    return value


def as_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    return value


def clean_level_names(value):
    cleaned = {}
    for key, name in as_dict(value).items():
        try:
            level = int(key)
        except (TypeError, ValueError):
            continue
        text = str(name or "").strip()
        if level < 1 or not text:
            continue
        cleaned[str(level)] = text[:15]
    return cleaned or None


def clean_milestones(value):
    cleaned = {}
    for key, count in as_dict(value).items():
        try:
            level = int(key)
            amount = int(count)
        except (TypeError, ValueError):
            continue
        if level < 1 or amount < 1:
            continue
        cleaned[str(level)] = min(amount, 99999)
    return cleaned or None


def clean_counts(value):
    cleaned = {}
    for key, count in as_dict(value).items():
        try:
            level = int(key)
            amount = int(count)
        except (TypeError, ValueError):
            continue
        if level < 1 or amount < 0:
            continue
        cleaned[str(level)] = amount
    return cleaned


def rank_name(system, level):
    names = as_dict(system.level_names)
    raw = names.get(str(level), "")
    text = str(raw or "").strip()
    return text or f"レベル{level}"


def combined_counts(row):
    totals = defaultdict(int)
    for source in (row.manual_level_counts, row.level_counts):
        for key, count in clean_counts(source).items():
            totals[key] += count
    return dict(totals)


def member_level_data(row):
    system = row.level_system
    return {
        "id": row.id,
        "level_system": row.level_system_id,
        "level_system_name": system.name,
        "level": row.level,
        "level_counts": clean_counts(row.level_counts),
        "manual_level_counts": clean_counts(row.manual_level_counts),
        "level_participation": combined_counts(row),
    }


def member_levels_data(member):
    rows = member.levels.select_related("level_system").all()
    return [member_level_data(row) for row in rows]


def _level_items(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, list):
        return None
    items = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            system_id = int(item.get("level_system"))
            level = int(item.get("level") or 1)
        except (TypeError, ValueError):
            continue
        items.append({
            "level_system": system_id,
            "level": max(1, level),
            "manual_level_counts": clean_counts(item.get("manual_level_counts")),
        })
    return items


def sync_member_levels(member, raw):
    parsed = _level_items(raw)
    if parsed is None:
        return
    keep = []
    for item in parsed:
        system = LevelSystem.objects.filter(
            id=item["level_system"],
            club_id=member.club_id,
        ).first()
        if system is None:
            continue
        row, _created = MemberLevel.objects.update_or_create(
            member=member,
            level_system=system,
            defaults={
                "level": item["level"],
                "manual_level_counts": item["manual_level_counts"],
            },
        )
        keep.append(row.id)
    MemberLevel.objects.filter(member=member).exclude(id__in=keep).delete()


def sync_join_request_levels(join_request, raw):
    parsed = _level_items(raw)
    if parsed is None:
        return
    keep = []
    for item in parsed:
        system = LevelSystem.objects.filter(
            id=item["level_system"],
            club_id=join_request.club_id,
        ).first()
        if system is None:
            continue
        row, _created = JoinRequestLevel.objects.update_or_create(
            join_request=join_request,
            level_system=system,
            defaults={
                "level": item["level"],
                "manual_level_counts": item["manual_level_counts"],
            },
        )
        keep.append(row.id)
    JoinRequestLevel.objects.filter(join_request=join_request).exclude(id__in=keep).delete()


def copy_join_request_levels(join_request, member):
    for row in join_request.levels.select_related("level_system"):
        if row.level_system.club_id != member.club_id:
            continue
        MemberLevel.objects.update_or_create(
            member=member,
            level_system=row.level_system,
            defaults={
                "level": row.level or 1,
                "manual_level_counts": clean_counts(row.manual_level_counts),
            },
        )


def record_level_progress(member, lesson):
    level_ups = []
    for system in lesson.level_systems.all():
        row, _created = MemberLevel.objects.get_or_create(
            member=member,
            level_system=system,
            defaults={
                "level": 1,
                "level_counts": {},
                "manual_level_counts": {},
            },
        )
        counts = clean_counts(row.level_counts)
        key = str(row.level)
        counts[key] = counts.get(key, 0) + 1
        row.level_counts = counts

        milestones = as_dict(system.level_milestones)
        try:
            required = int(milestones.get(key) or 0)
        except (TypeError, ValueError):
            required = 0
        total = combined_counts(row).get(key, 0)
        if required and total >= required:
            row.level += 1
            level_ups.append({
                "member_id": member.id,
                "level_system_id": system.id,
                "level_system_name": system.name,
                "new_level": row.level,
                "level_name": rank_name(system, row.level),
            })
        row.save()
    return level_ups
