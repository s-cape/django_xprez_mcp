from django.apps import apps
from django.test import SimpleTestCase


class NameClashTests(SimpleTestCase):
    """PostgreSQL keeps tables and the indexes behind constraints in one
    namespace; SQLite does not, so the tests would not notice a clash."""

    def test_constraint_and_index_names_differ_from_tables(self):
        tables = {model._meta.db_table for model in apps.get_models()}
        for model in apps.get_models():
            for item in [*model._meta.constraints, *model._meta.indexes]:
                self.assertNotIn(item.name, tables, f"{model.__name__}: {item.name}")
