"""Bank Feeds — sync accounts, transactions, and statement PDFs from
Banno-powered (Jack Henry Digital Toolkit) financial institutions.

Design: ~/.claude/plans (rev 3, audited). Multi-institution: each Banno
bank provisions its own external application (client_id/secret in Banno
People), so institutions are rows, not a singleton config. The transaction
store is provider-agnostic (``provider`` column) so a future Plaid/CSV
adapter can join the same tables.

Future consumers (deliberately NOT wired here): the ADR-016 overhead
completeness badge (recurring-debit detection) and GL Phase 2 bank
reconciliation (``bank_statement_lines`` fold-in). Consumers must filter
``amount_cents IS NOT NULL AND pending = FALSE AND deleted_at IS NULL``.
"""
