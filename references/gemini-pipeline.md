# Gemini 异步高效检索流水线指南

面向 **Antigravity / Gemini CLI / Gemini API** 的小红书高速深度检索方案。完全复用原仓库的 SQLite 队列、不可变原文快照、共享账号锁、`STOP.json` 熔断及 Chrome 固定配置绑定（**零自研扩展、免重启日常 Chrome、零 Codex/OpenAI 额度消耗**）。

## 核心提速与深查机制

1. **浏览器通道减阻（`fast_mode`）**：
   - `xhs_bridge_driver.mjs` 新增单次受守卫复合动作 `verified_capture`（把原有 `metadata + identity + capture` 三次完整往返合并为一次，保留同等账号/URL/CDP targetId/`ACCESS_STOP` 守卫）。
   - 搜索筛选面板已收起（`filter_already_closed`）时跳过冗余等待与二次捕获；压缩滚动前后死等。
   - 楼中楼深读开放有界预算（默认 `deepRoots=50, deepReplyClicks=5, deepBudgetMs=14000`），配合“同帖黏性调度”（优先把当前详情标签页的 `body → roots → replies` 连续读透再切帖），大幅减少跨帖重载折叠损耗。
2. **采-审异步并行（消灭 `handoff` 干等）**：
   - 采集轨维持**单账号严格串行 + 人类节奏抖动（默认 `5s ± 1.2s`）**；
   - 审读轨在后台多线程并发读取新落盘快照，分层调用 **Gemini Flash**（卡片初筛、正文提取、常规评论包、多模态看图）与 **Gemini Pro**（长楼归因与最终综合报告），并实时提交合规 `xhs_bridge.review()` 回执、自动入队高价值帖子。
3. **严格防幻觉与防注入**：
   - 所有小红书文本包裹在 `<UNTRUSTED_XHS_CONTENT>` 边界内；
   - 本地落地校验强制核对：所有卡片/评论实体无静默遗漏、每条 `quote` 均为原文真实子串、`context_supported` 必须说明 `missing_confirmation`、作者 UID 必须真实出现于原始快照、图片逐张校验 SHA-256（空白图自动标为不可读）。

## 环境准备（复用既有运行环境）

无需额外 pip 依赖（`xhs_gemini_client.py` 仅使用 Python 标准库），直接复用原仓库已安装的外部 Python/Node/OpenCLI 环境与已授权的 Chrome OpenCLI 绑定。

启用全自动 `api-async` 模式时，仅需在当前终端环境变量中配置密钥（严禁写入仓库或项目目录）：

```sh
export GEMINI_API_KEY="your-api-key"
# 可选覆盖默认模型或自定义 HTTPS 端点：
# export XHS_GEMINI_FAST_MODEL="gemini-2.5-flash"
# export XHS_GEMINI_DEEP_MODEL="gemini-2.5-pro"
# export XHS_GEMINI_VISION_MODEL="gemini-2.5-flash"
# export GEMINI_API_BASE="https://generativelanguage.googleapis.com/v1beta"
```

建议先对已有研究绑定开启后台文字模式（避免文字检索抢占 Chrome 前台窗口）：

```sh
"$XHS_PY" scripts/xhs_bridge.py --project "$XHS_PROJECT" text-mode --binding "$XHS_BINDING" --mode background
```

## 运行模式 1：`api-async`（全自动高速异步流水线）

```sh
# 1. 只读自检（检查绑定、共享 STOP、推荐模式）
"$XHS_PY" scripts/xhs_gemini_pipeline.py --project "$XHS_PROJECT" doctor --binding "$XHS_BINDING"

# 2. 启动采-审异步并行流水线
"$XHS_PY" scripts/xhs_gemini_pipeline.py --project "$XHS_PROJECT" run \
  --binding "$XHS_BINDING" \
  --mode api-async \
  --research-goal "研究目标、关注维度与明确排除条件" \
  --interval 5 --jitter 1.2 \
  --review-workers 4 --max-review-backlog 20 \
  --auto-enqueue \
  --units 60 --max-seconds 1800

# 3. 随时查看分阶段覆盖、已提取证据数与新发现别名/作者台账
"$XHS_PY" scripts/xhs_gemini_pipeline.py --project "$XHS_PROJECT" status

# 4. 用 Gemini Pro 长上下文生成带原帖链接、哈希与缺口表的综合报告
"$XHS_PY" scripts/xhs_gemini_pipeline.py --project "$XHS_PROJECT" synthesize \
  --research-goal "研究目标" \
  --output "$XHS_PROJECT/gemini/report.md"
```

