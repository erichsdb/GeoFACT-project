"""Generate a GEOFACT_WEB_USERS entry for the web backend (FA24).

Prints "name:salt.hash" for the given username/password so it can be
appended (comma-separated) to GEOFACT_WEB_USERS in .env. Never prints or
logs the raw password itself beyond the interactive prompt.

Usage:
    uv run python scripts/add_web_user.py alice
    (prompts for the password, doesn't echo it)
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from geofact_web.auth import hash_password  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    args = parser.parse_args()

    password = getpass.getpass("Passwort: ")
    confirm = getpass.getpass("Passwort (Wiederholung): ")
    if password != confirm:
        print("Passwörter stimmen nicht überein.", file=sys.stderr)
        raise SystemExit(1)
    if not password:
        print("Passwort darf nicht leer sein.", file=sys.stderr)
        raise SystemExit(1)

    entry = f"{args.username}:{hash_password(password)}"
    print("\nEintrag für GEOFACT_WEB_USERS (an bestehende, kommagetrennt anhängen):\n")
    print(entry)


if __name__ == "__main__":
    main()
