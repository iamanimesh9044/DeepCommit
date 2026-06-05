from . import main


def run() -> None:
    """Entry point for ``python -m deepcommit.cli``."""

    main()


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    run()
