"""GL journal integrity DDL — the Postgres triggers that make the journal the
immutable money truth. One source of truth, shared by migration 012 and the
trigger tests.

Three guarantees (design §3.4/§3.5):

1. **Immutability** — ``gl_journal_lines`` rejects UPDATE/DELETE; entries reject
   DELETE and permit UPDATE only for ``status: posted->reversed`` and
   ``reversed_by_entry_id: NULL->value``.
2. **Sealing** — a line may only be inserted in the transaction that created its
   entry (``entry.created_txid = txid_current()``).
3. **Balance invariant** — a DEFERRABLE INITIALLY DEFERRED constraint trigger
   asserts, at commit, ``SUM(amount_cents)=0`` with ``COUNT(*)>=2`` and at least
   one debit and one credit per entry.

Idempotent: CREATE OR REPLACE FUNCTION + DROP TRIGGER IF EXISTS then CREATE.
Postgres-only (txid_current, to_jsonb, constraint triggers).
"""
from __future__ import annotations

# Function definitions (CREATE OR REPLACE — safe to re-run).
_FUNCTIONS = (
    # Lines are append-only.
    """
    CREATE OR REPLACE FUNCTION gl_lines_immutable() RETURNS trigger AS $fn$
    BEGIN
      RAISE EXCEPTION 'gl_journal_lines is append-only (% rejected)', TG_OP;
    END
    $fn$ LANGUAGE plpgsql;
    """,
    # Entries are append-only except two audited transitions.
    """
    CREATE OR REPLACE FUNCTION gl_entries_immutable() RETURNS trigger AS $fn$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'gl_journal_entries is append-only (DELETE rejected)';
      END IF;

      IF NEW.status IS DISTINCT FROM OLD.status
         AND NOT (OLD.status = 'posted' AND NEW.status = 'reversed') THEN
        RAISE EXCEPTION
          'gl_journal_entries.status may only change posted->reversed (got %->%)',
          OLD.status, NEW.status;
      END IF;

      IF NEW.reversed_by_entry_id IS DISTINCT FROM OLD.reversed_by_entry_id
         AND OLD.reversed_by_entry_id IS NOT NULL THEN
        RAISE EXCEPTION 'gl_journal_entries.reversed_by_entry_id is write-once';
      END IF;

      -- nothing else may change (robust to future columns)
      IF (to_jsonb(OLD) - 'status' - 'reversed_by_entry_id')
         IS DISTINCT FROM (to_jsonb(NEW) - 'status' - 'reversed_by_entry_id') THEN
        RAISE EXCEPTION
          'gl_journal_entries is append-only (only status / reversed_by_entry_id may change)';
      END IF;

      RETURN NEW;
    END
    $fn$ LANGUAGE plpgsql;
    """,
    # Sealing: lines only insertable in the entry's creating transaction.
    """
    CREATE OR REPLACE FUNCTION gl_lines_sealing() RETURNS trigger AS $fn$
    DECLARE
      entry_txid bigint;
    BEGIN
      SELECT created_txid INTO entry_txid
        FROM gl_journal_entries WHERE id = NEW.entry_id;
      IF entry_txid IS NULL THEN
        RAISE EXCEPTION
          'gl_journal_lines: parent entry % has no created_txid (engine must set it = txid_current())',
          NEW.entry_id;
      END IF;
      IF entry_txid IS DISTINCT FROM txid_current() THEN
        RAISE EXCEPTION
          'gl_journal_lines: entry % is sealed; lines may only be inserted in the creating transaction',
          NEW.entry_id;
      END IF;
      RETURN NEW;
    END
    $fn$ LANGUAGE plpgsql;
    """,
    # Balance invariant (deferred to commit).
    """
    CREATE OR REPLACE FUNCTION gl_assert_entry_balanced() RETURNS trigger AS $fn$
    DECLARE
      total    bigint;
      n_lines  int;
      n_debit  int;
      n_credit int;
    BEGIN
      SELECT COALESCE(SUM(amount_cents), 0),
             COUNT(*),
             COUNT(*) FILTER (WHERE amount_cents > 0),
             COUNT(*) FILTER (WHERE amount_cents < 0)
        INTO total, n_lines, n_debit, n_credit
        FROM gl_journal_lines
       WHERE entry_id = NEW.entry_id;

      IF total <> 0 THEN
        RAISE EXCEPTION 'gl entry % is unbalanced: SUM(amount_cents)=%', NEW.entry_id, total;
      END IF;
      IF n_lines < 2 THEN
        RAISE EXCEPTION 'gl entry % must have at least 2 lines (has %)', NEW.entry_id, n_lines;
      END IF;
      IF n_debit < 1 OR n_credit < 1 THEN
        RAISE EXCEPTION 'gl entry % needs at least one debit and one credit line', NEW.entry_id;
      END IF;
      RETURN NULL;
    END
    $fn$ LANGUAGE plpgsql;
    """,
)

