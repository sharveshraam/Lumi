"""Single source of truth for application identity and version."""

from __future__ import annotations

APP_NAME = "LUMI"
APP_SLUG = "lumi"
APP_TAGLINE = "Local-first AI desktop assistant"
__version__ = "1.0.0"

#: Schema version of settings.json.  Bumped when the on-disk settings layout
#: changes in a way that requires migration.
SETTINGS_SCHEMA_VERSION = 3

#: Schema version of the SQLite database (see lumi.memory.db migrations).
DB_SCHEMA_VERSION = 2
