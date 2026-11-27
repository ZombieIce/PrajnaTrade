# Issue #74 全链路合成验收证据

| 关闭条件 | 状态 | 实测 |
| --- | --- | --- |
| 本地策略插件与依赖指纹 (#75) | 通过 | 操作者通过显式 `--plugin-config` 登记本地插件（含薄包装器与导入实现）；Agent 入口拒绝登记；未登记策略族拒执行；仅篡改被导入实现文件触发 `PLUGIN_CONTENT_CHANGED` / `REVALIDATION_INELIGIBLE`，且已提交 Shadow 账本不受影响。同版本平台修复复检保持 `automatic_used == 1`。 |
| Shadow 逐日行情与停牌估值 (#76) | 通过 | `shadow export-feed` 从 Discovery 封存载荷按截止日无前视导出 schema-3 行情。`carry-forward-suspended-v1` 在全天停牌期间保留 `close: null` 与沿用估值，分红除息日扣减沿用价并计入应收款、派息日转入现金、复牌日按实际收盘价估值不重复调整；去除停牌状态证据立即阻塞 `missing_close`。 |
| 收盘后决策时点与共同观测 (#77) | 通过 | `after-close-common-v1` 在冻结截止点（`15:30 Asia/Shanghai`）选取五只 ETF 共同有效收盘观测；批准前预热日仅作历史不生成已提交日或业绩；当日派生数据晚到（`16:00`）仅暂停当日新信号并记录原因，此前已生效指令仍独立结算。 |
| 盘前冻结目标权重与开盘整手撮合 (#78) | 通过 | `actual-open-lot-v1` 盘前冻结 `open_weight` / `fixed_amount` 指令，次日按实际不复权开盘价（含跳空与复牌实际开盘价）与每手 100 份定量；先卖后买、以实际卖出净额与原有现金支持买入，区分正常整手尾差（`lot_rounding_residual`）与资金受限（`cash_constrained`），并在日记录明示日线模拟资金假设。 |
| 有状态调仓、月内顺延与跟踪恢复 (#79) | 通过 | 暴露全部 8 个策略状态字段及原有账户字段；跨月成交归属信号周期与信号日；零成交满足目标允许推进锚点；资金受限、停牌跳过或部分成交不推进锚点；月度顺延记录原定日、实际信号日与原因，整月未恢复记 `not_executed`，已提交未成交订单不自动重发；独立初始账户起步、重放幂等（`already_committed`），以及可唯一重建与不可唯一重建（`STATE_NOT_UNIQUELY_RECONSTRUCTIBLE` + 显式 `--new-start-date`）旧账本迁移均通过；#35 旧协议回归全部保留。 |

结论为「Spec #74（#75–#79）全部可显式选择、可冻结的新协议已通过离线合成端到端验收」。真实历史回测、真实数据源、私有策略与真实前瞻观察均未在本验收中运行，Final Holdout 保持封存。

## 复跑命令

从交付提交根目录执行以下命令（可按需指定 `$PYTHON` 与临时产物目录）：

```sh
evidence_dir="$(mktemp -d)"
PRAJNATRADE_TEST_CLI_SUBPROCESS=1 scripts/test.sh \
  tests/test_issue74_e2e_synthetic_acceptance_cli.py \
  tests/test_issue35_synthetic_acceptance_cli.py \
  tests/test_stateful_rebalance_cli.py -q --basetemp "$evidence_dir"
git rev-parse HEAD > "$evidence_dir/code-commit.txt"
printf '%s\n' "$evidence_dir"
```

细粒度单元测试与边界回归覆盖：

```sh
scripts/test.sh \
  tests/test_local_plugins_cli.py \
  tests/test_shadow_feed_v3_valuation_cli.py \
  tests/test_decision_timing_observations_cli.py \
  tests/test_open_lot_settlement_cli.py \
  tests/test_stateful_rebalance_cli.py \
  tests/test_issue74_e2e_synthetic_acceptance_cli.py -q
```

## 端到端合成场景与逐日预期/实测对账

验收测试（[test_issue74_e2e_synthetic_acceptance_cli.py](../../tests/test_issue74_e2e_synthetic_acceptance_cli.py)）通过公开 CLI 依次执行：
`snapshot seal` → `project init` → `plugin register` → `campaign create`（`actual-open-lot-v1` + `carry-forward-suspended-v1` + `after-close-common-v1` + `--monthly-deferral` + `--lot-size 100` + `--discovery-split 70/30`）→ `experiment submit`（本地插件 V1 与内置等权 V2）→ `round open/submit/validate/revalidate/review` → `shadow export-feed/update/report/compare`。

前瞻合成账本设置初始现金 `100,000.00`、手续费 `5 bps`、滑点 `0`、每手 `100` 份，包含 2 个批准前预热交易日（W1、W2）与跨越月边界的 7 个活跃交易日（D1–D7：D1/D2 属月 M，D3–D7 属月 M+1）。下表按日核对预期与实测结果（测试以绝对误差 `1e-8` 校验）：

| 会话 | 关键事件与指令 | 预期 / 实测持仓 | 当日费用 | 预期 / 实测现金 | 预期 / 实测净值 | 完整锚点 / 月度基准 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| W1–W2 | 批准前预热，返回 `not_started`，不写入已提交日 | 0 | 0.00 | 100,000.00 | 100,000.00 | `null` / `null` |
| D1 (M末-1) | 收盘后发出 `initial` 信号（`511260` 20%），盘前冻结 `open_weight` 指令 | 0 | 0.00 | 100,000.00 | 100,000.00 | `null` / `null` |
| D2 (M末) | `511260` 开盘跳空至 12.00；整手定量 `floor(20000 / 1200.60) = 16` 手（1,600 份），正常尾差 `lot_rounding_residual`；盘后发出跨月 `monthly` 信号（清仓 `511260`，买 `515080` 50%、`159612` 30%） | `511260`: 1,600 | 9.60 | 80,790.40 | 99,990.40 | D1 / `511260`: 0.20 |
| D3 (M+1首日) | `511260` 盘中复牌实际开盘 12.50 先卖后买（净入 19,990.00），买 `515080` 5,000 份与 `159612` 3,000 份（明示日线资金假设）；跨月成交归属 D2（月 M）；盘后 `518880` 派生值晚到（16:00），仅暂停 D3 新信号并触发月度顺延（`scheduled_date = D3`） | `515080`: 5,000；`159612`: 3,000 | 50.00 | 20,740.40 | 100,740.40 | D2 / `515080`: 0.50, `159612`: 0.30 |
| D4 (M+1-2) | `159612` 确认全天停牌 + 除息 0.20/份；`close: null`，沿用估值价 9.80（市值 29,400.00），应收股利 600.00，净值不变 | `515080`: 5,000；`159612`: 3,000 | 0.00 | 20,740.40 | 100,740.40 | D2 / 同上 |
| D5 (M+1-3) | `159612` 继续停牌，派息日 600.00 应收转现金；盘后按当日共同观测重算，发出顺延月度信号（`scheduled_date = D3`、`actual_signal_date = D5`，目标 `515080` 50.05%、`159612` 29.95%） | `515080`: 5,000；`159612`: 3,000 | 0.00 | 21,340.40 | 100,740.40 | D2 / 同上 |
| D6 (M+1-4) | `159612` 复牌开盘 9.80、收盘 10.00（不重复扣减分红）；顺延月度目标与持仓差额不足 1 手，零成交满足目标（`zero_trade_satisfied: true`）推进完整与月度锚点至 D5；盘后发出日常风控 `risk_control`（`515080` 降至 30%） | `515080`: 5,000；`159612`: 3,000 | 0.00 | 21,340.40 | 101,340.40 | D5 / `515080`: 0.5005, `159612`: 0.2995 |
| D7 (M+1-5) | `risk_control` 开盘卖出 `515080` `floor(19597.88 / 1000) = 19` 手（1,900 份，费用 9.50）；推进完整锚点至 D6 与 `last_filled_target`，**不改写** `last_monthly_filled_target`；重放 D7 返回 `already_committed` | `515080`: 3,100；`159612`: 3,000 | 9.50 | 40,330.90 | 101,330.90 | D6 / `515080`: 0.5005（月度未改） |

共同区间比较（`shadow compare`）对 V1 与 V2 在 D1–D7 共 7 个已提交交易日完成对账。

## 单条件反例覆盖索引

| 反例类别 | 触发变更 | 预期并实测结果 | 覆盖测试 |
| --- | --- | --- | --- |
| 权限边界与未登记插件 | Agent 调用 `plugin register`，或提交未登记策略族 | 拒绝执行，非零退出 | `test_issue74_e2e_synthetic_acceptance_cli.py`, `test_local_plugins_cli.py` |
| 篡改薄包装器后端依赖 | 仅修改 `stateful_logic.py`（不改包装器入口） | 触发 `PLUGIN_CONTENT_CHANGED` / `REVALIDATION_INELIGIBLE`；已提交 Shadow 日账保持不变 | `test_issue74_e2e_synthetic_acceptance_cli.py`, `test_local_plugins_cli.py` |
| 晚到/缺失观测数据 | D3 派生数据晚于 `15:30`（或收盘晚到/缺失） | 仅暂停当日新信号（`signal_status: paused`），不使用前视数据，前日有效指令独立结算 | `test_issue74_e2e_synthetic_acceptance_cli.py`, `test_decision_timing_observations_cli.py` |
| 未解估值缺口 | 移除停牌持仓 `159612` 在 D4 的状态证据并改为 `unknown` | 估值阻塞（`status: blocked`, `reason_code: missing_close`），沿用价绝不用于定量 | `test_issue74_e2e_synthetic_acceptance_cli.py`, `test_shadow_feed_v3_valuation_cli.py` |
| 资金受限与停牌部分成交 | 固定金额买单超过可用现金，或开盘停牌致部分成交 | `cash_constrained: true` 或 `unfilled` 非空，`batch_complete: false`，不推进锚点；已提交部分成交月度单不以顺延名义自动重发 | `test_stateful_rebalance_cli.py`, `test_open_lot_settlement_cli.py` |
| 不可唯一重建旧账本 | 旧账本含 `committed_with_anomalies` 或缺失 `signal_date` | `shadow migrate-state` 拒绝 `STATE_NOT_UNIQUELY_RECONSTRUCTIBLE` 并保留历史；指定 `--new-start-date` 后保留旧记录并在新起点干净起步 | `test_stateful_rebalance_cli.py` |
| 旧语义回归 | `next-open-v1`、`strict-close-v1`、内置等权/动量与 #35 全链路 | 行为、哈希与回执字段完全不变 | `test_issue35_synthetic_acceptance_cli.py` 及全部既有 fast 套件 |

## 制品索引

下列路径均相对于单次验收运行的临时产物目录（`--basetemp` 下的测试工作目录）：

| 阶段 | 路径 | 关联键 |
| --- | --- | --- |
| 插件配置与源码 | `plugins/plugins.json`、`plugins/wrapper.py`、`plugins/stateful_logic.py` | `family` → `plugin_id` / `dependencies_hash` |
| 快照与项目 | `snap-input.json`、`snap-store/<snapshot_id>/`、`project/.prajnatrade/project.sqlite` | `snapshot_id` → `campaign_id` → `experiment_id` |
| 轮次与正式验证/复检 | `round.json`、`ver-1.json`、`ver-2.json`、`trusted/research-loop-validations/`、`trusted/research-loop-revalidations/` | `round_id` → `version_id` → `validation_id` / `revalidation_id` |
| 准入与导出行情 | `calendar.json`、`obs.json`、`trusted/releases/<release_id>/`、`exported-feed.json` | `release_id` / `artifact_hash` |
| 逐日账本与比较 | `shadow-feed.json`、`ledger/shadow.sqlite`、`report-v1.json`、`report-v2.json`、`compare.json` | `version_id` (`release_id`) → `date` / `result_hash` → `strategy_state` |
