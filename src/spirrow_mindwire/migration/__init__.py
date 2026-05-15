"""Schema migration utilities.

Each migration step is a self-contained module (``vN_to_vM``) exposing
``migrate_data_dir`` + a CLI ``main``. The package itself stays small;
adding a new bump means dropping in another module, not touching the
others.
"""

from __future__ import annotations

from .v1_to_v2 import MigrationReport, ThreadOutcome, migrate_data_dir

__all__ = ["MigrationReport", "ThreadOutcome", "migrate_data_dir"]
