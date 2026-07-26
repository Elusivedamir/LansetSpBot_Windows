"""Single source of truth for the public LansetSpBot application identity."""

__version__ = "4.8.0"
BUILD_ID = "V46-OPENAI-PREMIUM-GUI"
APP_NAME = "LansetSpBot"
APP_BUNDLE_NAME = f"{APP_NAME}.app"
APP_BUNDLE_ID = "com.lanset.spbot"

# The existing local storage identifiers intentionally remain legacy-compatible
# so an update opens the user's current database, sessions and encrypted secrets.
LEGACY_STORAGE_NAME = "Marlen"
