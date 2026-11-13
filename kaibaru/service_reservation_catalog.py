"""Named reservation prices and the add-ons attached to them.

A club keeps a catalog of base prices and add-ons. A lesson offers every
club price until the owner customizes it. Customizing picks which club
prices to offer, can replace the add-ons on those prices, and can add
prices and add-ons that exist only on that lesson.

Booking stores the chosen names on the reservation. Later edits to the
menu do not change a reservation that was already made.
"""

import json

from .charge_amounts import invalid_set_price
from .models import LessonPriceOffer, ReservationAddon, ReservationPrice

AUDIENCES = ("visitor", "member", "trial", "any")

_UNAVAILABLE = {
    "visitor": "このレッスンはビジター予約を受け付けていません。",
    "member": "このレッスンでは会員予約を受け付けていません。",
    "trial": "このレッスンでは体験予約を受け付けていません。",
}


def parse_catalog(raw):
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("料金の形式が正しくありません。") from exc
    if not isinstance(raw, dict):
        raise ValueError("料金の形式が正しくありません。")
    return raw


def posted_addon_ids(request):
    values = request.POST.getlist("addon_ids")
    if values:
        return values
    raw = (request.POST.get("addon_ids") or "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _yen(value):
    if value in (None, ""):
        return 0
    message = invalid_set_price(value, allow_free=True)
    if message:
        raise ValueError(message)
    return int(value)


def _name(value, label):
    name = str(value or "").strip()
    if not name:
        raise ValueError(f"{label}の名前を入力してください。")
    if len(name) > 80:
        raise ValueError(f"{label}の名前は80文字以内にしてください。")
    return name


def _audience(value):
    audience = str(value or "visitor").strip()
    if audience not in AUDIENCES:
        raise ValueError("料金の対象が正しくありません。")
    return audience


def _rows(payload, key):
    rows = payload.get(key) or []
    if not isinstance(rows, list):
        raise ValueError("料金の形式が正しくありません。")
    return rows


def _sorted_addons(addons):
    return sorted(addons, key=lambda addon: (addon.position, addon.id))


def _addon_payload(addon):
    return {
        "id": addon.id,
        "name": addon.name,
        "amount": addon.amount,
    }


def _load(lesson):
    club_prices = list(
        ReservationPrice.objects.filter(
            club_id=lesson.club_id,
            lesson__isnull=True,
        )
        .order_by("position", "id")
        .prefetch_related("addons")
    )
    lesson_prices = list(
        ReservationPrice.objects.filter(lesson=lesson)
        .order_by("position", "id")
        .prefetch_related("addons")
    )
    offers = list(
        LessonPriceOffer.objects.filter(lesson=lesson).prefetch_related("addons")
    )
    return club_prices, lesson_prices, offers


def _matches(price, audience):
    return price.audience in (audience, "any")


def _menu(lesson, audience, club_prices, lesson_prices, offers):
    club_prices = [price for price in club_prices if _matches(price, audience)]
    if not lesson.reservation_menu_customized:
        return [
            _item(price, _sorted_addons(price.addons.all()), "club")
            for price in club_prices
        ]

    offer_by_price = {offer.price_id: offer for offer in offers}
    items = []
    for price in club_prices:
        offer = offer_by_price.get(price.id)
        if offer is None:
            continue
        if offer.addons_overridden:
            addons = _sorted_addons(offer.addons.all())
        else:
            addons = _sorted_addons(price.addons.all())
        items.append(_item(price, addons, "club"))
    for price in lesson_prices:
        if not _matches(price, audience):
            continue
        items.append(_item(price, _sorted_addons(price.addons.all()), "lesson"))
    return items


def _item(price, addons, source):
    return {
        "id": price.id,
        "name": price.name,
        "amount": price.amount,
        "audience": price.audience,
        "source": source,
        "addons": [_addon_payload(addon) for addon in addons],
    }


def lesson_menu(lesson):
    club_prices, lesson_prices, offers = _load(lesson)
    seen = set()
    items = []
    for audience in ("visitor", "member", "trial"):
        for item in _menu(lesson, audience, club_prices, lesson_prices, offers):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            items.append(item)
    return items


def resolve_reservation_quote(lesson, audience, price_id, addon_ids):
    """Return the chosen base price plus any add-ons."""
    club_prices, lesson_prices, offers = _load(lesson)
    menu = _menu(lesson, audience, club_prices, lesson_prices, offers)
    if not menu:
        raise ValueError(_UNAVAILABLE[audience])

    if price_id in (None, ""):
        if len(menu) == 1:
            chosen = menu[0]
        else:
            raise ValueError("料金を選んでください。")
    else:
        try:
            price_id = int(price_id)
        except (TypeError, ValueError):
            raise ValueError("料金を選んでください。")
        chosen = next((item for item in menu if item["id"] == price_id), None)
        if chosen is None:
            raise ValueError("選択した料金は見つかりません。")

    allowed = {addon["id"]: addon for addon in chosen["addons"]}
    selected = []
    seen = set()
    raw_ids = addon_ids or []
    if isinstance(raw_ids, str):
        raw_ids = [part for part in raw_ids.split(",") if part.strip()]
    for raw in raw_ids:
        try:
            addon_id = int(raw)
        except (TypeError, ValueError):
            raise ValueError("追加メニューの指定が正しくありません。")
        if addon_id in seen:
            continue
        seen.add(addon_id)
        addon = allowed.get(addon_id)
        if addon is None:
            raise ValueError("選択した追加メニューは、この料金では選べません。")
        selected.append(addon)

    amount = chosen["amount"] + sum(addon["amount"] for addon in selected)
    if selected:
        price_name = chosen["name"] + "（" + "、".join(addon["name"] for addon in selected) + "）"
    else:
        price_name = chosen["name"]
    return {"amount": amount, "price_name": price_name[:200]}


def club_catalog_payload(club):
    addons = ReservationAddon.objects.filter(club=club, lesson__isnull=True).order_by(
        "position", "id"
    )
    prices = (
        ReservationPrice.objects.filter(club=club, lesson__isnull=True)
        .order_by("position", "id")
        .prefetch_related("addons")
    )
    return {
        "addons": [_addon_payload(addon) for addon in addons],
        "prices": [
            {
                "id": price.id,
                "name": price.name,
                "amount": price.amount,
                "audience": price.audience,
                "addon_ids": [addon.id for addon in _sorted_addons(price.addons.all())],
            }
            for price in prices
        ],
    }


def lesson_catalog_payload(lesson):
    offers = (
        LessonPriceOffer.objects.filter(lesson=lesson)
        .select_related("price")
        .prefetch_related("addons")
        .order_by("price__position", "price_id")
    )
    addons = lesson.reservation_addons.order_by("position", "id")
    prices = lesson.reservation_prices.order_by("position", "id").prefetch_related("addons")
    return {
        "customized": lesson.reservation_menu_customized,
        "offers": [
            {
                "price_id": offer.price_id,
                "override": offer.addons_overridden,
                "addon_ids": [addon.id for addon in _sorted_addons(offer.addons.all())],
            }
            for offer in offers
            if offer.price.lesson_id is None
        ],
        "addons": [_addon_payload(addon) for addon in addons],
        "prices": [
            {
                "id": price.id,
                "name": price.name,
                "amount": price.amount,
                "audience": price.audience,
                "addon_ids": [addon.id for addon in _sorted_addons(price.addons.all())],
            }
            for price in prices
        ],
    }


def sync_club_catalog(club, payload):
    addon_rows = _rows(payload, "addons")
    price_rows = _rows(payload, "prices")
    existing_addons = {
        addon.id: addon
        for addon in ReservationAddon.objects.filter(club=club, lesson__isnull=True)
    }
    kept_addons = []
    keys = {}
    for index, row in enumerate(addon_rows):
        if not isinstance(row, dict):
            raise ValueError("料金の形式が正しくありません。")
        if not str(row.get("name") or "").strip():
            continue
        addon = _take_existing(existing_addons, row.get("id"))
        if addon is None:
            addon = ReservationAddon(club=club)
        addon.lesson = None
        addon.name = _name(row.get("name"), "追加メニュー")
        addon.amount = _yen(row.get("amount"))
        addon.position = index
        addon.save()
        kept_addons.append(addon.id)
        _remember_key(keys, row, addon.id)
    ReservationAddon.objects.filter(club=club, lesson__isnull=True).exclude(
        id__in=kept_addons
    ).delete()

    existing_prices = {
        price.id: price
        for price in ReservationPrice.objects.filter(club=club, lesson__isnull=True)
    }
    kept_prices = []
    for index, row in enumerate(price_rows):
        if not isinstance(row, dict):
            raise ValueError("料金の形式が正しくありません。")
        if not str(row.get("name") or "").strip():
            continue
        price = _take_existing(existing_prices, row.get("id"))
        if price is None:
            price = ReservationPrice(club=club)
        price.lesson = None
        price.name = _name(row.get("name"), "料金")
        price.amount = _yen(row.get("amount"))
        price.audience = _audience(row.get("audience"))
        price.position = index
        price.save()
        price.addons.set(_keys_to_ids(row.get("addon_keys"), keys))
        kept_prices.append(price.id)
    ReservationPrice.objects.filter(club=club, lesson__isnull=True).exclude(
        id__in=kept_prices
    ).delete()


def sync_lesson_catalog(lesson, payload):
    customized = bool(payload.get("customized"))
    if lesson.reservation_menu_customized != customized:
        lesson.reservation_menu_customized = customized
        lesson.save(update_fields=["reservation_menu_customized"])
    if not customized:
        LessonPriceOffer.objects.filter(lesson=lesson).delete()
        ReservationPrice.objects.filter(lesson=lesson).delete()
        ReservationAddon.objects.filter(lesson=lesson).delete()
        return

    existing_addons = {
        addon.id: addon for addon in ReservationAddon.objects.filter(lesson=lesson)
    }
    kept_addons = []
    lesson_keys = {}
    for index, row in enumerate(_rows(payload, "addons")):
        if not isinstance(row, dict):
            raise ValueError("料金の形式が正しくありません。")
        if not str(row.get("name") or "").strip():
            continue
        addon = _take_existing(existing_addons, row.get("id"))
        if addon is None:
            addon = ReservationAddon(club_id=lesson.club_id, lesson=lesson)
        addon.club_id = lesson.club_id
        addon.lesson = lesson
        addon.name = _name(row.get("name"), "追加メニュー")
        addon.amount = _yen(row.get("amount"))
        addon.position = index
        addon.save()
        kept_addons.append(addon.id)
        if row.get("key") not in (None, ""):
            lesson_keys[str(row.get("key"))] = addon.id
    ReservationAddon.objects.filter(lesson=lesson).exclude(id__in=kept_addons).delete()

    club_addon_ids = set(
        ReservationAddon.objects.filter(
            club_id=lesson.club_id,
            lesson__isnull=True,
        ).values_list("id", flat=True)
    )
    lesson_addon_ids = set(kept_addons)

    def resolve(keys):
        if not isinstance(keys, list):
            raise ValueError("料金の形式が正しくありません。")
        ids = []
        seen = set()
        for key in keys:
            text = str(key)
            if text.startswith("c-"):
                addon_id = _int_id(text[2:])
                if addon_id not in club_addon_ids:
                    raise ValueError("選択した追加メニューは見つかりません。")
            elif text.startswith("l-"):
                addon_id = lesson_keys.get(text[2:])
                if addon_id not in lesson_addon_ids:
                    raise ValueError("選択した追加メニューは見つかりません。")
            else:
                raise ValueError("追加メニューの指定が正しくありません。")
            if addon_id in seen:
                continue
            seen.add(addon_id)
            ids.append(addon_id)
        return ids

    club_price_ids = set(
        ReservationPrice.objects.filter(
            club_id=lesson.club_id,
            lesson__isnull=True,
        ).values_list("id", flat=True)
    )
    LessonPriceOffer.objects.filter(lesson=lesson).delete()
    seen_prices = set()
    for row in _rows(payload, "offers"):
        if not isinstance(row, dict):
            raise ValueError("料金の形式が正しくありません。")
        price_id = _int_id(row.get("price_id"))
        if price_id not in club_price_ids or price_id in seen_prices:
            continue
        seen_prices.add(price_id)
        override = bool(row.get("override"))
        offer = LessonPriceOffer.objects.create(
            lesson=lesson,
            price_id=price_id,
            addons_overridden=override,
        )
        if override:
            offer.addons.set(resolve(row.get("addon_keys") or []))

    existing_prices = {
        price.id: price for price in ReservationPrice.objects.filter(lesson=lesson)
    }
    kept_prices = []
    for index, row in enumerate(_rows(payload, "prices")):
        if not isinstance(row, dict):
            raise ValueError("料金の形式が正しくありません。")
        if not str(row.get("name") or "").strip():
            continue
        price = _take_existing(existing_prices, row.get("id"))
        if price is None:
            price = ReservationPrice(club_id=lesson.club_id, lesson=lesson)
        price.club_id = lesson.club_id
        price.lesson = lesson
        price.name = _name(row.get("name"), "料金")
        price.amount = _yen(row.get("amount"))
        price.audience = _audience(row.get("audience"))
        price.position = index
        price.save()
        price.addons.set(resolve(row.get("addon_keys") or []))
        kept_prices.append(price.id)
    ReservationPrice.objects.filter(lesson=lesson).exclude(id__in=kept_prices).delete()


def _take_existing(existing, raw_id):
    if raw_id in (None, ""):
        return None
    try:
        return existing.get(int(raw_id))
    except (TypeError, ValueError):
        return None


def _remember_key(keys, row, addon_id):
    if row.get("key") not in (None, ""):
        keys[str(row.get("key"))] = addon_id


def _keys_to_ids(raw_keys, keys):
    if not isinstance(raw_keys, list):
        raise ValueError("料金の形式が正しくありません。")
    ids = []
    seen = set()
    for key in raw_keys:
        addon_id = keys.get(str(key))
        if addon_id is None or addon_id in seen:
            continue
        seen.add(addon_id)
        ids.append(addon_id)
    return ids


def _int_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("料金の指定が正しくありません。")
