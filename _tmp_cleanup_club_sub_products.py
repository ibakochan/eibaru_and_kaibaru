"""One-off: delete leftover junk products on platform Stripe.

Targets:
  - "Kaibaru Club Subscription" duplicates
  - "Prorated payment until end of month" leftovers

Keeps any product referenced by settings price IDs / worker product id.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

env_path = Path(__file__).resolve().parent / "mysite" / ".env"
for line in env_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip().strip('"').strip("'")
    if key and key not in os.environ:
        os.environ[key] = value

import stripe

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

KEEP_PRICE_IDS = {
    "price_1SeNvyEuyKDnirof2pUdCR0v",  # STRIPE_BASE_PRICE_ID
    "price_1UQzRZEuyKDnirofHlor1CAj",  # STRIPE_BASE_PRICE_ID_REDUCED
    "price_1SeRBPEuyKDnirof40RPZZnw",  # STRIPE_MEMBER_PRICE_ID
    "price_1UQzXIEuyKDnirofJDEOQsMr",  # STRIPE_MEMBER_PRICE_ID_REDUCED
    "price_1UbVSEEuyKDnirofcp382Yle",  # STRIPE_WORKER_PRICE_ID
    "price_1UWdj7EuyKDnirofTYIVi0AP",  # STRIPE_CUSTOM_DOMAIN_PRICE_ID
    "price_1UWdlAEuyKDnirofQaW0HMGv",  # STRIPE_CUSTOM_DOMAIN_EXTRA_PRICE_ID
}
KEEP_PRODUCT_IDS = {"prod_VckyHlwEN01QJC"}  # STRIPE_WORKER_PRODUCT_ID

APPLY = os.environ.get("APPLY", "").lower() in ("1", "true", "yes")

# Match either leftover product name family.
NAME_PATTERN = re.compile(
    r"(kaibaru\s*club\s*subscription)"
    r"|(prorated\s+payment\s+until\s+end\s+of\s+month)",
    re.I,
)


def list_all_products(*, active: bool):
    starting_after = None
    while True:
        kwargs = {"limit": 100, "active": active}
        if starting_after:
            kwargs["starting_after"] = starting_after
        page = stripe.Product.list(**kwargs)
        for product in page.data:
            yield product
        if not page.has_more:
            break
        starting_after = page.data[-1].id


def main():
    for price_id in KEEP_PRICE_IDS:
        try:
            price = stripe.Price.retrieve(price_id)
            product = price.get("product")
            if isinstance(product, dict):
                product = product.get("id")
            if product:
                KEEP_PRODUCT_IDS.add(product)
                print(f"keep via {price_id} -> {product}")
        except Exception as exc:
            print(f"WARN could not resolve {price_id}: {exc}")

    print("KEEP_PRODUCT_IDS:", sorted(KEEP_PRODUCT_IDS))

    matches = []
    seen = set()
    for active in (True, False):
        for product in list_all_products(active=active):
            name = product.get("name") or ""
            if not NAME_PATTERN.search(name):
                continue
            if product.id in seen:
                continue
            seen.add(product.id)
            matches.append(product)

    print(f"matched products: {len(matches)}")
    to_remove = []
    for product in matches:
        keep = product.id in KEEP_PRODUCT_IDS
        flag = "KEEP" if keep else "DELETE"
        print(
            f"  [{flag}] {product.id} active={product.active} "
            f"name={product.name!r} created={product.created}"
        )
        if not keep:
            to_remove.append(product)

    if not APPLY:
        print(
            f"\nDry run only ({len(to_remove)} would be removed). "
            "Re-run with APPLY=1 to delete/archive."
        )
        return

    deleted = 0
    archived = 0
    failed = 0
    for product in to_remove:
        try:
            stripe.Product.delete(product.id)
            deleted += 1
            print(f"deleted {product.id}")
        except Exception as exc:
            try:
                stripe.Product.modify(product.id, active=False)
                archived += 1
                print(f"archived {product.id} (delete failed: {exc})")
            except Exception as exc2:
                failed += 1
                print(f"FAILED {product.id}: delete={exc}; archive={exc2}")

    print(
        f"\nDone. deleted={deleted} archived={archived} failed={failed} "
        f"kept={len(KEEP_PRODUCT_IDS)}"
    )


if __name__ == "__main__":
    main()