常用可选开关：
- `--round ROUND_ID`：限定固定轮次范围，严格禁止自动扩新查询词（新词仅记入 `gemini/discoveries.json` 供下轮评估）。
- `--auto-expand-queries --max-expanded-queries 5`：仅在非固定轮次的开放深查下启用，自动把高频新发现查询词按上限加入三排序搜索队列。
- `--include-media`：同步对已捕获的轮播图 PNG 发起 Gemini Vision 逐张审读（仅当前台满足视觉守卫时采集截图；遇后台不可见保留缺口，不阻断文字流）。
- `--mode review-only`：完全离线，不连接 Chrome，仅用 Gemini 并发消化积压的未审快照和图片。

## 运行模式 2：`agent-hybrid`（零额外 API Key + Antigravity `run_workflow`）

当不配置 `GEMINI_API_KEY` 时，可由 Antigravity / Gemini 对话编排并行子代理：

```sh
# 1. 高速非阻塞采集一批任务（达到未审快照背压阈值自动停手）
"$XHS_PY" scripts/xhs_gemini_pipeline.py --project "$XHS_PROJECT" run \
  --binding "$XHS_BINDING" --mode collect-only \
  --interval 5 --jitter 1.2 --max-review-backlog 20 --units 25

# 2. 导出待审读批次包（每个评论包默认最多 150 条含完整父楼/回复对象上下文）
"$XHS_PY" scripts/xhs_gemini_pipeline.py --project "$XHS_PROJECT" export-review-batch \
  --limit 8 --output "$XHS_PROJECT/gemini/batch-001.json"
```

随后在 Antigravity 中用 `run_workflow` 对 `batch["tasks"]` 执行 `parallel(..., lambda t: agent(..., model="flash"))` 并行审读（图片任务直接用 `read_file` 查看 `media_items` 中的 PNG），汇总为 `{"results": [{"task_id": ..., "packet_id": ..., "review": ...}]}` 写入 `batch-001-results.json`，再提交校验与入库：

```sh
# 3. 经同一套防幻觉校验器校验并提交合规回执 + 自动入队高价值下游任务
"$XHS_PY" scripts/xhs_gemini_pipeline.py --project "$XHS_PROJECT" commit-review-batch \
  --file "$XHS_PROJECT/gemini/batch-001-results.json"

# 4. 离线确定性汇编（不调用 API 也能生成结构化证据与缺口报告）
"$XHS_PY" scripts/xhs_gemini_pipeline.py --project "$XHS_PROJECT" synthesize \
  --research-goal "研究目标" --offline-markdown \
  --output "$XHS_PROJECT/gemini/report-offline.md"
```

## 安全调参建议与禁区

| 参数 | 推荐值 | 边界说明 |
| --- | --- | --- |
| `--interval` | `4.5 ~ 6.0` 秒 | 程序强制最低 `3.0` 秒；严禁高并发或多进程同时刷同一小红书账号 |
| `--jitter` | `1.0 ~ 1.5` 秒 | 模拟自然浏览抖动；遇 `CONTENT_LOADING` 自动 1.5x 退避 |
| `--review-workers` | `3 ~ 6` | 仅并发本地审读与 Gemini API 调用，绝不并发 Chrome 操作 |
| `--max-review-backlog` | `15 ~ 25` | 统计未审快照总数，审读受阻时自动背压暂停采集 |

中断与风控处理：
- 手动随时暂停：按 `Ctrl-C` 或创建 `$XHS_PROJECT/bridge/PAUSE` 文件，采集线程 0.25 秒内响应退出，已落盘快照仍可离线审完。
- 遇验证码 / `ACCESS_STOP`：立即写入共享 `STOP.json` 并终止采集与自动入队；已采集的离线快照会正常审完落盘。恢复流程仍严格遵循 [访问恢复核查](access-recovery.md) 与 [用户标签的网页接续](manual-tab-recovery.md)，绝不自动清除 `STOP`。

## 验证边界

- 离线测试覆盖全部合成场景（`test_bridge_fast_mode.mjs`、`test_gemini_bridge_compat.py`、`test_gemini_client.py`、`test_gemini_pipeline.py` 及原有全量 Python/Node 回归），不联网、不读真实 Cookie。
- 离线通过不代表真实账号可访问、平台搜索末尾已穷尽或所有评论附件/视频已完成核查；真实 Chrome 标签页吞吐需在用户授权研究任务中小规模实测核对。
