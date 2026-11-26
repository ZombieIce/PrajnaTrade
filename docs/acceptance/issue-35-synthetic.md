# Issue #35 双循环合成验收证据

| 关闭条件 | 状态 | 实测 |
| --- | --- | --- |
| Research Loop | 通过 | Discovery 按 7/3 切分。两个锁定版本分别取得正式 Validation `passed` 回执，首次读取共计 1 次自动和 1 次人工用量，两次重放均未增加用量。 |
| 人工准入与 Evidence Loop | 通过 | 同一轮次下的两个版本分别获批，在不同会话启用独立模拟账本。逐日持仓、现金、净值及机会分类均与预期相符。 |
| 共同区间对账 | 通过 | D2 至 D4 连续三日均已提交。开盘前持久化的订单可按 ID 追到下一交易日终态，共同区间和两套完整历史均通过账本复算。 |

结论为「双循环系统已通过合成数据端到端验收」。真实历史回测、真实源质量和实际前瞻观察在本轮均未运行。样例只有合成输入、测试额度与模拟订单，Final Holdout 保持封存。旧 [#17 验收报告](issue-17-report.md) 记录原六场景口径。

## 复跑

被验收的应用源码来自提交 `215ebdbadac26d72834f031a4c127003d24ce2bb`，本次交付增加回放测试与证据索引。复跑时从包含[验收测试](../../tests/test_issue35_synthetic_acceptance_cli.py)的交付提交运行以下命令，`code-commit.txt` 记录该次实际 checkout。

```sh
python -m pip install -e '.[test]'
evidence_dir="$(mktemp -d)"
PRAJNATRADE_TEST_CLI_SUBPROCESS=1 scripts/test.sh \
  tests/test_issue35_synthetic_acceptance_cli.py -q --basetemp "$evidence_dir"
git rev-parse HEAD > "$evidence_dir/code-commit.txt"
printf '%s\n' "$evidence_dir"
```

测试通过公开 CLI 参数调用 `snapshot seal`、`campaign create`、`experiment submit`、`round open/submit/validate/admit/review` 和 `shadow update/report/compare`。输入和回执保留在 `$evidence_dir/test_synthetic_dual_loop_accep0/`，包括生成时的完整 ID 和时间戳。每次运行使用新目录。审批使用实际运行时刻，回放会话按上海本地日期生成。`shadow update` 的测试时钟推进到各会话 16:00，行情逐日加入，交易日历提供下一会话的日期。

## Research Loop

合成快照有 17 个会话，封存 Discovery 为 2024-01-01 至 2024-01-10 共 10 日。前段 2024-01-01 至 01-07 共 7 日，前 4 日用于预热，实际评分 01-05 至 01-07 共 3 日。正式 Validation 使用 01-08 至 01-10 共 3 日，两个版本从 01-08 即可评分，预热来源均为前段 7 日。

每个账户初始现金 100,000，手续费 5 bps，滑点 0，按 `next-open-v1` 以未复权开盘价 10 买入。V1 和基准为五只合成标的等权，V2 用 3 日回看选择涨幅最高的 `511260`。只有该标的在前段最后一日收于 10.5、后段最后一日收于 11。投入市值为 `100000 / 1.0005`，期末净值为投入市值加上该标的持股乘以收盘价涨幅。

| 阶段 | 账户 | 预期 / 实测成交笔数 | 预期 / 实测期末净值 | 实测收益率 |
| --- | --- | ---: | ---: | ---: |
| Discovery | V1 / 基准 | 5 / 5 | 100,949.525237 / 100,949.525237 | 0.949525% |
| Discovery | V2 | 1 / 1 | 104,947.526237 / 104,947.526237 | 4.947526% |
| 正式 Validation | V1 / 基准 | 5 / 5 | 101,949.025487 / 101,949.025487 | 1.949025% |
| 正式 Validation | V2 | 1 / 1 | 109,945.027486 / 109,945.027486 | 9.945027% |

预期用量为 V1 首读/重放 `1 → 1`。V2 在自动额度用完时被拒，累计仍为 `1`。操作员通过 `round admit` 发放一次绑定 V2 的测试准入，随后 V2 首读/重放 `2 → 2`。实测计数与预期一致。两次重放复用原 `validation_id`、`receipt_hash` 和用量记录，准入记录的 `consumed_validation_id` 指向 V2 回执。

## 人工准入与逐日模拟账本

两次 `round review` 各有一个 `decision_id` 和一个 `enable` 事件。V1 和 V2 获批后，分别于各自测试日历的首个可用会话 D1 与 D2 生效。批准前的会话对两者均返回 `not_started`，D1 对 V2 也返回 `not_started`。

下表为预期与实测一致的日末结果。金额与份额显示至小数点后六位，测试以绝对误差 `1e-8` 核对。

| 版本与会话 | 订单或信号结果 | 日末持仓 | 现金 | 净值 |
| --- | --- | --- | ---: | ---: |
| V1 D1 | 1 个应交易机会，盘后持久化 5 笔 D2 买单 | 0 | 100,000 | 100,000 |
| V1 D2 | 4 笔 `filled`，`518880` 缺开盘价，终态为 `missing_open` | 4 只各 1,999.000500 | 20,000 | 99,960.019990 |
| V1 D3 | 无新订单 | 4 只各 1,999.000500 | 20,000 | 99,960.019990 |
| V1 D4 | `511260` 收盘价升至 11 | 4 只各 1,999.000500 | 20,000 | 101,959.020490 |
| V2 D2 | 仅 3 个可用会话，少于所需 4 个，`metrics_not_ready` | 0 | 100,000 | 100,000 |
| V2 D3 | 1 个应交易机会，盘后持久化 1 笔 D4 买单 | 0 | 100,000 | 100,000 |
| V2 D4 | `511260` 买单终态为 `filled`，收盘价为 11 | `511260` 9,995.002499 | 0 | 109,945.027486 |

V1 的机会计数（应交易、全部成交、存在未成交）为 `1 / 0 / 1`，数据不足为 0 日。V2 为 `1 / 1 / 0`，数据不足为 1 日。D1→D2、D3→D4 的每笔源订单与终态订单 ID 和数量相同，终态 `persisted_at` 等于源日提交时间并早于下一会话 09:30。回放将缺开盘价只施加在 D2 的 `518880`。

## 共同区间对账

从两份 `shadow report` 读取已验证的逐日结果，以区间首日净值计算收益，逐日累计费用并从峰值复算最大回撤。V1 独有的 D1 只出现在其完整历史，其 D1 机会在 D2 终结为 4 笔成交和 1 笔未成交。V1 的共同区间因此有 1 笔未成交，机会计数为 0。V2 的完整历史与共同区间重合。

| 版本与范围 | 预期 / 实测会话数 | 首日净值 | 末日净值 | 累计收益 | 最大回撤 | 区间费用 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| V1 共同 D2–D4 | 3 / 3 | 99,960.019990 | 101,959.020490 | 1.999800% | 0% | 39.980010 |
| V1 完整 D1–D4 | 4 / 4 | 100,000 | 101,959.020490 | 1.959020% | -0.039980% | 39.980010 |
| V2 共同与完整 D2–D4 | 3 / 3 | 100,000 | 109,945.027486 | 9.945027% | 0% | 49.975012 |

## 制品索引

下列路径相对于该次测试产物目录。所有 `vN` 文件均用相同版本编号关联。

| 阶段 | 路径 | 关联键 |
| --- | --- | --- |
| 原始输入和轮次 | `project-snapshot.json`、`project-round.json`、`campaign.json`、`round.json` | `snapshot_id` → `campaign_id` → `round_id` |
| 实验与锁定 | `experiment-vN.json`、`locked-vN.json` | `experiment_id` → `version_id`，并含执行规则及运行时指纹 |
| 正式 Validation | `validation-vN.json`、`validation-vN-replay.json`、`project-trusted/research-loop-validations/<validation_id>.json` | `provenance.version_id` → `validation_id` / `receipt_hash` |
| 测试额度 | `validation-v2-blocked.json`、`admission-input.json`、`admission.json`、`round-status.json` | `admission_id` → `consumed_validation_id` |
| 操作员准入 | `calendar-vN.json`、`observation.json`、`approval-vN.json`、`release-vN.json`、`lifecycle-vN.json` | `validation_id` / `receipt_hash` → `decision_id` → `release_id` / `event_hash` |
| 逐日更新与完整历史 | `feed-<date>.json`、`update-<date>.json`、`ledger/shadow.sqlite`、`ledger-vN.json`、`evidence-status.json` | `release_id`（`shadow report` 的 `version_id`）→ `date` / `result_hash` → `order_id` |
| 共同区间 | `comparison.json` | `common_interval`、`per_version[release_id]`、`full_history` |

从任一正式回执复核时，在对应的 `release-vN.json` 查找相同的 `binding.validation_id` 和 `binding.receipt_hash`，然后用该制品的 `release_id` 查找 `ledger-vN.json`。每个报告日的 `result_hash` 与对应的 `update-<date>.json` 一致。
