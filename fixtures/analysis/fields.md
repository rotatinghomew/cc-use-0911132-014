# 业务演练数据口径

计划区间左闭右开，supersedes 仅自新计划 start 起替代旧计划。opportunity_at 指该答复对应的应随访机会，received_at 是实际回补时刻，二者不可互换；responses.supersedes 替代同一机会的旧应答。退出后不再生成新机会。未回复人没有可推断的问卷真值。

各 ID 均为本地合成标识，不包含真实人员或生产数据。时刻使用带时区 ISO 8601；as_of 是分析可知信息的截止时刻，不是业务发生时间上界。晚到事实允许保留在输入中，但只能在 available_at 到达之后采用。source_versions 标识输入版本；baseline_case_id 关联同库既有演示运行。空值表示未知，不应未经说明转成零。