# (table, trigger_name, create-body). Dropped-then-created for idempotency.
_TRIGGERS = (
    (
        "gl_journal_lines",
        "trg_gl_lines_immutable",
        """
        CREATE TRIGGER trg_gl_lines_immutable
          BEFORE UPDATE OR DELETE ON gl_journal_lines
          FOR EACH ROW EXECUTE FUNCTION gl_lines_immutable();
        """,
    ),
    (
        "gl_journal_entries",
        "trg_gl_entries_immutable",
        """
        CREATE TRIGGER trg_gl_entries_immutable
          BEFORE UPDATE OR DELETE ON gl_journal_entries
          FOR EACH ROW EXECUTE FUNCTION gl_entries_immutable();
        """,
    ),
    (
        "gl_journal_lines",
        "trg_gl_lines_sealing",
        """
        CREATE TRIGGER trg_gl_lines_sealing
          BEFORE INSERT ON gl_journal_lines
          FOR EACH ROW EXECUTE FUNCTION gl_lines_sealing();
        """,
    ),
    (
        "gl_journal_lines",
        "trg_gl_lines_balance",
        """
        CREATE CONSTRAINT TRIGGER trg_gl_lines_balance
          AFTER INSERT ON gl_journal_lines
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION gl_assert_entry_balanced();
        """,
    ),
)

_FUNCTION_NAMES = (
    "gl_lines_immutable()",
    "gl_entries_immutable()",
    "gl_lines_sealing()",
    "gl_assert_entry_balanced()",
)


def _raw_execute(bind, statements) -> None:
    """Execute DDL through the raw psycopg2 cursor (no params).

    The function bodies contain literal ``%`` (RAISE EXCEPTION format specifiers).
    SQLAlchemy's ``exec_driver_sql`` always hands psycopg2 an (empty) parameter
    mapping, which makes psycopg2 run ``%``-interpolation and choke on those
    literals. Executing on the DBAPI cursor with a single argument skips
    interpolation, so the ``%`` are preserved verbatim. ``bind`` is a SQLAlchemy
    Connection (or Alembic ``op.get_bind()``) — we borrow its DBAPI connection so
    the DDL runs inside the caller's transaction.
    """
    cursor = bind.connection.dbapi_connection.cursor()
    try:
        for sql in statements:
            cursor.execute(sql)
    finally:
        cursor.close()


def install_gl_triggers(bind) -> None:
    """Create/replace the GL integrity functions and triggers. ``bind`` is a
    SQLAlchemy Connection (or Alembic ``op.get_bind()``). Idempotent.
    """
    stmts = list(_FUNCTIONS)
    for tbl, trg, create_sql in _TRIGGERS:
        stmts.append(f"DROP TRIGGER IF EXISTS {trg} ON {tbl};")
        stmts.append(create_sql)
    _raw_execute(bind, stmts)


def drop_gl_triggers(bind) -> None:
    """Drop the GL integrity triggers and functions. Leaves the tables intact."""
    stmts = [f"DROP TRIGGER IF EXISTS {trg} ON {tbl};" for tbl, trg, _ in _TRIGGERS]
    stmts += [f"DROP FUNCTION IF EXISTS {fn};" for fn in _FUNCTION_NAMES]
    _raw_execute(bind, stmts)
