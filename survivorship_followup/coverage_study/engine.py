"""覆盖研究核心引擎。

口径要点（与 fixtures/analysis/fields.md 一致）：

* 计划区间左闭右开；``supersedes`` 仅自新计划 ``start`` 起替代旧计划。
* 退出后不再生成新机会；暂停/被替代区间也不产生机会。
* 一次计划机会无论被补交、重发多少次，至多计一次成功（responses.supersedes
  只替代同一机会的旧应答）。
* ``opportunity_at`` 与 ``received_at`` 不可互换：前者标识应随访机会，后者是
  实际回补时刻，回补延迟按二者之差计算。
* 晚到事实在 ``available_at`` 到达之后才能采用；``as_of`` 是可知信息截止时刻，
  不是业务发生时间上界。
* 未回复者没有可推断的问卷真值：缺失机会的完成概率只做区间假设，不用已回复者
  的均值冒充全体。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from survivorship_followup.coverage_study.storage import attach_study_run, find_baseline_run
from survivorship_followup.runtime import fingerprint

MISSED = "missed"          # 窗口已关闭且无有效应答
COMPLETED = "completed"    # 到期机会：窗口内（含结束当天）收到有效应答
EARLY_COMPLETED = "early_completed"  # 窗口尚未关闭但已收到有效应答
OPEN = "open"              # 机会时刻晚于 as_of，当前尚不存在
UNKNOWN = "unknown"        # 机会已到期、窗口未关闭且尚无应答，结果当前不可知


class CoverageStudyError(ValueError):
    """覆盖研究输入不完整或自相矛盾。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _parse(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise CoverageStudyError(f"{label} 必须是 ISO 8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CoverageStudyError(f"{label} 时间格式无效：{value}") from exc
    if parsed.tzinfo is None:
        raise CoverageStudyError(f"{label} 必须带时区")
    return parsed


def _iso(value: datetime) -> str:
    return value.isoformat()


def _round(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


def _ratio(numerator: int, denominator: int) -> float | None:
    """空分母返回 None（保留空白，不硬外推为零）。"""
    if denominator == 0:
        return None
    return _round(numerator / denominator)


def _percentile(values: Sequence[float], q: float) -> float | None:
    """最近秩百分位数；空序列返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(q * (len(ordered) - 1))) + 1))
    return _round(ordered[rank - 1], 2)


def _delay_stats(delays: Sequence[float]) -> dict[str, Any]:
    if not delays:
        return {"count": 0, "mean_days": None, "median_days": None,
                "p90_days": None, "max_days": None}
    return {
        "count": len(delays),
        "mean_days": _round(sum(delays) / len(delays), 2),
        "median_days": _percentile(delays, 0.5),
        "p90_days": _percentile(delays, 0.9),
        "max_days": _round(max(delays), 2),
    }


# ---------------------------------------------------------------------------
# 内部记录
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PlanSegment:
    plan_id: str
    person_id: str
    every_days: int
    start: datetime
    end: datetime
    supersedes: str | None
    available_at: datetime


@dataclass(slots=True)
class ResponseRecord:
    response_id: str
    person_id: str
    opportunity_at: datetime
    received_at: datetime
    available_at: datetime
    completed: bool
    supersedes: str | None
    adopted: bool = False


@dataclass(slots=True)
class Opportunity:
    opportunity_id: str
    person_id: str
    team: str
    cohort: str
    need: str
    plan_id: str
    every_days: int
    opportunity_at: datetime
    window_start: datetime
    window_end: datetime
    due: bool
    status: str
    response_id: str | None
    received_at: datetime | None
    recovery_delay_days: float | None
    sequence: int
    exit_at: datetime | None = None


@dataclass(slots=True)
class PersonContext:
    person_id: str
    team_at_enrollment: str
    cohort: str
    need: str
    entered_at: datetime
    exit_at: datetime | None
    segments: list[PlanSegment] = field(default_factory=list)
    opportunities: list[Opportunity] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 输入装载与 as_of 过滤
# ---------------------------------------------------------------------------

def _available(rows: Iterable[Mapping[str, Any]], as_of: datetime) -> tuple[list[dict], list[dict]]:
    """按 available_at 切分：as_of 之后才可知的事实不得采用。"""
    usable: list[dict] = []
    not_yet: list[dict] = []
    for row in rows:
        available_at = _parse(row.get("available_at"), "available_at")
        if available_at <= as_of:
            usable.append(dict(row))
        else:
            not_yet.append({"id": row.get("plan_id") or row.get("response_id")
                            or row.get("person_id") or row.get("at"),
                            "available_at": row.get("available_at")})
    return usable, not_yet


def _build_team_timeline(enrollment: Mapping[str, Any],
                         transfers: Sequence[Mapping[str, Any]]) -> list[tuple[datetime, str]]:
    """按时间排列的 (生效时刻, 团队) 序列，用于机会归属团队。"""
    timeline = [(enrollment["entered_at_dt"], enrollment["team"])]
    for transfer in sorted(transfers, key=lambda item: item["at_dt"]):
        timeline.append((transfer["at_dt"], transfer["to_team"]))
    return timeline


def _team_at(timeline: Sequence[tuple[datetime, str]], moment: datetime) -> str:
    current = timeline[0][1]
    for changed_at, team in timeline:
        if changed_at <= moment:
            current = team
        else:
            break
    return current


def _build_segments(person_id: str, plans: Sequence[Mapping[str, Any]],
                    exit_at: datetime | None, as_of: datetime) -> list[PlanSegment]:
    """合并计划替代关系，形成互不重叠、左闭右开的有效计划段。"""
    own = [row for row in plans if row["person_id"] == person_id]
    if not own:
        return []
    # 被替代的旧计划在新计划 start 处截断；区间一律左闭右开。
    supersede_start = {row["supersedes"]: row["start_dt"]
                       for row in own if row.get("supersedes")}
    segments: list[PlanSegment] = []
    for row in sorted(own, key=lambda item: item["start_dt"]):
        start = row["start_dt"]
        end = row["end_dt"]
        if row["plan_id"] in supersede_start:
            end = min(end, supersede_start[row["plan_id"]])
        if exit_at is not None:
            end = min(end, exit_at)
        # 业务截止只影响“可知性”，不截断计划段；机会生成时再按 as_of 收口。
        if start >= end:
            continue
        segments.append(PlanSegment(
            plan_id=row["plan_id"], person_id=person_id,
            every_days=int(row["every_days"]), start=start, end=end,
            supersedes=row.get("supersedes"), available_at=row["available_at_dt"],
        ))
    segments.sort(key=lambda item: (item.start, item.plan_id))
    return segments


def _generate_opportunities(person: PersonContext, window_days: int,
                            team_timeline: Sequence[tuple[datetime, str]],
                            as_of: datetime) -> list[Opportunity]:
    """按有效计划段逐段生成应完成机会；段起点即首个机会，段间不补发。

    机会时刻必须严格早于计划段结束（左闭右开）。晚于 as_of 的机会仍按计划预排
    并标 open（当前尚不存在），不得直接丢弃或预记为未完成。
    """
    opportunities: list[Opportunity] = []
    sequence = 0
    for segment in person.segments:
        moment = segment.start
        step = 0
        while moment < segment.end:
            sequence += 1
            step += 1
            window_end = moment + timedelta(days=window_days)
            opportunities.append(Opportunity(
                opportunity_id=f"{person.person_id}:opp-{sequence}",
                person_id=person.person_id,
                team=_team_at(team_timeline, moment),
                cohort=person.cohort, need=person.need,
                plan_id=segment.plan_id, every_days=segment.every_days,
                opportunity_at=moment, window_start=moment, window_end=window_end,
                due=window_end <= as_of,
                status=OPEN,
                response_id=None, received_at=None,
                recovery_delay_days=None, sequence=sequence,
                exit_at=person.exit_at,
            ))
            moment = segment.start + timedelta(days=segment.every_days * step)
    return opportunities


def _resolve_responses(person_id: str,
                       responses: Sequence[Mapping[str, Any]]) -> tuple[dict[datetime, ResponseRecord], list[ResponseRecord]]:
    """同一机会只保留最终有效应答；返回 (按机会时刻索引, 未采纳的重发/修订)。"""
    own = [row for row in responses if row["person_id"] == person_id and row.get("completed", True)]
    by_id = {row["response_id"]: row for row in own}
    # 标记被后续 supersedes 链替代的应答。
    superseded_ids: set[str] = set()
    for row in own:
        target = row.get("supersedes")
        seen: set[str] = set()
        while target and target not in seen:
            seen.add(target)
            superseded_ids.add(target)
            previous = by_id.get(target)
            target = previous.get("supersedes") if previous else None
    adopted: dict[datetime, ResponseRecord] = {}
    duplicates: list[ResponseRecord] = []
    for row in own:
        record = ResponseRecord(
            response_id=row["response_id"], person_id=person_id,
            opportunity_at=row["opportunity_at_dt"], received_at=row["received_at_dt"],
            available_at=row["available_at_dt"], completed=bool(row.get("completed", True)),
            supersedes=row.get("supersedes"),
        )
        if record.response_id in superseded_ids:
            record.adopted = False
            duplicates.append(record)
            continue
        current = adopted.get(record.opportunity_at)
        if current is None or record.available_at > current.available_at or (
            record.available_at == current.available_at and record.response_id > current.response_id
        ):
            if current is not None:
                current.adopted = False
                duplicates.append(current)
            record.adopted = True
            adopted[record.opportunity_at] = record
        else:
            record.adopted = False
            duplicates.append(record)
    return adopted, duplicates


# ---------------------------------------------------------------------------
# 研究主流程
# ---------------------------------------------------------------------------

def run_study(data: Mapping[str, Any], database: str | Any | None = None) -> dict[str, Any]:
    """执行覆盖研究并返回可 JSON 序列化的结果字典。"""
    if not isinstance(data, Mapping):
        raise CoverageStudyError("输入必须是 JSON 对象")
    as_of = _parse(data.get("as_of"), "as_of")
    study_cfg = data.get("study", {})
    reference_every_days = int(study_cfg.get("reference_every_days", 28))
    window_days = int(study_cfg.get("window_days", 28))
    dropout_range = study_cfg.get("dropout_completion_range", [0, 1])
    try:
        low_p, high_p = float(dropout_range[0]), float(dropout_range[1])
    except (TypeError, IndexError, ValueError) as exc:
        raise CoverageStudyError("dropout_completion_range 必须是两个数值") from exc
    if not 0 <= low_p <= high_p <= 1:
        raise CoverageStudyError("dropout_completion_range 必须满足 0 <= low <= high <= 1")

    enrollments, enroll_not_yet = _available(data.get("enrollments", []), as_of)
    plans, plan_not_yet = _available(data.get("plans", []), as_of)
    responses, response_not_yet = _available(data.get("responses", []), as_of)
    transfers, transfer_not_yet = _available(data.get("transfers", []), as_of)
    exits, exit_not_yet = _available(data.get("exits", []), as_of)

    for collection in (plans, responses, transfers, exits):
        for row in collection:
            for key in ("start", "end", "opportunity_at", "received_at", "at", "available_at"):
                if key in row:
                    row[f"{key}_dt"] = _parse(row[key], key)
    for row in enrollments:
        row["entered_at_dt"] = _parse(row["entered_at"], "entered_at")
        row["available_at_dt"] = _parse(row["available_at"], "available_at")

    exit_by_person = {row["person_id"]: row["at_dt"] for row in exits}
    transfers_by_person: dict[str, list[dict]] = {}
    for row in transfers:
        transfers_by_person.setdefault(row["person_id"], []).append(row)

    people: dict[str, PersonContext] = {}
    for enrollment in enrollments:
        person_id = enrollment["person_id"]
        people[person_id] = PersonContext(
            person_id=person_id, team_at_enrollment=enrollment["team"],
            cohort=enrollment["cohort"], need=enrollment["need"],
            entered_at=enrollment["entered_at_dt"],
            exit_at=exit_by_person.get(person_id),
        )

    all_opportunities: list[Opportunity] = []
    duplicate_responses: list[ResponseRecord] = []
    orphan_responses: list[dict[str, Any]] = []
    matched_response_ids: set[str] = set()
    enrolled_ids = set(people)

    # 指向未知人员的应答无法归属任何机会，单独留痕，不静默丢弃。
    for row in responses:
        if row["person_id"] not in enrolled_ids:
            orphan_responses.append({
                "response_id": row["response_id"], "person_id": row["person_id"],
                "opportunity_at": _iso(row["opportunity_at_dt"]),
                "reason": "unknown_person",
                "received_at": _iso(row["received_at_dt"]),
            })

    for person_id in sorted(people):
        person = people[person_id]
        person.segments = _build_segments(person_id, plans, person.exit_at, as_of)
        timeline = _build_team_timeline(
            {"entered_at_dt": person.entered_at, "team": person.team_at_enrollment},
            transfers_by_person.get(person_id, []),
        )
        person.opportunities = _generate_opportunities(person, window_days, timeline, as_of)
        adopted, duplicates = _resolve_responses(person_id, responses)
        duplicate_responses.extend(duplicates)
        scheduled = {opp.opportunity_at: opp for opp in person.opportunities}
        for opp in person.opportunities:
            record = adopted.get(opp.opportunity_at)
            if record is None:
                if opp.opportunity_at > as_of:
                    opp.status = OPEN  # 机会时刻未到，当前尚不存在
                elif not opp.due:
                    opp.status = UNKNOWN  # 机会已到期、窗口未关闭，结果不可知
                else:
                    opp.status = MISSED
                continue
            matched_response_ids.add(record.response_id)
            if record.received_at <= opp.window_end:
                if opp.due:
                    opp.status = COMPLETED
                else:
                    # 窗口尚未关闭但应答已到：覆盖已发生，但不计入到期率分母。
                    opp.status = EARLY_COMPLETED
                opp.response_id = record.response_id
                opp.received_at = record.received_at
                opp.recovery_delay_days = _round(
                    (record.received_at - opp.opportunity_at).total_seconds() / 86400.0, 2)
            else:
                # 应答晚于窗口：机会仍记未覆盖，但保留应答事实供修订解释。
                opp.status = UNKNOWN if not opp.due else MISSED
                orphan_responses.append({
                    "response_id": record.response_id, "person_id": person_id,
                    "opportunity_at": _iso(record.opportunity_at),
                    "reason": "received_after_window_closed",
                    "received_at": _iso(record.received_at),
                    "window_end": _iso(opp.window_end),
                })
        for moment, record in adopted.items():
            if record.response_id in matched_response_ids:
                continue
            if moment not in scheduled:
                orphan_responses.append({
                    "response_id": record.response_id, "person_id": person_id,
                    "opportunity_at": _iso(moment),
                    "reason": "opportunity_not_scheduled",
                    "received_at": _iso(record.received_at),
                })
        all_opportunities.extend(person.opportunities)

    for record in duplicate_responses:
        if record.response_id not in matched_response_ids and not any(
                item["response_id"] == record.response_id for item in orphan_responses):
            orphan_responses.append({
                "response_id": record.response_id, "person_id": record.person_id,
                "opportunity_at": _iso(record.opportunity_at),
                "reason": "superseded_response",
                "received_at": _iso(record.received_at),
            })

    coverage = _build_coverage(
        people, all_opportunities,
        known_levels={
            "team": {row["team"] for row in enrollments},
            "cohort": {row["cohort"] for row in enrollments},
            "need": {row["need"] for row in enrollments},
        })
    standardized = _build_standardized(people, all_opportunities, reference_every_days,
                                       window_days, as_of)
    sensitivity = _build_sensitivity(people, all_opportunities, low_p, high_p)
    revisions = _build_revisions(plans, responses, people, all_opportunities,
                                 transfers, exits, window_days, as_of)

    opportunity_payload = {
        "window_days": window_days,
        "as_of": _iso(as_of),
        "summary": _opportunity_summary(all_opportunities),
        "records": [_opportunity_dict(opp) for opp in
                    sorted(all_opportunities, key=lambda item: (item.person_id, item.sequence))],
        "per_person": [_person_rollup(person) for person_id, person in sorted(people.items())],
        "unmatched_responses": sorted(orphan_responses, key=lambda item: item["response_id"]),
        "facts_not_available": {
            "enrollments": enroll_not_yet, "plans": plan_not_yet,
            "responses": response_not_yet, "transfers": transfer_not_yet,
            "exits": exit_not_yet,
        },
    }

    result = {
        "study_id": "",  # 由 fingerprint 后回填，保证同输入重放得到同一 ID
        "source_versions": dict(data.get("source_versions", {})),
        "schema_version": data.get("schema_version"),
        "opportunities": opportunity_payload,
        "coverage": coverage,
        "standardized_comparisons": standardized,
        "dropout_sensitivity": sensitivity,
        "revisions": revisions,
    }
    study_id = "cov-" + fingerprint(result)[:16]
    result["study_id"] = study_id

    if database is not None:
        baseline_case_id = data.get("baseline_case_id")
        baseline = find_baseline_run(database, baseline_case_id)
        attach_study_run(database, study_id, result, baseline, baseline_case_id)
        result["persistence"] = {
            "database": str(database),
            "baseline_case_id": data.get("baseline_case_id"),
            "baseline_run_id": baseline["run_id"] if baseline else None,
        }
    return result


# ---------------------------------------------------------------------------
# 机会汇总与分组覆盖
# ---------------------------------------------------------------------------

def _opportunity_summary(opportunities: Sequence[Opportunity]) -> dict[str, Any]:
    summary = {"total": len(opportunities), "due": 0, "completed": 0,
               "missed": 0, "early_completed": 0, "open": 0, "unknown": 0}
    for opp in opportunities:
        if opp.due:
            summary["due"] += 1
        summary[opp.status] += 1
    # 恒等关系：到期机会只有 completed/missed；未到期机会为 early_completed/open/unknown。
    summary["identity_check"] = (
        summary["due"] == summary["completed"] + summary["missed"]
        and summary["total"] == summary["due"] + summary["early_completed"]
        + summary["open"] + summary["unknown"])
    return summary


def _opportunity_dict(opp: Opportunity) -> dict[str, Any]:
    return {
        "opportunity_id": opp.opportunity_id,
        "person_id": opp.person_id,
        "team": opp.team,
        "cohort": opp.cohort,
        "need": opp.need,
        "plan_id": opp.plan_id,
        "every_days": opp.every_days,
        "sequence": opp.sequence,
        "opportunity_at": _iso(opp.opportunity_at),
        "window_start": _iso(opp.window_start),
        "window_end": _iso(opp.window_end),
        "due": opp.due,
        "status": opp.status,
        "response_id": opp.response_id,
        "received_at": _iso(opp.received_at) if opp.received_at else None,
        "recovery_delay_days": opp.recovery_delay_days,
        "exit_at": _iso(opp.exit_at) if opp.exit_at else None,
    }


def _longest_miss_streak(opportunities: Sequence[Opportunity]) -> int:
    """连续中断：同一人按序排列的到期未覆盖机会最长连段。"""
    longest = current = 0
    for opp in sorted(opportunities, key=lambda item: item.sequence):
        if opp.due and opp.status == MISSED:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _person_rollup(person: PersonContext) -> dict[str, Any]:
    due = [opp for opp in person.opportunities if opp.due]
    completed = [opp for opp in due if opp.status == COMPLETED]
    delays = [opp.recovery_delay_days for opp in completed
              if opp.recovery_delay_days is not None]
    return {
        "person_id": person.person_id,
        "team_at_enrollment": person.team_at_enrollment,
        "cohort": person.cohort,
        "need": person.need,
        "entered_at": _iso(person.entered_at),
        "exit_at": _iso(person.exit_at) if person.exit_at else None,
        "opportunity_count": len(person.opportunities),
        "due_count": len(due),
        "completed_count": len(completed),
        "missed_count": sum(1 for opp in due if opp.status == MISSED),
        "open_count": sum(1 for opp in person.opportunities if opp.status == OPEN),
        "unknown_count": sum(1 for opp in person.opportunities if opp.status == UNKNOWN),
        "early_completed_count": sum(
            1 for opp in person.opportunities if opp.status == EARLY_COMPLETED),
        "coverage_rate": _ratio(len(completed), len(due)),
        "longest_miss_streak": _longest_miss_streak(person.opportunities),
        "recovery_delay": _delay_stats(delays),
        "lost_to_followup": bool(due) and not completed,
    }


def _make_group(key: str, value: str, people: Sequence[PersonContext],
                opportunities: Sequence[Opportunity]) -> dict[str, Any]:
    person_ids = {person.person_id for person in people}
    group_opps = [opp for opp in opportunities if opp.person_id in person_ids]
    due = [opp for opp in group_opps if opp.due]
    completed = [opp for opp in due if opp.status == COMPLETED]
    delays = [opp.recovery_delay_days for opp in completed
              if opp.recovery_delay_days is not None]
    lost_people = [person.person_id for person in people
                   if any(opp.due for opp in person.opportunities)
                   and not any(opp.due and opp.status == COMPLETED
                               for opp in person.opportunities)]
    streaks = [_longest_miss_streak(person.opportunities) for person in people]
    return {
        "dimension": key,
        key: value,
        "person_count": len(people),
        "opportunity_count": len(group_opps),
        "due_count": len(due),
        "completed_count": len(completed),
        "missed_count": sum(1 for opp in due if opp.status == MISSED),
        "open_count": sum(1 for opp in group_opps if opp.status == OPEN),
        "unknown_count": sum(1 for opp in group_opps if opp.status == UNKNOWN),
        "early_completed_count": sum(
            1 for opp in group_opps if opp.status == EARLY_COMPLETED),
        "coverage_rate": _ratio(len(completed), len(due)),
        "lost_to_followup_person_count": len(lost_people),
        "lost_to_followup_person_rate": _ratio(len(lost_people), len(people)),
        "max_longest_miss_streak": max(streaks, default=0),
        "recovery_delay": _delay_stats(delays),
    }


def _build_coverage(people: Mapping[str, PersonContext],
                    opportunities: Sequence[Opportunity],
                    known_levels: Mapping[str, set[str]] | None = None) -> dict[str, Any]:
    """按团队、入组届次、服务需求层及三维交叉单元给出覆盖结果。

    known_levels 提供输入中出现过的维度取值；无人取值仍输出空白单元
    （率为 null），而不是静默省略。无任何取值支撑的组合不臆造。
    """
    persons = list(people.values())
    groups: list[dict[str, Any]] = []
    for dimension, attr in (("team", "team_at_enrollment"),
                            ("cohort", "cohort"),
                            ("need", "need")):
        values = sorted({getattr(person, attr) for person in persons})
        if known_levels and dimension in known_levels:
            values = sorted(known_levels[dimension] | set(values))
        for value in values:
            members = [person for person in persons if getattr(person, attr) == value]
            groups.append(_make_group(dimension, value, members, opportunities))
    # 三维交叉单元：任一维度无样本则不输出（不硬外推）。
    cells: dict[tuple, list[PersonContext]] = {}
    for person in persons:
        cells.setdefault((person.team_at_enrollment, person.cohort, person.need), []).append(person)
    cross_cells = []
    for (team, cohort, need), members in sorted(cells.items()):
        cell = _make_group("cell", team, members, opportunities)
        cell["cell"] = {"team": team, "cohort": cohort, "need": need}
        cell.pop("team", None)
        cross_cells.append(cell)
    return {
        "rate_basis": "due_opportunities_only",
        "status_legend": {
            "completed": "到期机会，窗口内收到有效应答",
            "missed": "窗口已关闭且无有效应答，计到期未覆盖",
            "early_completed": "窗口尚未关闭但已收到有效应答，不计入到期率分母",
            "open": "机会时刻晚于 as_of，当前尚不存在",
            "unknown": "机会已到期但窗口未关闭且尚无应答，结果当前不可知",
        },
        "groups": groups,
        "cross_cells": cross_cells,
        "note": "open/unknown 机会不计入覆盖率分母；空单元保留 null，不做外推。",
    }


# ---------------------------------------------------------------------------
# 标准化比较
# ---------------------------------------------------------------------------

def _reference_opportunity_times(person: PersonContext, every_days: int,
                                 as_of: datetime) -> list[datetime]:
    """统一参考计划：自入组起按固定节律排机会，退出/as_of 后不排。"""
    stop = as_of if person.exit_at is None else min(person.exit_at, as_of)
    times: list[datetime] = []
    moment = person.entered_at
    step = 0
    while moment < stop:
        times.append(moment)
        step += 1
        moment = person.entered_at + timedelta(days=every_days * step)
    return times


def _reference_person_coverage(person: PersonContext, every_days: int,
                               window_days: int, as_of: datetime) -> dict[str, Any]:
    """同一参考节律下，用每人真实应答判定覆盖（不虚构未回复者的结果）。

    每个真实完成机会至多匹配一个参考机会：取窗口包含该机会、且时刻最近的
    到期参考机会，避免一次回补被两个参考窗口重复计为成功。
    """
    ref_times = _reference_opportunity_times(person, every_days, as_of)
    due_refs = [moment for moment in ref_times
                if moment + timedelta(days=window_days) <= as_of]
    completed_opps = [opp for opp in person.opportunities
                      if opp.status in (COMPLETED, EARLY_COMPLETED)]
    assigned: set[datetime] = set()
    for opp in completed_opps:
        candidates = [ref for ref in due_refs
                      if ref <= opp.opportunity_at
                      <= ref + timedelta(days=window_days)]
        if candidates:
            nearest = min(candidates,
                          key=lambda ref: abs((opp.opportunity_at - ref).total_seconds()))
            assigned.add(nearest)
    return {"due": len(due_refs), "completed": len(assigned),
            "scheduled_reference": len(ref_times)}


def _build_standardized(people: Mapping[str, PersonContext],
                        opportunities: Sequence[Opportunity],
                        reference_every_days: int, window_days: int,
                        as_of: datetime) -> dict[str, Any]:
    persons = list(people.values())
    per_person: dict[str, dict[str, Any]] = {}
    for person in persons:
        per_person[person.person_id] = _reference_person_coverage(
            person, reference_every_days, window_days, as_of)

    def aggregate(member_ids: Sequence[str]) -> dict[str, Any]:
        due = sum(per_person[pid]["due"] for pid in member_ids)
        completed = sum(per_person[pid]["completed"] for pid in member_ids)
        return {"reference_every_days": reference_every_days,
                "reference_window_days": window_days,
                "due_count": due, "completed_count": completed,
                "coverage_rate": _ratio(completed, due)}

    # 1) 统一参考计划：把所有人放到同一节律下，消除计划密度差异。
    reference_plan = {"reference_every_days": reference_every_days,
                      "window_days": window_days, "teams": []}
    for team in sorted({person.team_at_enrollment for person in persons}):
        member_ids = [person.person_id for person in persons
                      if person.team_at_enrollment == team]
        entry = aggregate(member_ids)
        entry["team"] = team
        entry["person_count"] = len(member_ids)
        reference_plan["teams"].append(entry)
    overall = aggregate([person.person_id for person in persons])
    reference_plan["overall"] = overall

    # 2) 参考人群直接标准化：以全队入组合计为标准人群，按 need 层加权各团队层率。
    strata = sorted({person.need for person in persons})
    standard_weights: dict[str, float] = {}
    for need in strata:
        standard_weights[need] = len([person for person in persons if person.need == need]) / len(persons)
    team_rates: dict[str, dict[str, float | None]] = {}
    for team in sorted({person.team_at_enrollment for person in persons}):
        team_rates[team] = {}
        for need in strata:
            member_ids = [person.person_id for person in persons
                          if person.team_at_enrollment == team and person.need == need]
            if not member_ids:
                team_rates[team][need] = None  # 无支撑保留空白
                continue
            team_rates[team][need] = aggregate(member_ids)["coverage_rate"]

    standardized_rates: dict[str, Any] = {}
    for team, rates in team_rates.items():
        terms: list[dict[str, Any]] = []
        supported = True
        for need in strata:
            rate = rates[need]
            if rate is None:
                supported = False
                terms.append({"need": need, "weight": _round(standard_weights[need]),
                              "stratum_rate": None})
            else:
                terms.append({"need": need, "weight": _round(standard_weights[need]),
                              "stratum_rate": rate})
        standardized_rates[team] = {
            "standard_population": "all_enrolled_persons",
            "stratum_terms": terms,
            "standardized_coverage_rate": (
                _round(sum(term["weight"] * term["stratum_rate"] for term in terms), 4)
                if supported else None),
            "unsupported_strata": [term["need"] for term in terms if term["stratum_rate"] is None],
        }

    # 3) 原始团队率并列展示，便于看出计划密度造成的表观差异。
    raw_rates = []
    for team in sorted({person.team_at_enrollment for person in persons}):
        member_ids = {person.person_id for person in persons
                      if person.team_at_enrollment == team}
        due = [opp for opp in opportunities if opp.due and opp.person_id in member_ids]
        completed = [opp for opp in due if opp.status == COMPLETED]
        raw_rates.append({"team": team, "due_count": len(due),
                          "completed_count": len(completed),
                          "coverage_rate": _ratio(len(completed), len(due))})
    return {
        "reference_plan": reference_plan,
        "reference_population": {
            "method": "direct_standardization",
            "standard_weights": {key: _round(value) for key, value in sorted(standard_weights.items())},
            "team_rates": standardized_rates,
        },
        "raw_team_rates": raw_rates,
        "note": "标准化只调整服务节律与人群构成，不推断未回复问卷内容；缺支撑层返回 null。",
    }


# ---------------------------------------------------------------------------
# 失访敏感性分析
# ---------------------------------------------------------------------------

def _build_sensitivity(people: Mapping[str, PersonContext],
                       opportunities: Sequence[Opportunity],
                       low_p: float, high_p: float) -> dict[str, Any]:
    """对到期未覆盖机会施加完成概率区间假设，给出界值、情景网格与结论翻转点。"""
    persons = list(people.values())
    grid = [_round(low_p + (high_p - low_p) * index / 4, 4) for index in range(5)]
    teams = sorted({person.team_at_enrollment for person in persons})

    person_rows = []
    for person in sorted(persons, key=lambda item: item.person_id):
        due = [opp for opp in person.opportunities if opp.due]
        completed = sum(1 for opp in due if opp.status == COMPLETED)
        missing = sum(1 for opp in due if opp.status == MISSED)
        row = {
            "person_id": person.person_id,
            "team": person.team_at_enrollment,
            "need": person.need,
            "due_count": len(due),
            "observed_completed": completed,
            "missing_count": missing,
            "observed_rate": _ratio(completed, len(due)),
            "scenario_rates": {},
        }
        for p in grid:
            if not due:
                row["scenario_rates"][str(p)] = None
            else:
                row["scenario_rates"][str(p)] = _round(
                    (completed + missing * p) / len(due), 4)
        row["bounds"] = {
            "worst_case": row["scenario_rates"][str(grid[0])],
            "best_case": row["scenario_rates"][str(grid[-1])],
        }
        person_rows.append(row)

    team_scenarios: dict[str, dict[str, float | None]] = {team: {} for team in teams}
    for p in grid:
        for team in teams:
            member_ids = {person.person_id for person in persons
                          if person.team_at_enrollment == team}
            due = [opp for opp in opportunities if opp.due and opp.person_id in member_ids]
            completed = sum(1 for opp in due if opp.status == COMPLETED)
            missing = sum(1 for opp in due if opp.status == MISSED)
            team_scenarios[team][str(p)] = (
                None if not due else _round((completed + missing * p) / len(due), 4))

    lost_people = [row["person_id"] for row in person_rows
                   if row["due_count"] > 0 and row["observed_completed"] == 0]
    tipping = _tipping_points(teams, team_scenarios, grid)
    return {
        "assumption": "missing_opportunity_completion_probability",
        "probability_grid": grid,
        "interpretation": {
            "worst_case": "所有失访机会均未完成（概率下界）",
            "best_case": "所有失访机会均完成（概率上界）",
            "observed": "仅计入已观测应答，缺失机会计为未完成；不代表失访者真值。",
        },
        "team_scenarios": team_scenarios,
        "person_scenarios": person_rows,
        "lost_to_followup_persons": lost_people,
        "tipping_points": tipping,
        "note": "区间宽度直接反映失访带来的不确定性；网格内团队排序翻转点之外的结论才稳健。",
    }


def _tipping_points(teams: Sequence[str],
                    team_scenarios: Mapping[str, Mapping[str, float | None]],
                    grid: Sequence[float]) -> list[dict[str, Any]]:
    """两两团队比较：列出排序随失访假设改变的翻转区间。"""
    points: list[dict[str, Any]] = []
    for index, team_a in enumerate(teams):
        for team_b in teams[index + 1:]:
            order_before = None
            flips: list[dict[str, Any]] = []
            for p in grid:
                rate_a = team_scenarios[team_a][str(p)]
                rate_b = team_scenarios[team_b][str(p)]
                if rate_a is None or rate_b is None:
                    order = None
                elif rate_a > rate_b:
                    order = "a_higher"
                elif rate_a < rate_b:
                    order = "b_higher"
                else:
                    order = "tie"
                if order_before is not None and order is not None and order != order_before:
                    flips.append({"between_probability": p,
                                  "from": order_before, "to": order})
                if order is not None:
                    order_before = order
            points.append({"team_a": team_a, "team_b": team_b,
                           "order_changes": flips,
                           "stable_ordering": not flips})
    return points


# ---------------------------------------------------------------------------
# 修订与运营事件解释
# ---------------------------------------------------------------------------

def _build_revisions(plans: Sequence[Mapping[str, Any]],
                     responses: Sequence[Mapping[str, Any]],
                     people: Mapping[str, PersonContext],
                     opportunities: Sequence[Opportunity],
                     transfers: Sequence[Mapping[str, Any]],
                     exits: Sequence[Mapping[str, Any]],
                     window_days: int, as_of: datetime) -> dict[str, Any]:
    plan_revisions = []
    for row in sorted(plans, key=lambda item: item["plan_id"]):
        if not row.get("supersedes"):
            continue
        person = people.get(row["person_id"])
        if person is None:
            continue
        old = next((item for item in plans if item["plan_id"] == row["supersedes"]), None)
        if old is None:
            continue
        change_start = row["start_dt"]
        stop = as_of if person.exit_at is None else min(person.exit_at, as_of)
        old_times = []
        moment = change_start
        step = 0
        while moment < stop:
            old_times.append(moment)
            step += 1
            moment = change_start + timedelta(days=int(old["every_days"]) * step)
        new_times = [opp.opportunity_at for opp in person.opportunities
                     if opp.plan_id == row["plan_id"]]
        plan_revisions.append({
            "plan_id": row["plan_id"],
            "supersedes": row["supersedes"],
            "person_id": row["person_id"],
            "effective_at": _iso(change_start),
            "every_days_before": int(old["every_days"]),
            "every_days_after": int(row["every_days"]),
            "opportunities_under_old_rhythm": len(old_times),
            "opportunities_under_new_rhythm": len(new_times),
            "opportunity_difference": len(new_times) - len(old_times),
            "note": "替代仅自新计划 start 生效；替代前旧计划机会保持不变。",
        })

    response_revisions = []
    by_id = {row["response_id"]: row for row in responses}
    for row in sorted(responses, key=lambda item: item["response_id"]):
        if not row.get("supersedes"):
            continue
        previous = by_id.get(row["supersedes"])
        response_revisions.append({
            "response_id": row["response_id"],
            "supersedes": row["supersedes"],
            "person_id": row["person_id"],
            "opportunity_at": row["opportunity_at"],
            "original_received_at": previous["received_at"] if previous else None,
            "revised_received_at": row["received_at"],
            "original_available_at": previous["available_at"] if previous else None,
            "revised_available_at": row["available_at"],
            "counted_as_completions": 1,
            "note": "同一机会的修订应答只计一次成功，重发不制造额外覆盖。",
        })

    transfer_payload = [{
        "person_id": row["person_id"],
        "from_team": row["from_team"], "to_team": row["to_team"],
        "at": row["at"], "effective_available_at": row["available_at"],
    } for row in sorted(transfers, key=lambda item: item["at_dt"])]

    exit_payload = [{
        "person_id": row["person_id"], "at": row["at"],
        "reason": row.get("reason"),
        "available_at": row["available_at"],
        "opportunities_after_exit": sum(
            1 for opp in opportunities
            if opp.person_id == row["person_id"] and opp.opportunity_at >= row["at_dt"]),
    } for row in sorted(exits, key=lambda item: item["at_dt"])]

    return {
        "plan_revisions": plan_revisions,
        "response_revisions": response_revisions,
        "transfers": transfer_payload,
        "exits": exit_payload,
        "merge_rule": "responses.supersedes 替代同一 opportunity_at 的旧应答；计划 supersedes 自 start 起截断旧计划。",
    }
