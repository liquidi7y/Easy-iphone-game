"""Защити в базата: неизменими ценови версии, разрешени преходи, append-only audit log.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE OR REPLACE FUNCTION plv_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'Версия % на ценоразпис със статус % не може да се изтрие', OLD.version, OLD.status;
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.status <> 'draft' AND (
       NEW.currency IS DISTINCT FROM OLD.currency OR NEW.price_unit IS DISTINCT FROM OLD.price_unit OR
       NEW.tax_basis IS DISTINCT FROM OLD.tax_basis OR NEW.valid_from IS DISTINCT FROM OLD.valid_from OR
       NEW.valid_to IS DISTINCT FROM OLD.valid_to OR NEW.min_order_m2 IS DISTINCT FROM OLD.min_order_m2 OR
       NEW.min_invoiced_m2 IS DISTINCT FROM OLD.min_invoiced_m2 OR NEW.version IS DISTINCT FROM OLD.version OR
       NEW.price_list_id IS DISTINCT FROM OLD.price_list_id OR NEW.tier_basis IS DISTINCT FROM OLD.tier_basis) THEN
    RAISE EXCEPTION 'Съдържанието на одобрена версия не се променя; създайте нова версия';
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
       (OLD.status = 'draft' AND NEW.status IN ('approved')) OR
       (OLD.status = 'approved' AND NEW.status IN ('active', 'reference', 'archived')) OR
       (OLD.status = 'active' AND NEW.status IN ('archived', 'reference')) OR
       (OLD.status = 'reference' AND NEW.status IN ('archived'))) THEN
    RAISE EXCEPTION 'Недопустим преход на статус % -> %', OLD.status, NEW.status;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_plv_guard BEFORE UPDATE OR DELETE ON price_list_versions
  FOR EACH ROW EXECUTE FUNCTION plv_guard();

CREATE OR REPLACE FUNCTION plv_content_guard() RETURNS trigger AS $$
DECLARE st text;
BEGIN
  SELECT status INTO st FROM price_list_versions
   WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.version_id ELSE NEW.version_id END;
  IF st IS DISTINCT FROM 'draft' THEN
    RAISE EXCEPTION 'Прагове и цени на версия със статус % не се променят', st;
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.version_id <> OLD.version_id THEN
    RAISE EXCEPTION 'Редът не може да се премести в друга версия';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_tiers_guard BEFORE INSERT OR UPDATE OR DELETE ON price_tiers
  FOR EACH ROW EXECUTE FUNCTION plv_content_guard();
CREATE TRIGGER trg_prices_guard BEFORE INSERT OR UPDATE OR DELETE ON material_price_tiers
  FOR EACH ROW EXECUTE FUNCTION plv_content_guard();

CREATE OR REPLACE FUNCTION audit_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_log е само за добавяне';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_append_only();

CREATE OR REPLACE FUNCTION import_file_guard() RETURNS trigger AS $$
BEGIN
  IF NEW.content IS DISTINCT FROM OLD.content OR NEW.sha256 IS DISTINCT FROM OLD.sha256 THEN
    RAISE EXCEPTION 'Оригиналният файл не се променя';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_import_file_guard BEFORE UPDATE ON import_files
  FOR EACH ROW EXECUTE FUNCTION import_file_guard();
"""

DOWNGRADE = r"""
DROP TRIGGER IF EXISTS trg_import_file_guard ON import_files;
DROP TRIGGER IF EXISTS trg_audit_append_only ON audit_log;
DROP TRIGGER IF EXISTS trg_prices_guard ON material_price_tiers;
DROP TRIGGER IF EXISTS trg_tiers_guard ON price_tiers;
DROP TRIGGER IF EXISTS trg_plv_guard ON price_list_versions;
DROP FUNCTION IF EXISTS import_file_guard, audit_append_only, plv_content_guard, plv_guard;
"""


def upgrade():
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
