class CockpitError(Exception):
    """Base class for expected modal-cockpit failures."""


class ConfigError(CockpitError):
    """Modal configuration is missing, invalid, or unsafe to mutate."""


class StoreError(CockpitError):
    """Account store operation failed."""


class CryptoError(CockpitError):
    """Encrypted payload or encryption key is invalid."""


class ExternalCommandError(CockpitError):
    """A child process failed."""
