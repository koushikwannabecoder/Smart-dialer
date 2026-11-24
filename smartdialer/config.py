import os


class ConfigurationError(ValueError):
    """A required setting is missing."""


def database_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise ConfigurationError("Set DATABASE_URL to the PostgreSQL connection URL.")
    return value
