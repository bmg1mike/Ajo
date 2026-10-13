from alembic import context
from sqlalchemy import create_engine, pool

from app.config import Settings
from app.models import Base

config = context.config
target_metadata = Base.metadata
# Connection injection supports isolated integration tests without reading .env.
connection = config.attributes.get("connection")


def run(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=Settings().database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif connection is not None:
    run(connection)
else:
    engine = create_engine(Settings().database_url, poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        run(connection)
    engine.dispose()
