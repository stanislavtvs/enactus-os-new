import re

from django.core.exceptions import ImproperlyConfigured
from django.db.backends.postgresql.creation import DatabaseCreation as PostgreSQLDatabaseCreation


class DatabaseCreation(PostgreSQLDatabaseCreation):
    def __init__(self, connection):
        super().__init__(connection)
        # Django changes NAME in place while the test suite is running.
        self._working_database_name = connection.settings_dict["NAME"]
        self._created_test_database = None

    def _check_test_database_name(self, name):
        if (
            name == self._working_database_name
            or not isinstance(name, str)
            or not re.fullmatch(r"test_enactus_[a-z0-9_]{1,48}", name)
        ):
            raise ImproperlyConfigured("Enactus tests require a separate test_enactus_* database.")

    def _create_test_db(self, verbosity, autoclobber, keepdb=False):
        if keepdb:
            raise ImproperlyConfigured("Enactus tests do not reuse test databases. Omit --keepdb.")

        name = self.connection.settings_dict["TEST"]["NAME"]
        self._check_test_database_name(name)
        if self._created_test_database is not None:
            raise ImproperlyConfigured("This connection has already created a test database.")

        quoted_name = self.connection.ops.quote_name(name)
        suffix = self.sql_table_creation_suffix()
        # A collision must fail. Django's default fallback can drop an existing database.
        with self._nodb_cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {quoted_name} {suffix}")

        self._created_test_database = name
        return name

    def _destroy_test_db(self, test_database_name, verbosity):
        self._check_test_database_name(test_database_name)
        if test_database_name != self._created_test_database:
            raise ImproperlyConfigured("Refusing to drop a database not created by this test run.")

        super()._destroy_test_db(test_database_name, verbosity)
        self._created_test_database = None

    def _clone_test_db(self, suffix, verbosity, keepdb=False):
        raise ImproperlyConfigured("Enactus tests use one isolated database. Omit --parallel.")
