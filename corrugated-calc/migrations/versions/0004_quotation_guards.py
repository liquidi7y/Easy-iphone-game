"""Защити: неизменими финализирани оферти, минимуми по материал в одобрени версии, история на палетни цени.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TRIGGER trg_material_order_rules_guard BEFORE INSERT OR UPDATE OR DELETE ON material_order_rules
  FOR EACH ROW EXECUTE FUNCTION plv_content_guard();

CREATE OR REPLACE FUNCTION qv_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'Финализирана версия на оферта не може да се изтрие';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.status <> 'draft' AND (
       NEW.currency IS DISTINCT FROM OLD.currency OR NEW.valid_until IS DISTINCT FROM OLD.valid_until OR
       NEW.delivery_terms IS DISTINCT FROM OLD.delivery_terms OR NEW.payment_terms IS DISTINCT FROM OLD.payment_terms OR
       NEW.client_notes IS DISTINCT FROM OLD.client_notes OR NEW.internal_notes IS DISTINCT FROM OLD.internal_notes OR
       NEW.totals IS DISTINCT FROM OLD.totals OR NEW.snapshot IS DISTINCT FROM OLD.snapshot OR
       NEW.version IS DISTINCT FROM OLD.version OR NEW.quotation_id IS DISTINCT FROM OLD.quotation_id OR
       NEW.finalized_at IS DISTINCT FROM OLD.finalized_at OR NEW.finalized_by IS DISTINCT FROM OLD.finalized_by) THEN
    RAISE EXCEPTION 'Финализирана оферта не се променя; създайте нова версия';
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
       (OLD.status = 'draft' AND NEW.status IN ('final', 'cancelled')) OR
       (OLD.status = 'final' AND NEW.status IN ('sent', 'accepted', 'rejected', 'cancelled')) OR
       (OLD.status = 'sent' AND NEW.status IN ('accepted', 'rejected', 'cancelled'))) THEN
    RAISE EXCEPTION 'Недопустим преход на оферта % -> %', OLD.status, NEW.status;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_qv_guard BEFORE UPDATE OR DELETE ON quotation_versions
  FOR EACH ROW EXECUTE FUNCTION qv_guard();

CREATE OR REPLACE FUNCTION ql_guard() RETURNS trigger AS $$
DECLARE st text;
BEGIN
  SELECT status INTO st FROM quotation_versions
   WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.version_id ELSE NEW.version_id END;
  IF st IS DISTINCT FROM 'draft' THEN
    RAISE EXCEPTION 'Редовете на оферта със статус % не се променят', st;
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_ql_guard BEFORE INSERT OR UPDATE OR DELETE ON quotation_lines
  FOR EACH ROW EXECUTE FUNCTION ql_guard();

CREATE OR REPLACE FUNCTION pallet_price_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'Историята на палетните цени не се променя; добавете нов запис';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_pallet_price_append_only BEFORE UPDATE OR DELETE ON pallet_prices
  FOR EACH ROW EXECUTE FUNCTION pallet_price_append_only();
"""

DOWNGRADE = r"""
DROP TRIGGER IF EXISTS trg_pallet_price_append_only ON pallet_prices;
DROP TRIGGER IF EXISTS trg_ql_guard ON quotation_lines;
DROP TRIGGER IF EXISTS trg_qv_guard ON quotation_versions;
DROP TRIGGER IF EXISTS trg_material_order_rules_guard ON material_order_rules;
DROP FUNCTION IF EXISTS pallet_price_append_only, ql_guard, qv_guard;
"""


def upgrade():
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
