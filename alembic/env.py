"""Alembic environment: URL comes from DATABASE_URL; metadata from models."""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from patchline import config as patchline_config  # noqa: E402
from patchline import models  # noqa: E402

alembic_config = context.config

if alembic_config.config_file_name is not None:
    try:
        fileConfig(alembic_config.config_file_name)
    except Exception:  # noqa: BLE001 - logging config is optional
        pass

target_metadata = models.Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=patchline_config.database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = alembic_config.get_section(alembic_config.config_ini_section, {})
    configuration["sqlalchemy.url"] = patchline_config.database_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
