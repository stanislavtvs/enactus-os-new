from unittest.mock import MagicMock, patch

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError
from django.db.backends.postgresql.creation import DatabaseCreation as PostgreSQLCreation
from django.test import SimpleTestCase

from config.db.backends.postgresql.creation import DatabaseCreation


WORKING_DB = "enactus_working"
TEST_DB = "test_enactus_safety"


class DatabaseSafetyTests(SimpleTestCase):
    def make_backend(self, *, working=WORKING_DB, test=TEST_DB):
        connection = MagicMock()
        connection.alias = "default"
        connection.settings_dict = {
            "NAME": working,
            "TEST": {
                "NAME": test,
                "CHARSET": None,
                "COLLATION": None,
                "TEMPLATE": None,
            },
        }
        connection.ops.quote_name.side_effect = lambda name: f'"{name}"'
        cursor = connection._nodb_cursor.return_value.__enter__.return_value
        return DatabaseCreation(connection), connection, cursor

    def test_destroy_cannot_drop_working_db_even_if_test_name_is_given(self):
        backend, connection, cursor = self.make_backend()
        backend._create_test_db(verbosity=0, autoclobber=False, keepdb=False)
        cursor.reset_mock()
        connection._nodb_cursor.reset_mock()

        # Django's public method uses the connection NAME for the database it
        # destroys. Its argument is the name to restore after teardown.
        with self.assertRaises(ImproperlyConfigured):
            backend.destroy_test_db(TEST_DB, verbosity=0)

        self.assertEqual(connection.settings_dict["NAME"], WORKING_DB)
        connection._nodb_cursor.assert_not_called()
        cursor.execute.assert_not_called()

    def test_working_database_name_is_rejected_even_with_allowed_prefix(self):
        backend, connection, cursor = self.make_backend(working=TEST_DB)

        with self.assertRaises(ImproperlyConfigured):
            backend._create_test_db(verbosity=0, autoclobber=True, keepdb=False)

        connection._nodb_cursor.assert_not_called()
        cursor.execute.assert_not_called()

    def test_missing_or_unsafe_test_name_is_rejected_before_sql(self):
        for name in (
            None,
            "",
            "another_database",
            "test_enactus_",
            "test_enactus_name;drop",
            "test_enactus_" + "x" * 64,
        ):
            with self.subTest(name=name):
                backend, connection, cursor = self.make_backend(test=name)

                with self.assertRaises(ImproperlyConfigured):
                    backend._create_test_db(verbosity=0, autoclobber=True, keepdb=False)

                connection._nodb_cursor.assert_not_called()
                cursor.execute.assert_not_called()

    def test_rejected_create_never_drops_or_claims_a_preexisting_database(self):
        backend, connection, cursor = self.make_backend()
        cursor.execute.side_effect = DatabaseError("database already exists")

        with self.assertRaises((DatabaseError, ImproperlyConfigured)):
            backend._create_test_db(verbosity=0, autoclobber=True, keepdb=False)

        cursor.execute.assert_called_once()
        self.assertIn(f'CREATE DATABASE "{TEST_DB}"', cursor.execute.call_args.args[0])
        connection.settings_dict["NAME"] = TEST_DB

        with patch.object(PostgreSQLCreation, "_destroy_test_db") as destroy:
            with self.assertRaises(ImproperlyConfigured):
                backend._destroy_test_db(TEST_DB, verbosity=0)
            destroy.assert_not_called()

    def test_owned_test_database_can_be_destroyed_but_not_a_different_test_db(self):
        backend, connection, cursor = self.make_backend()

        name = backend._create_test_db(verbosity=0, autoclobber=False, keepdb=False)

        self.assertEqual(name, TEST_DB)
        cursor.execute.assert_called_once()
        self.assertIn(f'CREATE DATABASE "{TEST_DB}"', cursor.execute.call_args.args[0])
        connection.settings_dict["NAME"] = TEST_DB
        with patch.object(PostgreSQLCreation, "_destroy_test_db") as destroy:
            with self.assertRaises(ImproperlyConfigured):
                backend._destroy_test_db("test_enactus_other", verbosity=0)
            destroy.assert_not_called()

            backend._destroy_test_db(TEST_DB, verbosity=0)
            destroy.assert_called_once()
            self.assertEqual(destroy.call_args.args[0], TEST_DB)

            with self.assertRaises(ImproperlyConfigured):
                backend._destroy_test_db(TEST_DB, verbosity=0)
            destroy.assert_called_once()

    def test_keepdb_and_cloning_are_rejected_before_sql(self):
        backend, connection, cursor = self.make_backend()

        with self.assertRaises(ImproperlyConfigured):
            backend._create_test_db(verbosity=0, autoclobber=False, keepdb=True)
        with self.assertRaises(ImproperlyConfigured):
            backend._clone_test_db("1", verbosity=0, keepdb=False)

        connection._nodb_cursor.assert_not_called()
        cursor.execute.assert_not_called()
