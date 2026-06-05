# Test Result Summary 字段说明

说明：以下 `*_to_*` 字段均是在对比 **start (before)** 与 **end (after)** 两次测试运行结果后得到的分类统计。

## 基本信息

- `mstone_id`：milestone 编号（如 `M003.1`）。

## 测试状态变化

- `pass_to_pass`：start 通过、end 仍通过的测试数。
- `pass_to_fail`：start 通过、end 变失败的测试数（回归信号；失败里可能包含 assertion fail 或 error）。
- `pass_to_skip`：start 通过、end 变跳过的测试数。
- `fail_to_pass`：start 失败、end 变通过的测试数（"修复"或环境波动导致）。
- `fail_to_fail`：start 失败、end 仍失败的测试数（持续失败/不稳定/环境依赖都可能）。
- `fail_to_skip`：start 失败、end 变跳过的测试数。
- `skip_to_pass`：start 跳过、end 变通过的测试数。
- `skip_to_fail`：start 跳过、end 变失败的测试数。
- `skip_to_skip`：start 跳过、end 仍跳过的测试数。

## 测试集变化

- `new_tests`：end 新出现的测试数（start 没有，end 有）。
- `removed_tests`：end 消失的测试数（start 有，end 没有）。
- `total_before`：start 侧统计到的测试总数。
- `total_after`：end 侧统计到的测试总数。

## Flaky 测试

- `flaky_total`：被判定为 flaky 的测试数量（跨多次 attempt 合并时发现结果不一致）。
- `flaky_in_start`：flaky 发生在 start 侧的数量。
- `flaky_in_end`：flaky 发生在 end 侧的数量。

## 执行信息

- `attempts`：这个 milestone 的分类结果由多少次 attempt 合并得到（越大通常越不稳定/越需要重跑合并）。
- `errors_start`：从 `start_summary.json` 的 `results.error(s)` 汇总出的原始 error 条目数量（更偏"环境/执行错误"而非断言失败）。
- `errors_end`：同上，但来自 end 的 `end_summary.json`。
- `env_hints`：从 `start_default.log`/`end_default.log` 中按环境关键词（如 `connect failed` / `zookeeper not connected` / `child module missing` 等）匹配到的行数。
