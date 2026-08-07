"""job photos on the invoice PDF

Invoice job-photo attachments (Doug 2026-08-07): the office can pick job
photos on a draft invoice and they print as a "Job Photos" grid on the
invoice PDF — before/after shots justify the bill wherever the PDF goes
(email, print, postal mail). attached_photo_ids stores the SELECTION as a
JSON array of job_photos.id strings; NULL/empty = no photos section. The
photos themselves stay in job_photos/documents — this is only the per-
invoice pick, mirroring how pdf_templates stores its blocks as JSON text.

Revision ID: 059_invoice_attached_photos
Revises: 058_invoice_origin
"""
from alembic import op

revision = "059_invoice_attached_photos"
down_revision = "058_invoice_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # invoices is in the squashed baseline, so it always exists here; IF NOT
    # EXISTS keeps the ALTER idempotent across multi-container boots.
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS attached_photo_ids TEXT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE invoices DROP COLUMN IF EXISTS attached_photo_ids")
