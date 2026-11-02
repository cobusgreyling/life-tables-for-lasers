"""Project a telecom laser life table onto a hot socket, then price the screen.

The hut table is not extrapolated. A warranty age that lands past the last
telecom point is unwritten. The drift limit is the highest drift among
calibration parts that survived, and lot outcomes are not used to set it.

Kelvin in the fixture uses 0°C = 273 K. Hours round half up. The Boltzmann
constant is fixed so the same inputs always print the same acceleration.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

# CODATA 2018, eV/K. Kept as a literal so the printed factor does not drift.
BOLTZMANN_EV_PER_K = 8.617333262145e-5


@dataclass(frozen=True)
class Knot:
    age_h: int
    survive_pct: int


@dataclass(frozen=True)
class Part:
    id: str
    split: str
    mttf_h: int
    drift_ppm: int
    fails: bool


@dataclass(frozen=True)
class Lot:
    program: str
    warranty_h: int
    hut_k: int
    socket_k: int
    ea_mev: int
    die_usd: int
    surgery_usd: int
    table: tuple[Knot, ...]
    parts: tuple[Part, ...]
    source: str

    @property
    def calibration(self) -> tuple[Part, ...]:
        return tuple(part for part in self.parts if part.split == "cal")

    @property
    def production(self) -> tuple[Part, ...]:
        return tuple(part for part in self.parts if part.split == "lot")


@dataclass(frozen=True)
class ProjectedKnot:
    age_hut_h: int
    survive_pct: int
    age_socket_h: int


@dataclass(frozen=True)
class Projection:
    af: float
    af_text: str
    knots: tuple[ProjectedKnot, ...]
    last_socket_h: int
    warranty_hut_h: int
    unwritten: bool
    naive_survive_pct: int | None


@dataclass(frozen=True)
class Policy:
    name: str
    die_scrap: int
    escapes: int
    false_scrap: int
    cost_usd: int


@dataclass(frozen=True)
class Decision:
    part: Part
    hut_scrap: bool
    hot_scrap: bool


@dataclass(frozen=True)
class Report:
    lot: Lot
    projection: Projection
    limit_ppm: int
    cal_survivors: int
    policies: tuple[Policy, ...]
    decisions: tuple[Decision, ...]

    def policy(self, name: str) -> Policy:
        for item in self.policies:
            if item.name == name:
                return item
        raise KeyError(name)

    @property
    def result_line(self) -> str:
        return format_result_line(self)


def load_lot(path: Path) -> Lot:
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON ({exc.msg})") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected an object")
    where = str(path)
    table = tuple(_load_knot(item, f"{where} hut_table") for item in _need_list(raw, "hut_table", where))
    if len(table) < 2:
        raise ValueError(f"{where}: hut_table needs at least two knots")
    if table[0].age_h != 0:
        raise ValueError(f"{where}: hut_table must start at age 0")
    for left, right in zip(table, table[1:]):
        if right.age_h <= left.age_h:
            raise ValueError(f"{where}: hut ages must increase")
    parts = tuple(_load_part(item, f"{where} parts") for item in _need_list(raw, "parts", where))
    if not parts:
        raise ValueError(f"{where}: parts must not be empty")
    _unique([part.id for part in parts], f"{where}: duplicate part id")
    lot = Lot(
        program=_need_str(raw, "program", where),
        warranty_h=_bounded_int(raw, "warranty_h", where, 1, 10_000_000),
        hut_k=_bounded_int(raw, "hut_k", where, 200, 500),
        socket_k=_bounded_int(raw, "socket_k", where, 200, 500),
        ea_mev=_bounded_int(raw, "ea_mev", where, 1, 5000),
        die_usd=_bounded_int(raw, "die_usd", where, 0, 1_000_000),
        surgery_usd=_bounded_int(raw, "surgery_usd", where, 0, 10_000_000),
        table=table,
        parts=parts,
        source=where,
    )
    if lot.socket_k <= lot.hut_k:
        raise ValueError(f"{where}: socket_k must be warmer than hut_k")
    if not lot.calibration or not lot.production:
        raise ValueError(f"{where}: need both cal and lot parts")
    if not any(not part.fails for part in lot.calibration):
        raise ValueError(f"{where}: calibration needs a survivor to set the drift limit")
    return lot


def acceleration_factor(ea_mev: int, hut_k: int, socket_k: int) -> float:
    ea_ev = ea_mev / 1000
    return math.exp((ea_ev / BOLTZMANN_EV_PER_K) * (1 / hut_k - 1 / socket_k))


def survival_at(table: tuple[Knot, ...], age_h: int) -> int | None:
    if age_h < table[0].age_h:
        raise ValueError("age is before the table")
    if age_h > table[-1].age_h:
        return None
    for knot in table:
        if age_h == knot.age_h:
            return knot.survive_pct
    for left, right in zip(table, table[1:]):
        if left.age_h < age_h < right.age_h:
            span = right.age_h - left.age_h
            rise = right.survive_pct - left.survive_pct
            value = left.survive_pct + (age_h - left.age_h) * rise / span
            return _hours(value)
    return None


def project(lot: Lot) -> Projection:
    af = acceleration_factor(lot.ea_mev, lot.hut_k, lot.socket_k)
    knots = tuple(
        ProjectedKnot(knot.age_h, knot.survive_pct, _hours(knot.age_h / af)) for knot in lot.table
    )
    warranty_hut_h = _hours(lot.warranty_h * af)
    return Projection(
        af=af,
        af_text=f"{af:.2f}",
        knots=knots,
        last_socket_h=knots[-1].age_socket_h,
        warranty_hut_h=warranty_hut_h,
        unwritten=warranty_hut_h > lot.table[-1].age_h,
        naive_survive_pct=survival_at(lot.table, lot.warranty_h),
    )


def drift_limit(calibration: tuple[Part, ...]) -> int:
    survivors = [part.drift_ppm for part in calibration if not part.fails]
    if not survivors:
        raise ValueError("calibration needs a survivor to set the drift limit")
    return max(survivors)


def run_lot(lot: Lot) -> Report:
    limit = drift_limit(lot.calibration)
    decisions = tuple(_decide(part, lot.warranty_h, limit) for part in lot.production)
    policies = (
        _cost("hut-table", decisions, lot, lambda item: item.hut_scrap),
        _cost("hot-screen", decisions, lot, lambda item: item.hot_scrap),
        _cost("both-gates", decisions, lot, lambda item: item.hut_scrap or item.hot_scrap),
    )
    return Report(
        lot=lot,
        projection=project(lot),
        limit_ppm=limit,
        cal_survivors=sum(1 for part in lot.calibration if not part.fails),
        policies=policies,
        decisions=decisions,
    )


def format_result_line(report: Report) -> str:
    hot = report.policy("hot-screen")
    hut = report.policy("hut-table")
    both = report.policy("both-gates")
    tail = ""
    if both.escapes == hot.escapes and both.cost_usd > hot.cost_usd and both.escapes:
        tail = " and still misses the overlap escape"
    return (
        f"hot-screen costs USD {hot.cost_usd} on {report.lot.program} "
        f"({_count(hot.escapes, 'escape')}, {_count(hot.false_scrap, 'false scrap')}); "
        f"hut-table costs USD {hut.cost_usd} "
        f"({_count(hut.escapes, 'escape')}, {_count(hut.false_scrap, 'false scrap')}); "
        f"both-gates costs USD {both.cost_usd}{tail}."
    )


def format_table(report: Report) -> str:
    lot = report.lot
    projection = report.projection
    naive = (
        f"{projection.naive_survive_pct}%"
        if projection.naive_survive_pct is not None
        else "unwritten"
    )
    past = "past the table" if projection.unwritten else "inside the table"
    lines = [
        (
            f"program {lot.program}  warranty {lot.warranty_h} h  "
            f"hut {lot.hut_k} K  socket {lot.socket_k} K  Ea {lot.ea_mev / 1000:.2f} eV"
        ),
        (
            f"AF {projection.af_text}  hut table ends {lot.table[-1].age_h} h "
            f"= {projection.last_socket_h} h at the socket "
            f"(survive {lot.table[-1].survive_pct}%)"
        ),
        (
            f"{lot.warranty_h} h at the socket needs {projection.warranty_hut_h} h of hut life, {past}"
        ),
        f"naive hut read at {lot.warranty_h} h is {naive}",
        "",
        f"{'age_hut_h':>10}  {'survive_pct':>11}  {'age_socket_h':>12}",
        f"{'-' * 10}  {'-' * 11}  {'-' * 12}",
    ]
    for knot in projection.knots:
        lines.append(f"{knot.age_hut_h:>10}  {knot.survive_pct:>11}  {knot.age_socket_h:>12}")
    lines.extend(
        [
            "",
            f"drift limit {report.limit_ppm} ppm from {report.cal_survivors} calibration survivors",
            "",
            f"{'policy':<10}  {'die_scrap':>9}  {'escapes':>7}  {'false_scrap':>11}  {'cost_usd':>8}",
            f"{'-' * 10}  {'-' * 9}  {'-' * 7}  {'-' * 11}  {'-' * 8}",
        ]
    )
    for policy in report.policies:
        lines.append(
            f"{policy.name:<10}  {policy.die_scrap:>9}  {policy.escapes:>7}  "
            f"{policy.false_scrap:>11}  {policy.cost_usd:>8}"
        )
    lines.append("")
    lines.append(report.result_line)
    return "\n".join(lines)


def format_details(report: Report) -> str:
    lines = ["lot decisions  hut is the telecom MTTF gate  hot is drift above the limit"]
    for item in report.decisions:
        part = item.part
        lines.append(
            f"  {part.id:<4}  mttf {part.mttf_h:>6}  drift {part.drift_ppm:>3}  "
            f"fails {str(part.fails).lower():<5}  "
            f"hut {'scrap' if item.hut_scrap else 'ship':<5}  "
            f"hot {'scrap' if item.hot_scrap else 'ship'}"
        )
    return "\n".join(lines)


def to_json(report: Report) -> str:
    projection = report.projection
    payload = {
        "program": report.lot.program,
        "af": projection.af_text,
        "last_socket_h": projection.last_socket_h,
        "warranty_hut_h": projection.warranty_hut_h,
        "unwritten": projection.unwritten,
        "naive_survive_pct": projection.naive_survive_pct,
        "drift_limit_ppm": report.limit_ppm,
        "policies": [
            {
                "policy": policy.name,
                "die_scrap": policy.die_scrap,
                "escapes": policy.escapes,
                "false_scrap": policy.false_scrap,
                "cost_usd": policy.cost_usd,
            }
            for policy in report.policies
        ],
        "result": report.result_line,
    }
    return json.dumps(payload, indent=2) + "\n"


def _decide(part: Part, warranty_h: int, limit_ppm: int) -> Decision:
    return Decision(
        part=part,
        hut_scrap=part.mttf_h < warranty_h,
        hot_scrap=part.drift_ppm > limit_ppm,
    )


def _cost(name: str, decisions: tuple[Decision, ...], lot: Lot, scrap_of) -> Policy:
    die_scrap = 0
    escapes = 0
    false_scrap = 0
    for item in decisions:
        scrap = scrap_of(item)
        if scrap:
            die_scrap += 1
            if not item.part.fails:
                false_scrap += 1
        elif item.part.fails:
            escapes += 1
    return Policy(
        name=name,
        die_scrap=die_scrap,
        escapes=escapes,
        false_scrap=false_scrap,
        cost_usd=die_scrap * lot.die_usd + escapes * lot.surgery_usd,
    )


def _count(n: int, singular: str) -> str:
    word = singular if n == 1 else f"{singular}s"
    return f"{n} {word}"


def _hours(value: float) -> int:
    return int(value + 0.5)


def _load_knot(raw: object, where: str) -> Knot:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected an object")
    return Knot(
        age_h=_bounded_int(raw, "age_h", where, 0, 10_000_000),
        survive_pct=_bounded_int(raw, "survive_pct", where, 0, 100),
    )


def _load_part(raw: object, where: str) -> Part:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected an object")
    part_id = _need_str(raw, "id", where)
    split = _need_str(raw, "split", f"{where} {part_id}")
    if split not in {"cal", "lot"}:
        raise ValueError(f"{where} {part_id}: split must be cal or lot")
    fails = raw.get("fails")
    if not isinstance(fails, bool):
        raise ValueError(f"{where} {part_id}: fails must be true or false")
    return Part(
        id=part_id,
        split=split,
        mttf_h=_bounded_int(raw, "mttf_h", f"{where} {part_id}", 0, 10_000_000),
        drift_ppm=_bounded_int(raw, "drift_ppm", f"{where} {part_id}", 0, 1_000_000),
        fails=fails,
    )


def _need_list(raw: dict, key: str, where: str) -> list:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{where}: {key} must be a list")
    return value


def _need_str(raw: dict, key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: {key} must be a non-empty string")
    return value


def _bounded_int(raw: dict, key: str, where: str, low: int, high: int) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}: {key} must be an integer")
    if value < low or value > high:
        raise ValueError(f"{where}: {key} must be from {low} to {high}")
    return value


def _unique(values: list[str], message: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(message)
