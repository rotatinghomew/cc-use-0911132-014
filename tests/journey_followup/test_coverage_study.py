"""覆盖研究方法正确性测试。

覆盖业务口径中的关键反例：
* 计划降频（supersedes）不制造额外机会，新旧节律差异可分别解释；
* 一人跨团队（transfer）后机会归属新团队；
* 提前退出（exit）后不再排机会，迟到应答不充当未完成也不补算覆盖；
* 应答修订/重发（responses.supersedes）同一机会只计一次成功；
* 迟到回补的延迟按 received_at - opportunity_at 计算；
* as_of 之后才 available_at 的事实不得采用；
* 失访者问卷真值不可推断，只给概率区间，不用应答者均值冒充；
* 无支撑人群保留 null，不硬外推；
* 同输入重放得到同一 study_id，并与既有产品台账衔接。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from survivorship_followup.coverage_study.engine import run_study
from survivorship_followup.coverage_study.storage import load_study_run
from survivorship_followup.service import ApplicationService

UTC = timezone.utc
AS_OF = datetime(2026, 10, 30, tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat()


class StudyBuilder:
    """构造最小合成输入；默认两名团队成员、两种需求层。"""

    def __init__(self) -> None:
        self.as_of = AS_OF
        self.enrollments: list[dict] = []
        self.plans: list[dict] = []
        self.responses: list[dict] = []
        self.transfers: list[dict] = []
        self.exits: list[dict] = []

    def enroll(self, person_id: str, team: str, need: str = "high",
               entered_at: datetime | None = None) -> datetime:
        entered_at = entered_at or datetime(2026, 8, 1, tzinfo=UTC)
        self.enrollments.append({
            "person_id": person_id, "entered_at": iso(entered_at),
            "team": team, "need": need, "cohort": "august",
            "available_at": iso(entered_at),
        })
        return entered_at

    def plan(self, plan_id: str, person_id: str, every_days: int,
             start: datetime, end: datetime | None = None,
             supersedes: str | None = None,
             available_at: datetime | None = None) -> None:
        row = {
            "plan_id": plan_id, "person_id": person_id,
            "every_days": every_days, "start": iso(start),
            "end": iso(end or self.as_of), "available_at": iso(available_at or start),
        }
        if supersedes:
            row["supersedes"] = supersedes
        self.plans.append(row)

    def response(self, response_id: str, person_id: str,
                 opportunity_at: datetime, received_at: datetime,
                 available_at: datetime | None = None,
                 supersedes: str | None = None) -> None:
        row = {
            "response_id": response_id, "person_id": person_id,
            "opportunity_at": iso(opportunity_at), "received_at": iso(received_at),
            "available_at": iso(available_at or received_at), "completed": True,
        }
        if supersedes:
            row["supersedes"] = supersedes
        self.responses.append(row)

    def transfer(self, person_id: str, at: datetime,
                 from_team: str, to_team: str,
                 available_at: datetime | None = None) -> None:
        self.transfers.append({
            "person_id": person_id, "from_team": from_team,
            "to_team": to_team, "at": iso(at),
            "available_at": iso(available_at or at),
        })

    def exit(self, person_id: str, at: datetime,
             available_at: datetime | None = None) -> None:
        self.exits.append({
            "person_id": person_id, "at": iso(at),
            "available_at": iso(available_at or at), "reason": "withdrawal",
        })

    def build(self, reference_every_days: int = 28,
              window_days: int = 28,
              dropout_range=(0, 1)) -> dict:
        return {
            "schema_version": 1,
            "as_of": iso(self.as_of),
            "source_versions": {"operations": "test", "reference": "test"},
            "enrollments": self.enrollments,
            "plans": self.plans,
            "responses": self.responses,
            "transfers": self.transfers,
            "exits": self.exits,
            "study": {
                "reference_every_days": reference_every_days,
                "window_days": window_days,
                "dropout_completion_range": list(dropout_range),
            },
            "baseline_case_id": "demo-001",
        }


def records_for(result: dict, person_id: str) -> list[dict]:
    return [row for row in result["opportunities"]["records"]
            if row["person_id"] == person_id]


class OpportunityReconstructionTests(unittest.TestCase):
    def test_plan_supersession_reduces_opportunities_without_extra_coverage(self):
        builder = StudyBuilder()
        start = builder.enroll("p1", "intensive")
        builder.plan("plan-a", "p1", 7, start)
        # 9 月 10 日起降频为 28 天。
        change = datetime(2026, 9, 10, tzinfo=UTC)
        builder.plan("plan-b", "p1", 28, change, supersedes="plan-a")
        result = run_study(builder.build())
        rows = records_for(result, "p1")
        old_rows = [row for row in rows if row["plan_id"] == "plan-a"]
        new_rows = [row for row in rows if row["plan_id"] == "plan-b"]
        self.assertEqual([row["opportunity_at"] for row in old_rows],
                         [iso(start + timedelta(days=7 * k)) for k in range(6)])
        self.assertEqual([row["opportunity_at"] for row in new_rows],
                         [iso(change), iso(change + timedelta(days=28))])
        # 机会时间点互不重叠，总数 = 旧段 6 + 新段 2。
        times = [row["opportunity_at"] for row in rows]
        self.assertEqual(len(times), len(set(times)))
        revision = result["revisions"]["plan_revisions"][0]
        self.assertEqual(revision["opportunity_difference"], 2 - 8)

    def test_transfer_changes_team_attribution_at_transfer_moment(self):
        builder = StudyBuilder()
        start = builder.enroll("p1", "intensive")
        builder.plan("plan-a", "p1", 7, start)
        builder.transfer("p1", datetime(2026, 9, 12, tzinfo=UTC),
                         "intensive", "monthly")
        result = run_study(builder.build())
        rows = records_for(result, "p1")
        before = [row for row in rows
                  if row["opportunity_at"] < "2026-09-12T00:00:00+00:00"]
        after = [row for row in rows
                 if row["opportunity_at"] >= "2026-09-12T00:00:00+00:00"]
        self.assertTrue(before)
        self.assertTrue(after)
        self.assertTrue(all(row["team"] == "intensive" for row in before))
        self.assertTrue(all(row["team"] == "monthly" for row in after))

    def test_exit_stops_opportunities_and_late_responses_do_not_count(self):
        builder = StudyBuilder()
        start = builder.enroll("p8", "monthly")
        # 计划本覆盖到 as_of，但退出时刻截断有效计划段。
        builder.plan("plan-8", "p8", 28, start, end=builder.as_of)
        exit_at = datetime(2026, 9, 5, tzinfo=UTC)
        builder.exit("p8", exit_at)
        # 退出后的“机会”收到应答，但这些机会本就不应存在。
        builder.response("r8-1", "p8", start + timedelta(days=56),
                         datetime(2026, 9, 26, tzinfo=UTC))
        builder.response("r8-2", "p8", start + timedelta(days=84),
                         datetime(2026, 10, 24, tzinfo=UTC))
        result = run_study(builder.build())
        rows = records_for(result, "p8")
        # 8/1 与 8/29 机会都早于 9/5 退出时刻，仍然存在；9/26 及之后不再排机会。
        self.assertEqual([row["opportunity_at"] for row in rows],
                         [iso(start), iso(start + timedelta(days=28))])
        self.assertTrue(all(row["opportunity_at"] < iso(exit_at) for row in rows))
        self.assertFalse(any(
            row["opportunity_at"] >= iso(exit_at) for row in rows))
        orphan_ids = {row["response_id"]
                      for row in result["opportunities"]["unmatched_responses"]}
        self.assertEqual(orphan_ids, {"r8-1", "r8-2"})
        self.assertEqual(result["revisions"]["exits"][0]["opportunities_after_exit"], 0)
        person = next(row for row in result["opportunities"]["per_person"]
                      if row["person_id"] == "p8")
        self.assertTrue(person["lost_to_followup"])

    def test_revised_response_counts_single_completion_with_late_delay(self):
        builder = StudyBuilder()
        start = builder.enroll("p1", "intensive")
        builder.plan("plan-a", "p1", 7, start)
        first_opp = start + timedelta(days=7)
        builder.response("r1", "p1", first_opp, first_opp + timedelta(days=2))
        builder.response("r1-revised", "p1", first_opp,
                         first_opp + timedelta(days=12),
                         available_at=first_opp + timedelta(days=13),
                         supersedes="r1")
        result = run_study(builder.build())
        target = next(row for row in records_for(result, "p1")
                      if row["opportunity_at"] == iso(first_opp))
        self.assertEqual(target["status"], "completed")
        self.assertEqual(target["response_id"], "r1-revised")
        # 延迟按修订后的 received_at 计算，且只有一次成功。
        self.assertEqual(target["recovery_delay_days"], 12.0)
        completed = sum(1 for row in records_for(result, "p1")
                        if row["status"] == "completed")
        self.assertEqual(completed, 1)
        orphan = next(row for row in result["opportunities"]["unmatched_responses"]
                      if row["response_id"] == "r1")
        self.assertEqual(orphan["reason"], "superseded_response")
        self.assertEqual(result["revisions"]["response_revisions"][0]["counted_as_completions"], 1)

    def test_open_and_unknown_opportunities_excluded_from_coverage_denominator(self):
        builder = StudyBuilder()
        start = builder.enroll("p1", "monthly")
        builder.plan("plan-a", "p1", 28, start)
        result = run_study(builder.build())
        rows = records_for(result, "p1")
        due_rows = [row for row in rows if row["due"]]
        # 8/1、8/29、9/26 窗口已关闭（missed）；10/24 窗口未关闭（unknown）。
        unknown_rows = [row for row in rows if row["status"] == "unknown"]
        self.assertTrue(unknown_rows)
        self.assertFalse(any(row["status"] == "open" for row in rows))
        group = next(g for g in result["coverage"]["groups"]
                     if g.get("team") == "monthly")
        self.assertEqual(group["due_count"], len(due_rows))
        self.assertEqual(group["unknown_count"], len(unknown_rows))
        self.assertTrue(result["opportunities"]["summary"]["identity_check"])

    def test_opportunity_beyond_as_of_is_open_not_precounted_as_missed(self):
        builder = StudyBuilder()
        # 入组 10/29、7 天节律：10/29 机会窗口未关闭（unknown）；11/05 机会晚于
        # as_of，仍应预排为 open，而不是当作未完成。
        start = builder.enroll("p1", "monthly",
                               entered_at=datetime(2026, 10, 29, tzinfo=UTC))
        # 计划 end 晚于 as_of：11/05 机会时刻在 as_of 之后，应预排为 open。
        builder.plan("plan-a", "p1", 7, start,
                     end=datetime(2026, 11, 30, tzinfo=UTC))
        result = run_study(builder.build())
        rows = records_for(result, "p1")
        statuses = {row["opportunity_at"]: row["status"] for row in rows}
        self.assertEqual(statuses[iso(start)], "unknown")
        self.assertEqual(statuses[iso(start + timedelta(days=7))], "open")
        group = next(g for g in result["coverage"]["groups"]
                     if g.get("team") == "monthly")
        self.assertEqual(group["due_count"], 0)
        self.assertIsNone(group["coverage_rate"])


class AvailabilityAndUnknownTests(unittest.TestCase):
    def test_facts_not_available_by_as_of_are_excluded(self):
        builder = StudyBuilder()
        start = builder.enroll("p1", "monthly")
        builder.plan("plan-a", "p1", 28, start)
        future = AS_OF + timedelta(days=2)
        builder.response("late", "p1", start, future, available_at=future)
        result = run_study(builder.build())
        held = result["opportunities"]["facts_not_available"]["responses"]
        self.assertEqual([row["id"] for row in held], ["late"])
        target = next(row for row in result["opportunities"]["records"]
                      if row["opportunity_at"] == iso(start))
        self.assertEqual(target["status"], "missed")

    def test_late_available_plan_revision_does_not_truncate_old_plan(self):
        builder = StudyBuilder()
        start = builder.enroll("p1", "monthly")
        builder.plan("plan-old", "p1", 28, start)
        change = datetime(2026, 10, 1, tzinfo=UTC)
        # 新计划要到 as_of 之后才可知，当前分析不得采用，旧节律继续生效。
        builder.plan("plan-new", "p1", 7, change, supersedes="plan-old",
                     available_at=AS_OF + timedelta(days=16))
        result = run_study(builder.build())
        held = result["opportunities"]["facts_not_available"]["plans"]
        self.assertEqual([row["id"] for row in held], ["plan-new"])
        rows = records_for(result, "p1")
        self.assertTrue(all(row["plan_id"] == "plan-old" for row in rows))
        self.assertNotIn(
            "plan-new",
            [rev["plan_id"] for rev in result["revisions"]["plan_revisions"]])

    def test_response_on_window_closing_day_is_covered(self):
        builder = StudyBuilder()
        start = builder.enroll("p1", "monthly")
        builder.plan("plan-a", "p1", 28, start)
        # 第 28 天恰好窗口关闭，按闭区间计入覆盖。
        builder.response("r1", "p1", start, start + timedelta(days=28))
        result = run_study(builder.build())
        target = records_for(result, "p1")[0]
        self.assertEqual(target["status"], "completed")
        self.assertEqual(target["recovery_delay_days"], 28.0)

    def test_response_for_unknown_person_is_audited_not_dropped(self) -> None:
        builder = StudyBuilder()
        start = builder.enroll("p1", "monthly")
        builder.plan("plan-a", "p1", 28, start)
        builder.response("ghost-r", "ghost", start, start + timedelta(days=1))
        result = run_study(builder.build())
        orphan = next(row for row in result["opportunities"]["unmatched_responses"]
                      if row["response_id"] == "ghost-r")
        self.assertEqual(orphan["reason"], "unknown_person")

    def test_invalid_dropout_range_rejected(self) -> None:
        builder = StudyBuilder()
        start = builder.enroll("p1", "monthly")
        builder.plan("plan-a", "p1", 28, start)
        with self.assertRaises(ValueError):
            run_study(builder.build(dropout_range=(0.8, 0.2)))

    def test_missing_timezone_rejected(self) -> None:
        builder = StudyBuilder()
        builder.enroll("p1", "monthly")
        data = builder.build()
        data["as_of"] = "2026-10-30T00:00:00"
        with self.assertRaises(ValueError):
            run_study(data)

    def test_late_response_after_window_kept_but_not_coverage(self):
        builder = StudyBuilder()
        start = builder.enroll("p1", "monthly")
        builder.plan("plan-a", "p1", 28, start,
                     end=start + timedelta(days=28))
        # 窗口关闭后才回补。
        builder.response("slow", "p1", start, start + timedelta(days=35))
        result = run_study(builder.build())
        target = records_for(result, "p1")[0]
        self.assertEqual(target["status"], "missed")
        self.assertIsNone(target["response_id"])
        orphan = next(row for row in result["opportunities"]["unmatched_responses"]
                      if row["response_id"] == "slow")
        self.assertEqual(orphan["reason"], "received_after_window_closed")


class StandardizationTests(unittest.TestCase):
    def test_reference_plan_equalizes_rhythm_and_keeps_real_responses(self):
        builder = StudyBuilder()
        weekly_start = builder.enroll("w", "intensive", need="high")
        monthly_start = builder.enroll("m", "monthly", need="high")
        builder.plan("pw", "w", 7, weekly_start)
        builder.plan("pm", "m", 28, monthly_start)
        # 周频团队只完成首个参考机会；月频团队同样完成首个。
        builder.response("rw", "w", weekly_start, weekly_start + timedelta(days=2))
        builder.response("rm", "m", monthly_start, monthly_start + timedelta(days=2))
        result = run_study(builder.build(reference_every_days=28))
        ref = result["standardized_comparisons"]["reference_plan"]
        per_team = {row["team"]: row for row in ref["teams"]}
        self.assertEqual(per_team["intensive"]["due_count"],
                         per_team["monthly"]["due_count"])
        self.assertEqual(per_team["intensive"]["completed_count"],
                         per_team["monthly"]["completed_count"])

    def test_reference_plan_never_double_counts_one_completion(self):
        # 周频计划下两个相邻完成，在 28 天参考节律中只能各匹配一个参考窗口，
        # 不允许同一次回补被多个参考窗口重复计数。
        builder = StudyBuilder()
        start = builder.enroll("w", "intensive", need="high")
        builder.plan("pw", "w", 7, start)
        builder.response("r1", "w", start, start + timedelta(days=2))
        builder.response("r2", "w", start + timedelta(days=7),
                         start + timedelta(days=9))
        result = run_study(builder.build(reference_every_days=28, window_days=28))
        ref = result["standardized_comparisons"]["reference_plan"]
        intensive = next(row for row in ref["teams"] if row["team"] == "intensive")
        self.assertLessEqual(intensive["completed_count"], intensive["due_count"])
        # 首个参考窗口最多记一次成功。
        self.assertEqual(intensive["completed_count"], 1)

    def test_unsupported_stratum_returns_null_instead_of_extrapolation(self):
        builder = StudyBuilder()
        # intensive 只有 high 层；monthly 只有 ordinary 层，互相缺支撑。
        builder.enroll("w1", "intensive", need="high")
        builder.enroll("m1", "monthly", need="ordinary")
        builder.plan("pw1", "w1", 28, datetime(2026, 8, 1, tzinfo=UTC))
        builder.plan("pm1", "m1", 28, datetime(2026, 8, 1, tzinfo=UTC))
        result = run_study(builder.build())
        rates = result["standardized_comparisons"]["reference_population"]["team_rates"]
        self.assertIsNone(rates["intensive"]["standardized_coverage_rate"])
        self.assertIsNone(rates["monthly"]["standardized_coverage_rate"])
        self.assertIn("ordinary", rates["intensive"]["unsupported_strata"])
        self.assertIn("high", rates["monthly"]["unsupported_strata"])
        # 有真实样本的层率仍然给出，不整体置空。
        intensive_terms = {t["need"]: t["stratum_rate"]
                           for t in rates["intensive"]["stratum_terms"]}
        self.assertIsNotNone(intensive_terms["high"])
        self.assertIsNone(intensive_terms["ordinary"])

    def test_empty_group_rates_are_null_not_zero(self):
        builder = StudyBuilder()
        builder.enroll("m1", "monthly", need="high")
        builder.plan("pm1", "m1", 28, datetime(2026, 8, 1, tzinfo=UTC))
        # intensive 团队有人入组但入组当天即退出，没有任何机会，属于“已知但空”组。
        builder.enroll("w1", "intensive", need="high",
                       entered_at=datetime(2026, 8, 2, tzinfo=UTC))
        builder.plan("pw1", "w1", 28, datetime(2026, 8, 2, tzinfo=UTC))
        builder.exit("w1", datetime(2026, 8, 2, tzinfo=UTC))
        result = run_study(builder.build())
        groups = result["coverage"]["groups"]
        intensive = next(g for g in groups if g.get("team") == "intensive")
        self.assertIsNone(intensive["coverage_rate"])
        self.assertEqual(intensive["person_count"], 1)
        self.assertEqual(intensive["opportunity_count"], 0)


class DropoutSensitivityTests(unittest.TestCase):
    def test_bounds_span_full_range_for_persistent_nonresponders(self):
        builder = StudyBuilder()
        start = builder.enroll("p0", "monthly", need="ordinary")
        builder.plan("plan-a", "p0", 28, start)
        result = run_study(builder.build())
        row = next(r for r in result["dropout_sensitivity"]["person_scenarios"]
                   if r["person_id"] == "p0")
        self.assertEqual(row["observed_completed"], 0)
        self.assertGreater(row["missing_count"], 0)
        self.assertEqual(row["bounds"]["worst_case"], 0.0)
        self.assertEqual(row["bounds"]["best_case"], 1.0)
        # 观测率绝不能拿应答者均值填充。
        self.assertEqual(row["observed_rate"], 0.0)

    def test_team_rates_interpolate_between_worst_and_best_case(self):
        builder = StudyBuilder()
        start = builder.enroll("p0", "monthly")
        builder.plan("plan-a", "p0", 28, start)
        result = run_study(builder.build(dropout_range=(0.2, 0.8)))
        scenarios = result["dropout_sensitivity"]
        self.assertEqual(scenarios["probability_grid"], [0.2, 0.35, 0.5, 0.65, 0.8])
        team_grid = scenarios["team_scenarios"]["monthly"]
        rates = [team_grid[str(p)] for p in scenarios["probability_grid"]]
        self.assertEqual(rates, sorted(rates))

    def test_tipping_point_flags_ordering_change(self):
        builder = StudyBuilder()
        # 月频团队观测率更高，但失访机会更少；在最乐观假设下两团队趋同。
        w_start = builder.enroll("w", "intensive", need="high")
        m_start = builder.enroll("m", "monthly", need="high")
        builder.plan("pw", "w", 7, w_start)
        builder.plan("pm", "m", 28, m_start)
        builder.response("rm", "m", m_start, m_start + timedelta(days=2))
        result = run_study(builder.build())
        tipping = result["dropout_sensitivity"]["tipping_points"][0]
        self.assertEqual(tipping["team_a"], "intensive")
        self.assertEqual(tipping["team_b"], "monthly")
        self.assertFalse(tipping["stable_ordering"])
        self.assertTrue(any(change["to"] == "tie" for change in tipping["order_changes"]))


class PersistenceAndReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.sqlite3"
        service = ApplicationService(self.db)
        service.execute({"case_id": "demo-001"}, request_key="baseline-key")
        self.builder = StudyBuilder()
        start = self.builder.enroll("p1", "monthly")
        self.builder.plan("plan-a", "p1", 28, start)

    def tearDown(self) -> None:
        self.temp.cleanup()
    def test_replay_uses_same_study_id_and_persists_link(self) -> None:
        data = self.builder.build()
        first = run_study(data, database=self.db)
        second = run_study(data, database=self.db)
        self.assertEqual(first["study_id"], second["study_id"])
        self.assertEqual(first["persistence"]["baseline_case_id"], "demo-001")
        self.assertIsNotNone(first["persistence"]["baseline_run_id"])
        loaded = load_study_run(self.db, first["study_id"])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["study_id"], first["study_id"])

    def test_runs_without_database_omit_persistence(self) -> None:
        result = run_study(self.builder.build())
        self.assertNotIn("persistence", result)
        self.assertTrue(result["study_id"].startswith("cov-"))

    def test_output_is_json_serializable_and_complete(self) -> None:
        required = ["study_id", "opportunities", "coverage",
                    "standardized_comparisons", "dropout_sensitivity", "revisions"]
        result = run_study(self.builder.build(), database=self.db)
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
        decoded = json.loads(encoded)
        for field in required:
            self.assertIn(field, decoded)

    def test_cli_writes_output_and_links_database(self) -> None:
        from survivorship_followup.coverage_study.__main__ import main

        input_path = Path(self.temp.name) / "input.json"
        output_path = Path(self.temp.name) / "coverage.json"
        input_path.write_text(json.dumps(self.builder.build()), encoding="utf-8")
        exit_code = main(["--input", str(input_path),
                          "--database", str(self.db),
                          "--output", str(output_path)])
        self.assertEqual(exit_code, 0)
        written = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertTrue(written["study_id"].startswith("cov-"))
        self.assertEqual(written["persistence"]["baseline_case_id"], "demo-001")

    def test_cli_missing_input_returns_error_code(self) -> None:
        from survivorship_followup.coverage_study.__main__ import main

        exit_code = main(["--input", str(Path(self.temp.name) / "missing.json"),
                          "--database", str(self.db),
                          "--output", str(Path(self.temp.name) / "x.json")])
        self.assertEqual(exit_code, 2)


class FixtureIntegrationTests(unittest.TestCase):
    """对仓库自带演练数据的端到端口径回归。"""

    def setUp(self) -> None:
        self.data = json.loads(
            (Path(__file__).resolve().parents[2]
             / "fixtures/analysis/input.json").read_text(encoding="utf-8"))

    def test_fixture_demo_persons_behave_as_documented(self) -> None:
        result = run_study(self.data)
        # 一人跨团队 + 一次降频 + 迟到回补（person-1）。
        p1 = records_for(result, "person-1")
        self.assertEqual(sum(1 for row in p1 if row["team"] == "monthly"), 1)
        revised = next(row for row in p1 if row["response_id"] == "response-revised")
        self.assertEqual(revised["recovery_delay_days"], 12.0)
        self.assertEqual(len([row for row in p1 if row["status"] == "completed"]), 2)
        # 提前退出者（person-8）退出后零机会、零覆盖。
        p8 = records_for(result, "person-8")
        self.assertEqual(len(p8), 1)
        self.assertTrue(all(row["status"] != "completed" for row in p8))
        # 历史月结与后来重述各自可解释：修订应答单列，原始应答进 unmatched。
        orphan_ids = {row["response_id"]
                      for row in result["opportunities"]["unmatched_responses"]}
        self.assertIn("response-1-1", orphan_ids)
        self.assertNotIn("response-revised", orphan_ids)
        # 总机会与完成数闭合。
        summary = result["opportunities"]["summary"]
        self.assertTrue(summary["identity_check"])
        self.assertEqual(summary["total"],
                         summary["completed"] + summary["missed"]
                         + summary["early_completed"] + summary["open"]
                         + summary["unknown"])
        self.assertEqual(summary["due"], summary["completed"] + summary["missed"])

    def test_fixture_sensitivity_never_imputes_missing_values(self) -> None:
        result = run_study(self.data)
        sensitivity = result["dropout_sensitivity"]
        for row in sensitivity["person_scenarios"]:
            if row["missing_count"]:
                self.assertEqual(row["bounds"]["worst_case"],
                                 row["scenario_rates"]["0.0"])
                self.assertEqual(row["bounds"]["best_case"],
                                 row["scenario_rates"]["1.0"])
        # 失访名单包含完全无应答者与退出者。
        self.assertIn("person-0", sensitivity["lost_to_followup_persons"])
        self.assertIn("person-8", sensitivity["lost_to_followup_persons"])


if __name__ == "__main__":
    unittest.main()
