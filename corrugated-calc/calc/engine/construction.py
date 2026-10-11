"""Конструкции (FEFCO) и разгъв.

Формулите са версионирани и регистрирани по идентификатор; добавките (клапа и др.)
идват от конфигурацията. Неподдържан FEFCO не използва мълчаливо формулата на 0201.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Callable, Optional

from .config import BoxStyle, Config
from .models import BlankResult, BoxInput, TraceStep
from .numbers import clean, div


class UnsupportedConstruction(ValueError):
    pass


def _fefco_0201(L: int, W: int, H: int, add: dict[str, Decimal]) -> tuple[Decimal, Decimal, str]:
    flap = add["glue_flap_mm"]
    blank_length = 2 * (L + W) + flap
    blank_width = Decimal(H + W)
    return Decimal(blank_length), blank_width, "blank_length = 2 × (L + W) + glue_flap_mm; blank_width = H + W"


FORMULAS: dict[str, Callable[[int, int, int, dict[str, Decimal]], tuple[Decimal, Decimal, str]]] = {
    "FEFCO_0201": _fefco_0201,
}


def resolve_additives(style: BoxStyle, flute: Optional[str]) -> dict[str, Decimal]:
    additives = dict(style.additives)
    for override in style.additive_overrides:
        if override.flute and override.flute == flute and override.machine_mode is None:
            additives.update(override.additives)
    return additives


def compute_blank(box: BoxInput, cfg: Config, flute: Optional[str], trace: list[TraceStep],
                  defaults_used: dict[str, str]) -> BlankResult:
    fefco = box.fefco or cfg.settings.defaults["fefco"]
    if not box.fefco:
        defaults_used["fefco"] = fefco
    basis = box.dimension_basis or cfg.settings.defaults["dimension_basis"]
    if not box.dimension_basis:
        defaults_used["dimension_basis"] = basis

    L, W, H = box.length_mm, box.width_mm, box.height_mm
    volume = div(Decimal(L * W * H), Decimal(1_000_000_000))

    if box.manual_blank:
        bl, bw = box.manual_blank.length_mm, box.manual_blank.width_mm
        area = div(Decimal(bl * bw), Decimal(1_000_000))
        trace.append(TraceStep(step="blank", formula="ръчно зададен разгъв",
                               inputs={"blank_length_mm": bl, "blank_width_mm": bw}, result=str(clean(area))))
        return BlankResult(fefco=fefco, formula_version=None, formula_status="manual", source="manual",
                           blank_length_mm=bl, blank_width_mm=bw, area_m2=clean(area), volume_m3=clean(volume),
                           dimension_basis=basis, additives={})

    style = cfg.box_styles.get(fefco)
    if style is None or style.formula not in FORMULAS:
        raise UnsupportedConstruction(
            f"FEFCO {fefco} няма проверена версия на формула. Задайте ръчен разгъв или лист.")

    additives = resolve_additives(style, flute)
    bl, bw, text = FORMULAS[style.formula](L, W, H, additives)
    area = div(bl * bw, Decimal(1_000_000))
    trace.append(TraceStep(step="blank", formula=text,
                           inputs={"L": L, "W": W, "H": H, **{k: str(v) for k, v in additives.items()}},
                           result={"blank_length_mm": str(bl), "blank_width_mm": str(bw)},
                           source=f"box_styles/{fefco}@{style.formula_version} ({style.status})"))
    trace.append(TraceStep(step="area", formula="area_m2 = blank_length_mm × blank_width_mm / 1 000 000",
                           result=str(clean(area))))
    trace.append(TraceStep(step="volume", formula="volume_m3 = L × W × H / 1 000 000 000",
                           result=str(clean(volume))))
    return BlankResult(fefco=fefco, formula_version=style.formula_version, formula_status=style.status,
                       source="formula", blank_length_mm=int(bl), blank_width_mm=int(bw), area_m2=clean(area),
                       volume_m3=clean(volume), dimension_basis=basis, additives=additives)
