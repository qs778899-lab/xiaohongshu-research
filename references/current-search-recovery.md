# 原失败搜索页的单次只读复核

仅用于已有完整 rendered-public-web 授权的原绑定：搜索导航后的 `identity` 曾触发 `ACCESS_STOP`，用户随后明确确认当前页面正常。原 `search` 标签必须仍在原查询；不能借此导航、重载、选排序、新建会话或更换账号。普通首次访问、其他失败动作、原笔记页失败继续走各自流程。

先读取原 `bridge/last-driver-error.json` 和 `bridge/handoff.json`，核对 search/identity/ACCESS_STOP、时间及原任务。它们在 probe 前冻结到项目归档，后续运行更新同名文件不替换历史证据。原 search.navigation_intent、query、任务、账号、context/session/page 和既有授权图都必须对应，所有 STOP 原字节保留。

用户确认 JSON 保留真实原话及来源，格式如下；`processed_at` 是实际处理这次回复的时间，不虚构用户发言时刻：

```json
{"user_confirmation":"页面均正常","confirmation_reference":"实际用户回复来源","processed_at":"带时区时间","known_failed_task":"原搜索任务ID","binding":"原绑定名","profile":"原配置标签"}
```

```text
xhs_current_search_recovery.py --project P probe --binding B --task ORIGINAL_SEARCH_TASK --confirmation USER_CONFIRMATION_JSON --authorization "已有只读研究授权依据"
xhs_current_search_recovery.py --project P attest --binding B --probe RETURNED_PROBE_JSON --review ACTUAL_CODEX_REVIEW_JSON
```

probe 先消费与该 binding/失败/STOP 指纹对应的固定一次机会，然后仅对原已加载页执行 `metadata → identity → capture(search)`。每阶段返回后立即落盘，登录/验证/访问提示、目标或身份变化、空/未渲染结果、超时及中断均失败并消耗机会。没有导航、筛选点击、Cookie、API 或 CDN 请求；失败后不能换确认时间/输入/任务获得新机会。此分支不支持自动子复核；只有下述用户明确人工重新登录后的固定一次子复核例外。

Codex 实际读取 `normal_file` 中的原查询、账号、卡片全文和返回状态后，回执使用真实返回哈希：

```json
{"probe_id":"实际ID","normal_sha256":"实际SHA256","reader":"codex","reviewed":true,"query_read":true,"search_cards_read":true,"body_read":false,"sort_verified":false,"pagination_verified":false,"summary":"实际观察及限制","positions":["/capture/url","/capture/cards","/capture/position"]}
```

复核未选择或核实排序；即便原任务请求 popular，也不能把该读取记成已完成热门排序、分页、正文或评论。attest 只恢复原绑定原有的 rendered-public-web 范围，不完成任何任务；普通采集仍须独立核排序、查询和账号。原 STOP、API/CDN限制不变，新 STOP 或授权来源/固定身份变化立即撤销恢复。

离线回归：`python -m unittest discover -s scripts -p test_current_search_recovery.py`。合成测试不证明真实原账号页面已通过复核；真实 probe 始终只允许一次。

## 用户重新登录后的唯一子复核

仅当原 root probe 在 `identity` 触发 `ACCESS_STOP`、已保存原 page/query 匹配的 metadata，且用户在失败后明确报告人工重新登录并要求继续时，允许派生一次 child。保留原 attempt/failure/STOP，不重开 root，不导航或选排序。用户新确认应保存真实原话和实际处理时刻：

```json
{"kind":"same-target-search-manual-login-restoration_v1","project":"原项目绝对路径","binding_name":"原绑定","task_id":"原任务","user_statement":"我已重新登陆好了，你继续","confirmation_reference":"实际用户新回复来源","processed_at":"晚于原failure的带时区处理时间","original_attempt":{"file":"原root/attempt.json绝对路径","sha256":"实际SHA256"},"original_failure":{"file":"原root/failure.json绝对路径","sha256":"实际SHA256"}}
```

原 `--confirmation` 仍引用首次正常确认文件，另加新确认：

```text
xhs_current_search_recovery.py --project P probe --binding B --task ORIGINAL_SEARCH_TASK --confirmation ORIGINAL_CONFIRMATION_JSON --authorization "持续研究授权依据" --manual-restoration MANUAL_LOGIN_JSON
```

child 从原 root attempt 中冻结的 failure_sources 核对原故障，不读取已被 root 失败覆盖的运行态 error/handoff。人工确认时间、原失败metadata及来源SHA、同账号/binding/session/page/query/task、全部STOP字节均重新核对。child ID固定由 kind＋原root probe ID生成，先保存 attempt 消费机会，再执行相同三步只读核验。改确认时间或输入文件、换任务、失败/中断、引用child为parent都不能取得第二个child。只有实际capture通过、Codex读卡片并attest后，才恢复原有网页范围；排序、分页、正文评论仍未核。
