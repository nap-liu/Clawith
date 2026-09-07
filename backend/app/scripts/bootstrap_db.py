"""Prepare a new database or upgrade an existing versioned database."""

from pathlib import Path

from alembic import command
from alembic.config import Config


def main() -> None:
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.attributes["bootstrap_current_schema"] = True
    command.upgrade(config, "heads")


if __name__ == "__main__":
    main()
