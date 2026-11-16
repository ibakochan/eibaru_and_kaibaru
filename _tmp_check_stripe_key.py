from pathlib import Path

for line in Path("mysite/.env").read_text(encoding="utf-8").splitlines():
    if line.startswith("STRIPE_SECRET_KEY="):
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        print(
            "prefix",
            value[:10],
            "len",
            len(value),
            "live",
            value.startswith("sk_live"),
            "test",
            value.startswith("sk_test"),
        )
        break
