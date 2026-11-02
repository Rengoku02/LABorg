"""The database keeps stock events append-only (migration 0002), and the migration reverses."""

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.loader import MigrationLoader

TRIGGER = "inventory_stockevent_append_only"
MIGRATION = ("inventory", "0002_stockevent_append_only")


def run(sql, *params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def refused(sql, *params):
    try:
        with transaction.atomic():
            run(sql, *params)
    except IntegrityError as exc:
        assert "append-only" in str(exc)
        return True
    return False


def trigger_exists():
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_trigger WHERE tgname = %s", [TRIGGER])
        return cursor.fetchone()[0] == 1


@pytest.mark.django_db
def test_the_database_refuses_to_change_or_delete_an_event(item, member):
    event = item.events.get()
    for column, value in [
        ("reason", "rewritten"),
        ("kind", "note"),
        ("seq", 7),
        ("occurred_at", "2020-01-01T00:00:00Z"),
        ("data", "{}"),
        ("to_custodian_id", None),
        ("client_uuid", "6f1c2c1e-6c9a-4a52-9a43-58f7c1c0a111"),
    ]:
        sql = f"UPDATE inventory_stockevent SET {column} = %s WHERE id = %s"  # noqa: S608
        assert refused(sql, value, event.pk), column
    assert refused("DELETE FROM inventory_stockevent WHERE id = %s", event.pk)
    assert refused("DELETE FROM inventory_stockevent")
    # Retention pseudonymizes the person who acted; that alone may change.
    assert run("UPDATE inventory_stockevent SET user_id = NULL WHERE id = %s", event.pk) == 1
    assert run("UPDATE inventory_stockevent SET user_id = %s WHERE id = %s", member.pk, event.pk)
    # An update that changes nothing at all is harmless.
    assert run("UPDATE inventory_stockevent SET seq = seq WHERE id = %s", event.pk) == 1
    assert item.events.get().reason == ""


@pytest.mark.django_db
def test_the_trigger_migration_reverses(item):
    event = item.events.get()
    loader = MigrationLoader(connection)
    migration = loader.get_migration(*MIGRATION)
    run("SET CONSTRAINTS ALL IMMEDIATE")  # no pending foreign-key checks before the DDL
    with connection.schema_editor() as editor:
        migration.unapply(loader.project_state(MIGRATION), editor)
    assert not trigger_exists()
    assert not refused("UPDATE inventory_stockevent SET reason = 'x' WHERE id = %s", event.pk)

    run("SET CONSTRAINTS ALL IMMEDIATE")
    with connection.schema_editor() as editor:
        migration.apply(loader.project_state(("inventory", "0001_initial")), editor)
    assert trigger_exists()
    assert refused("UPDATE inventory_stockevent SET reason = 'y' WHERE id = %s", event.pk)
    assert refused("DELETE FROM inventory_stockevent WHERE id = %s", event.pk)
