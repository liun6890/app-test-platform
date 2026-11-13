"""Allow ``python -m app_press_test`` to use the CLI."""

from .cli import main


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
