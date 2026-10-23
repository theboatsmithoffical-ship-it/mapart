"""Dithering algorithm registry — discovered from the engine, not hardcoded.

`AlgorithmSpec` lists supported parameters, defaults, valid ranges and the
constraint that unsupported parameter combinations raise. New algorithms
added to dither.py's kernel dict or BAYER list appear automatically here.
"""
from __future__ import annotations

from dataclasses import dataclass

from firefly.evolution.dither import BAYER_ALGORITHMS, ERROR_KERNELS, PALETTE
from . import appcore

_ERROR_DEFAULTS = {
    "floyd_steinberg": 1.0, "atkinson": 1.0, "burkes": 1.0,
    "sierra_lite": 1.0, "jjn": 1.0, "stucki": 1.0,
}


@dataclass(frozen=True)
class ParamSpec:
    name: str
    ptype: str          # "float" | "int" | "enum"
    default: object
    vrange: tuple       # (min, max) for numeric
    choices: tuple = ()


@dataclass(frozen=True)
class AlgorithmSpec:
    name: str
    family: str         # "error_diffusion" | "ordered" | "none"
    params: tuple
    note: str

    def default_params_for(self) -> dict:
        """Instance helper: defaults for this algorithm's params."""
        return {p.name: p.default for p in self.params}

    def validate(self, params: dict) -> dict:
        """Returns normalized params or raises ValueError with a useful message."""
        known = {p.name for p in self.params}
        unknown = set(params) - known
        if unknown:
            raise ValueError(f"{self.name}: unknown parameters {sorted(unknown)}; "
                             f"supported: {sorted(known)}")
        out = {}
        for p in self.params:
            v = params.get(p.name, p.default)
            if p.ptype == "float":
                f = float(v)
                if not (p.vrange[0] <= f <= p.vrange[1]):
                    raise ValueError(f"{self.name}.{p.name}={v} outside {p.vrange}")
                out[p.name] = f
            elif p.ptype == "int":
                i = int(v)
                if not (p.vrange[0] <= i <= p.vrange[1]):
                    raise ValueError(f"{self.name}.{p.name}={v} outside {p.vrange}")
                out[p.name] = i
            else:
                if v not in p.choices:
                    raise ValueError(f"{self.name}.{p.name}={v!r} not in {sorted(p.choices)}")
                out[p.name] = v
        return out


_STRENGTH = ParamSpec("strength", "float", 1.0, (0.0, 2.0))
_BIAS = ParamSpec("bias", "int", 0, (-64, 64))
_SERP = ParamSpec("serpentine", "enum", True, (0, 0), (True, False))

_SPECS = {}
for _n in ERROR_KERNELS:
    _SPECS[_n] = AlgorithmSpec(_n, "error_diffusion",
                               (_STRENGTH, _SERP),
                               "error diffusion; serpentine reverses scan direction"
                               if _n not in ("atkinson",) else "error diffusion (no serpentine flip inside kernel; still supported)")
for _n in sorted(set(BAYER_ALGORITHMS)):
    _SPECS[_n] = AlgorithmSpec(_n, "ordered",
                               (_STRENGTH, _BIAS),
                               f"ordered dithering with {_n}x{_n} Bayer matrix")
for _n in ("threshold",):
    _SPECS[_n] = AlgorithmSpec(_n, "flat", (_BIAS,),
                               "no dithering; global bias shift then quantization")
for _n in ("random",):
    _SPECS[_n] = AlgorithmSpec(_n, "stochastic",
                               (_STRENGTH, _BIAS),
                               "seeded blue-noise-ish random overlay")
for _n in ("none",):
    _SPECS[_n] = AlgorithmSpec(_n, "no-op", (),
                               "no dithering at all")


def all() -> dict:
    return dict(_SPECS)


def get(name: str) -> AlgorithmSpec:
    if name not in _SPECS:
        raise ValueError(f"unregistered algorithm {name!r}; registered: {sorted(_SPECS)}")
    return _SPECS[name]


def family(name: str) -> str:
    return get(name).family


def default_params(name: str) -> dict:
    return {p.name: p.default for p in get(name).params}



def validate(name: str, params: dict) -> dict:
    return get(name).validate(params)
