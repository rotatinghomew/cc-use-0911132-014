"""服务覆盖研究：重建随访机会、覆盖率、标准化比较与失访敏感性分析。

本包只评价“服务是否触达”，不评价治疗效果。输入为本地合成的随访演练数据
（见 fixtures/analysis/fields.md 的口径说明），输出可重放、可解释的覆盖研究结果。
"""
from __future__ import annotations

from survivorship_followup.coverage_study.engine import (
    CoverageStudyError,
    run_study,
)

__all__ = ["CoverageStudyError", "run_study"]
