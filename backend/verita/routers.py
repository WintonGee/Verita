"""
Database router that guards the migrator role from runtime ORM.

  - All reads/writes go to 'default' (app_role).
  - Migrations explicitly target 'migrator' via `manage.py migrate --database=migrator`.
  - This router refuses to let runtime ORM ever pick the 'migrator' connection.
"""


class AppRuntimeRouter:
    def db_for_read(self, model, **hints):  # noqa: ARG002
        return "default"

    def db_for_write(self, model, **hints):  # noqa: ARG002
        return "default"

    def allow_relation(self, obj1, obj2, **hints):  # noqa: ARG002
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):  # noqa: ARG002
        # Only run migrations against the 'migrator' connection.
        # `manage.py migrate` defaults to 'default'; we require --database=migrator.
        return db == "migrator"
