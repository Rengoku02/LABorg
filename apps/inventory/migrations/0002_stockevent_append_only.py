"""The stock ledger is append-only in the database too (docs/design/mvp-a.md §1).

A trigger refuses to delete a stock event or to change any column but ``user_id``, which
retention pseudonymizes. The ORM refuses the same (StockEvent and its queryset); this catches
raw SQL and anything that slips past it. It raises SQLSTATE 23001 (restrict_violation), which
Django reports as an IntegrityError. TRUNCATE is left alone: the app never truncates, and the
test runner flushes tables with it.
"""

from django.db import migrations

FUNCTION = "inventory_stockevent_append_only"
TRIGGER = "inventory_stockevent_append_only"

FORWARD_SQL = [
    f"""
    CREATE FUNCTION {FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'inventory_stockevent is append-only: events are never deleted'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF (to_jsonb(NEW) - 'user_id') IS DISTINCT FROM (to_jsonb(OLD) - 'user_id') THEN
            RAISE EXCEPTION 'inventory_stockevent is append-only: only user_id may change'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    f"""
    CREATE TRIGGER {TRIGGER}
    BEFORE UPDATE OR DELETE ON inventory_stockevent
    FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()
    """,
]
REVERSE_SQL = [
    f"DROP TRIGGER IF EXISTS {TRIGGER} ON inventory_stockevent",
    f"DROP FUNCTION IF EXISTS {FUNCTION}()",
]


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
