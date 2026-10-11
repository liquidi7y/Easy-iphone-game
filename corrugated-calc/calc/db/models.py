"""PostgreSQL schema. Точните стойности са NUMERIC; ценовите версии не се презаписват."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer,
                        LargeBinary, Numeric, String, Text, UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NUM = Numeric(28, 12)

ROLES = ("sales", "sales_manager", "technologist", "administrator", "read_only")
PRICE_LIST_STATUSES = ("draft", "approved", "active", "reference", "archived")
IMPORT_STATUSES = ("extracted", "validated", "approved", "failed", "cancelled")


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _now():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(300))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    __table_args__ = (CheckConstraint(f"role IN {ROLES}", name="ck_users_role"),)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = _now()
    actor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(128))
    old: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    new: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    __table_args__ = (Index("ix_audit_entity", "entity", "entity_id"),)


class RuleVersion(Base):
    """Версионирани настройки на engine (defaults, size classes, оптимизация, rounding)."""
    __tablename__ = "rule_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("kind", "version"),
                      Index("ux_rule_versions_active", "kind", unique=True, postgresql_where=text("active")))


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    from_ccy: Mapped[str] = mapped_column(String(3))
    to_ccy: Mapped[str] = mapped_column(String(3))
    rate: Mapped[Decimal] = mapped_column(NUM)
    rate_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _now()


class BoxStyle(Base):
    __tablename__ = "box_styles"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))


class FormulaVersion(Base):
    __tablename__ = "formula_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    box_style_code: Mapped[str] = mapped_column(ForeignKey("box_styles.code"))
    formula: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64))
    additives: Mapped[dict[str, Any]] = mapped_column(JSONB)
    additive_overrides: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("box_style_code", "version"),
                      Index("ux_formula_active", "box_style_code", unique=True, postgresql_where=text("active")))


class Material(Base):
    __tablename__ = "materials"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_code: Mapped[str] = mapped_column(String(32))
    factory_code: Mapped[str] = mapped_column(String(64))
    flute: Mapped[str] = mapped_column(String(8))
    layers: Mapped[Optional[int]] = mapped_column(Integer)
    paper_combination: Mapped[Optional[str]] = mapped_column(String(200))
    kraft_outer: Mapped[Optional[bool]] = mapped_column(Boolean)
    kraft_inner: Mapped[Optional[bool]] = mapped_column(Boolean)
    grammage_gsm: Mapped[Optional[Decimal]] = mapped_column(NUM)
    technical: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = _now()
    tags: Mapped[list["MaterialTag"]] = relationship(cascade="all, delete-orphan", lazy="selectin")
    alternatives: Mapped[list["MaterialAlternative"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", foreign_keys="MaterialAlternative.material_code")


class MaterialTag(Base):
    __tablename__ = "material_tags"
    material_code: Mapped[str] = mapped_column(ForeignKey("materials.code"), primary_key=True)
    tag: Mapped[str] = mapped_column(String(32), primary_key=True)


class MaterialAlternative(Base):
    __tablename__ = "material_alternatives"
    id: Mapped[int] = mapped_column(primary_key=True)
    material_code: Mapped[str] = mapped_column(ForeignKey("materials.code"))
    alternative_code: Mapped[str] = mapped_column(ForeignKey("materials.code"))
    type: Mapped[str] = mapped_column(String(16))
    priority: Mapped[int] = mapped_column(Integer, default=1)
    technical_equivalence: Mapped[bool] = mapped_column(Boolean, default=False)
    equivalence_conditions: Mapped[Optional[str]] = mapped_column(Text)
    approved_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (CheckConstraint("type IN ('ECONOMY','UPGRADE','HEAVY_DUTY','SIMILAR')"),
                      UniqueConstraint("material_code", "alternative_code"))


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)


class PriceList(Base):
    """Логически ценоразпис (доставчик + приложимост). Съдържанието е във версиите."""
    __tablename__ = "price_lists"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    company_contract: Mapped[Optional[str]] = mapped_column(String(200))
    created_at: Mapped[datetime] = _now()
    supplier: Mapped[Supplier] = relationship(lazy="joined")
    versions: Mapped[list["PriceListVersion"]] = relationship(back_populates="price_list", lazy="selectin",
                                                              order_by="PriceListVersion.version")


class PriceListVersion(Base):
    __tablename__ = "price_list_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    price_list_id: Mapped[int] = mapped_column(ForeignKey("price_lists.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3))
    price_unit: Mapped[str] = mapped_column(String(16))
    tax_basis: Mapped[str] = mapped_column(String(16))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[Optional[date]] = mapped_column(Date)
    tier_basis: Mapped[str] = mapped_column(String(64), default="purchased_m2_per_order")
    min_order_m2: Mapped[Optional[Decimal]] = mapped_column(NUM)
    min_invoiced_m2: Mapped[Optional[Decimal]] = mapped_column(NUM)
    source_import_id: Mapped[Optional[int]] = mapped_column(ForeignKey("import_jobs.id"))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    approved_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    activated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    price_list: Mapped[PriceList] = relationship(back_populates="versions")
    tiers: Mapped[list["PriceTier"]] = relationship(lazy="selectin", order_by="PriceTier.sort",
                                                    cascade="all")
    prices: Mapped[list["MaterialPriceTier"]] = relationship(lazy="selectin", cascade="all")
    material_rules: Mapped[list["MaterialOrderRule"]] = relationship(lazy="selectin", cascade="all")

    __table_args__ = (
        UniqueConstraint("price_list_id", "version"),
        CheckConstraint(f"status IN {PRICE_LIST_STATUSES}", name="ck_plv_status"),
        CheckConstraint("currency IN ('BGN','EUR')", name="ck_plv_currency"),
        CheckConstraint("price_unit IN ('per_m2','per_1000m2','per_sheet')", name="ck_plv_unit"),
        CheckConstraint("tax_basis IN ('excl_vat','incl_vat')", name="ck_plv_tax"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="ck_plv_validity"),
        Index("ux_plv_one_active", "price_list_id", unique=True, postgresql_where=text("status = 'active'")),
    )


class PriceTier(Base):
    __tablename__ = "price_tiers"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("price_list_versions.id"))
    code: Mapped[str] = mapped_column(String(16))
    sort: Mapped[int] = mapped_column(Integer)
    lower_m2: Mapped[Decimal] = mapped_column(NUM)
    lower_inclusive: Mapped[bool] = mapped_column(Boolean)
    upper_m2: Mapped[Optional[Decimal]] = mapped_column(NUM)
    upper_inclusive: Mapped[bool] = mapped_column(Boolean)
    __table_args__ = (UniqueConstraint("version_id", "code"),
                      CheckConstraint("upper_m2 IS NULL OR upper_m2 > lower_m2", name="ck_tier_bounds"),
                      CheckConstraint("lower_m2 >= 0", name="ck_tier_lower"))


class MaterialPriceTier(Base):
    __tablename__ = "material_price_tiers"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("price_list_versions.id"))
    tier_id: Mapped[int] = mapped_column(ForeignKey("price_tiers.id"))
    material_code: Mapped[str] = mapped_column(ForeignKey("materials.code"))
    price: Mapped[Decimal] = mapped_column(NUM)
    source_ref: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    tier: Mapped[PriceTier] = relationship(lazy="joined")
    __table_args__ = (UniqueConstraint("version_id", "tier_id", "material_code"),
                      CheckConstraint("price > 0", name="ck_price_positive"))


class ImportJob(Base):
    __tablename__ = "import_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    price_list_code: Mapped[Optional[str]] = mapped_column(String(64))
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    extraction: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    proposal: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    validation: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    result_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("price_list_versions.id",
                                                                        use_alter=True))
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
    files: Mapped[list["ImportFile"]] = relationship(lazy="selectin", order_by="ImportFile.id",
                                                     cascade="all, delete-orphan")
    __table_args__ = (CheckConstraint(f"status IN {IMPORT_STATUSES}", name="ck_import_status"),)


class ImportFile(Base):
    """Оригиналният файл се пази непроменен към импорта и версията на ценоразписа."""
    __tablename__ = "import_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("import_jobs.id"))
    filename: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(16))  # xlsx, xls, csv, pdf, image
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    preview_png: Mapped[Optional[bytes]] = mapped_column(LargeBinary, deferred=True)  # HEIC -> PNG
    created_at: Mapped[datetime] = _now()


class MaterialOrderRule(Base):
    """Минимална заявка, специфична за материал в рамките на версия на ценоразпис (напр. 2000 m²)."""
    __tablename__ = "material_order_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("price_list_versions.id"))
    material_code: Mapped[str] = mapped_column(ForeignKey("materials.code"))
    min_order_m2: Mapped[Decimal] = mapped_column(NUM)
    source_ref: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("version_id", "material_code"),
                      CheckConstraint("min_order_m2 > 0", name="ck_material_min_order"))


# ---------------------------------------------------------------- автентикация

class UserSession(Base):
    __tablename__ = "user_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 на token-а
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ip: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(String(300))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class ApiToken(Base):
    """Токен за машинен достъп (ERP). Пази се само hash; стойността се показва веднъж."""
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- клиенти, палети, оферти

class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    vat_number: Mapped[Optional[str]] = mapped_column(String(32))
    erp_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    internal_notes: Mapped[Optional[str]] = mapped_column(Text)  # никога в клиентския изход
    created_at: Mapped[datetime] = _now()


class PalletType(Base):
    __tablename__ = "pallet_types"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    length_mm: Mapped[int] = mapped_column(Integer)
    width_mm: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    prices: Mapped[list["PalletPrice"]] = relationship(lazy="selectin", order_by="PalletPrice.valid_from")


class PalletPrice(Base):
    """История на покупна и продажна цена; нов запис вместо презаписване."""
    __tablename__ = "pallet_prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    pallet_type_id: Mapped[int] = mapped_column(ForeignKey("pallet_types.id"))
    purchase_price: Mapped[Decimal] = mapped_column(NUM)
    sales_price: Mapped[Decimal] = mapped_column(NUM)
    currency: Mapped[str] = mapped_column(String(3))
    tax_basis: Mapped[str] = mapped_column(String(16), default="excl_vat")
    valid_from: Mapped[date] = mapped_column(Date)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    __table_args__ = (CheckConstraint("purchase_price >= 0 AND sales_price >= 0", name="ck_pallet_prices"),)


QUOTATION_STATUSES = ("draft", "final", "sent", "accepted", "rejected", "cancelled")


class Quotation(Base):
    __tablename__ = "quotations"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"))
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    customer: Mapped[Optional[Customer]] = relationship(lazy="joined")
    versions: Mapped[list["QuotationVersion"]] = relationship(lazy="selectin", order_by="QuotationVersion.version")


class QuotationVersion(Base):
    __tablename__ = "quotation_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    quotation_id: Mapped[int] = mapped_column(ForeignKey("quotations.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    currency: Mapped[str] = mapped_column(String(3))
    valid_until: Mapped[Optional[date]] = mapped_column(Date)
    delivery_terms: Mapped[Optional[str]] = mapped_column(Text)
    payment_terms: Mapped[Optional[str]] = mapped_column(Text)
    client_notes: Mapped[Optional[str]] = mapped_column(Text)
    internal_notes: Mapped[Optional[str]] = mapped_column(Text)
    based_on_version: Mapped[Optional[int]] = mapped_column(Integer)
    totals: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    finalized_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    quotation: Mapped[Quotation] = relationship(back_populates="versions")
    lines: Mapped[list["QuotationLine"]] = relationship(lazy="selectin", order_by="QuotationLine.position",
                                                       cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("quotation_id", "version"),
                      CheckConstraint(f"status IN {QUOTATION_STATUSES}", name="ck_qv_status"),
                      CheckConstraint("currency IN ('BGN','EUR')", name="ck_qv_currency"))


class QuotationLine(Base):
    __tablename__ = "quotation_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("quotation_versions.id"))
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # product | pallet
    description: Mapped[Optional[str]] = mapped_column(String(500))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_sales_price: Mapped[Optional[Decimal]] = mapped_column(NUM)
    unit_cost: Mapped[Optional[Decimal]] = mapped_column(NUM)  # вътрешно: себестойност/покупна цена
    cost_basis: Mapped[Optional[str]] = mapped_column(String(32))
    request: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    pallet_type_id: Mapped[Optional[int]] = mapped_column(ForeignKey("pallet_types.id"))
    # нормализирани ключове за търсене
    dimension_basis: Mapped[Optional[str]] = mapped_column(String(16))
    fefco: Mapped[Optional[str]] = mapped_column(String(16))
    length_mm: Mapped[Optional[int]] = mapped_column(Integer)
    width_mm: Mapped[Optional[int]] = mapped_column(Integer)
    height_mm: Mapped[Optional[int]] = mapped_column(Integer)
    material_code: Mapped[Optional[str]] = mapped_column(String(32))
    parts: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (CheckConstraint("kind IN ('product','pallet')", name="ck_ql_kind"),
                      CheckConstraint("quantity > 0", name="ck_ql_qty"),
                      Index("ix_ql_search", "fefco", "length_mm", "width_mm", "height_mm"))
