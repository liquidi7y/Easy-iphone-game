import os

from alembic import context
from sqlalchemy import create_engine

from calc.db.models import Base

target_metadata = Base.metadata


def run_migrations_online():
    url = context.config.attributes.get("url") or os.environ["DATABASE_URL"]
    connectable = context.config.attributes.get("connection")
    if connectable is not None:
        context.configure(connection=connectable, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return
    with create_engine(url).connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
