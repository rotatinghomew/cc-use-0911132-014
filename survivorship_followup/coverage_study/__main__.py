"""命令行入口：

    python3 -m survivorship_followup.coverage_study \
        --input <followup_json> --database <followup_db> --output <coverage_json>

只使用 Python 标准库；读取的输入为本地合成资料。同输入重放产生同一 study_id。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from survivorship_followup.coverage_study.engine import (
    CoverageStudyError,
    run_study,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="随访服务覆盖研究：机会重建、覆盖率、标准化比较与失访敏感性分析")
    parser.add_argument("--input", required=True, help="随访演练输入 JSON")
    parser.add_argument("--database", required=True, help="既有产品流水线 SQLite 台账")
    parser.add_argument("--output", required=True, help="覆盖研究结果输出 JSON")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"输入文件不存在：{input_path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"输入 JSON 无法解析：{exc}", file=sys.stderr)
        return 2

    try:
        result = run_study(data, database=args.database)
    except CoverageStudyError as exc:
        print(f"覆盖研究失败：{exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8")
    print(f"覆盖研究完成：study_id={result['study_id']}，输出 {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
