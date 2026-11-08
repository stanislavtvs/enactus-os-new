"""Create local Compose settings with unique secrets."""
import os
import secrets
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    content = (root / ".env.example").read_text(encoding="utf-8")
    for name, size in (("DJANGO_SECRET_KEY", 48), ("POSTGRES_PASSWORD", 32)):
        content = content.replace(f"{name}=\n", f"{name}={secrets.token_urlsafe(size)}\n")

    try:
        fd = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(".env already exists. Existing settings were preserved.") from None

    with os.fdopen(fd, "w", encoding="utf-8") as env:
        env.write(content)
    print("Created .env with local Compose settings.")


if __name__ == "__main__":
    main()
