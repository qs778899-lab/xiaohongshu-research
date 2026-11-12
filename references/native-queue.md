# 原生界面的持久化续查

适用：API run 不可复用，已有 CUA 人工观察与 JSON/文字断点。`xhs_native.py` 只管理本地任务和凭据，不控制 Chrome、不读取 Cookie，也不解除 STOP。联网操作继续使用已授权工具；脚本不能修复工具连接、证明人工记录真实或提供全站索引。

## 导入已有范围

由 Codex 阅读旧断点后创建显式 manifest。每个 unit 代表一个已规定范围，不是患者数、评论数或搜索页数。稳定键为 stage + target + sort；相同导入幂等。原始记录按哈希保存于项目 `native-sources`，不覆盖旧文档。

```json
{"units":[{"stage":"replies","target":"实际noteID","next":"从已观察父楼继续，剩余13回复","sources":["/绝对路径/既有观察.txt"],"priority":10},{"stage":"search","target":"实际查询词","sort":"latest","next":"选择最新并核对选择状态，再继续分页","sources":["/绝对路径/查询配置.json"]}]}
```

stage 支持 search/body/roots/replies/author/images/video/identity/employment/legacy_audit。正文 body 只表示文字；纳入完整帖子必须另建 roots、replies、images 和 video 范围。没有视频也要实际确认后记录 explicit_zero。无法解析的旧进度用 legacy_audit，由 Codex 核对原记录再拆分，不能猜测完成。

`author.target` 必须是实际观察到的公开账号 UID（24位十六进制），格式正确不等于身份已经核实。只有昵称或尚未打开主页时，先用 `identity` 登记“原帖/评论位置→公开账号”的待查范围，不能放入昵称、完整URL或伪UID。取得UID后另建 `author`；身份解析完成不等于作者历史读完。复制链接后必须核对实际地址、账号与来源，剪贴板里已有的地址不能证明刚刚复制成功。

旧版已误建的未知UID作者占位可显式迁移：

```bash
python "$XHS_SKILL/scripts/xhs_native.py" --project "$XHS_PROJECT" migrate-author-placeholder \
  --task '实际旧任务ID' --source '/绝对路径/迁移依据.txt' --reason 'UID未取得，需先解析公开账号'
```

仅迁移没有真实或含义不明内容观察、没有完成历史的待查占位任务；已有真实UID、目标身份任务冲突或旧来源失效会拒绝。任务ID、目标、旧凭据及失败/STOP不变，迁移依据归档；旧清单和新identity清单重导入仍落到同一任务。迁移不会增加已读/完成量，也不清除导航故障。迁移依据丢失时next/status/report单列审计；不是授权继续访问。

```bash
python "$XHS_SKILL/scripts/xhs_native.py" --project "$XHS_PROJECT" import --manifest '/manifest.json'
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" next --kind native --limit 20
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" report
```

导入任务纳入同一个 SQLite 的 next/status/report，但不伪装成可供 API 自动采集的任务。`resume/work` 不会控制 CUA；由 Codex 按 next 执行。旧报告中的已读声明先保留为来源，审核后才按具体范围记凭据。三排序分别建任务；先补已读帖的缺口，再扩新候选，避免只积累搜索卡片。

## 每次观察与逐层核验

打开已发现候选前，先按实际完整 note ID 查已登记阶段，避免搜索重排使旧帖再次从头展开：

```bash
python "$XHS_SKILL/scripts/xhs_native.py" --project "$XHS_PROJECT" lookup --target '实际完整noteID'
```

结果列出该目标的有效完成阶段、待处理断点及原始观察文件；只跳过仍有有效来源的已完成阶段。related_scopes另外列出以完整24位ID加冒号登记的身份/审计等子范围，不与正文评论阶段合并；主阶段完成不代表这些缺口消失。其他显式证据关联仍查项目report。它不按标题/昵称/ID前缀合并，也不搜索未导入的旧原文。无匹配时，再在本研究旧记录里查完整ID；旧原文已有阅读但队列未补账的，先离线核对，不能因为队列缺失就重读整帖。已完成阶段发现真实新回复时仍用new_gap续查；更小视窗不重开旧完成。只读查询不改变STOP、授权或通道状态。

每批同步当前优先清单：已读候选改为具体剩余阶段，已明确无关的标题候选标明排除依据，旧断点保留历史但注明被何条新记录替代；不要只在报告末尾追加结论而继续派发旧入口。

保存本次工具观察中与目标有关的原文或人工逐项记录，排除无关窗口资料。凭据字段：

```json
{"target":"实际noteID","action":"expand","outcome":"no_effect","observed_at":"实际带时区时间","summary":"点展开后未出现新回复，剩余13条未变","source_file":"/绝对路径/当前观察.txt","gaps":["父楼剩余13回复"],"next":"核对展开通道后接此楼"}
```

```bash
python "$XHS_SKILL/scripts/xhs_native.py" --project "$XHS_PROJECT" record --task '实际taskID' --receipt '/receipt.json'
```

action：input/navigate/read/scroll/expand/media/offline。outcome：ready/input_stale/content_loading/snapshot_mismatch/no_effect/window_missing/scope_changed/access_stop/offline。

- 先核对输入实际值，再提交；标题/地址变化与正文到位分开。正文题名、作者或结果卡片应与目标相符。
- 点击回复后核对新增文字、父楼及剩余数；不要只因为命令返回成功就重复点击。截图与文字仍属不同页面时记 snapshot_mismatch，不照旧坐标继续。
- 同一操作类型连续两次失败会跨任务、跨进程保留 hold；next 提示 channel_probe_required_no_repeat_action。暂停该类型，不用每个新帖子重试一遍。其他不依赖故障操作的离线或读取任务仍可推进。
- 用户恢复窗口或工具重连只提供恢复机会，不清空计数。一次当前观察确认 target_verified/content_ready/view_consistent 均为 true，才可记录 ready，重开该操作类型。以工具实际返回为准，不为了续跑填 true。
- access_stop 不因 ready 清除；遵循访问恢复流程。已有项目、账号或 run STOP 继续有效。脚本的 next 只是规划，不授予联网许可。

## 完成、较小快照和新增缺口

完整凭据还需 complete=true、reviewed=true、positions（实际原文位置）、gaps=[] 和该阶段真实 end。联网阶段还需实际 https 小红书 URL 及三个核验布尔值。搜索需 sort 与任务一致，end=platform_end；一级评论 platform_end/explicit_zero；回复 all_discovered_threads_read；作者 platform_end；正文 body_text_read；媒体需 media_verified=true，并使用 all_positions_read/all_relevant_segments_verified/explicit_zero。身份、任职、旧记录审计用 offline 与 assessment_recorded/claims_reconciled，允许结论是身份不足，但不能因此完成其他采集范围。

完整范围与部分观察独立：部分成功只追加事件，保持任务 pending。后续较小视窗不降低已读历史；发现真正的新遗漏时用 new_gap=true 重新打开，旧完成凭据保留。同一 receipt 重复导入不重复计数。完成来源缺失或哈希变化时报告自动退为有效待审。

原生范围数量在 `native_coverage` 中按阶段报告；observed_units 是有实际内容观察的范围数，reviewed_observation_units 是至少一次注明已阅读的范围数，两者都可能只是部分。只有 processed_units 表示范围结束且来源仍有效；不能把有观察当作已完成。不与 API 评论数相加，也不从昵称合并患者。评论 ID 缺失继续写明。不因所有已登记任务完成就断言平台穷尽；先核对医生矩阵、已发现作者和媒体是否都已登记。

仅登记“9图未读”“作者主页未打开”等缺口时，使用 `action=offline`、`outcome=offline`、`content_observed=false`、`reviewed=false`；发现媒体数量不等于观察了媒体内容。实际读到部分内容可记 `content_observed=true` 并按真实范围填写 reviewed，任务仍待查。旧凭据未提供该字段时保持兼容，不自动猜测哪些内容读过。

发现旧的非完成凭据把未读范围误记为已观察时，追加上述缺口凭据，并用 `corrects_observation_event` 指定同一任务的原 event ID，说明纠正依据。原凭据及归档不修改；报告和通道统计排除被纠正的那次观察，后续真实阅读仍累积。只可纠正 ready/offline 内容观察，失败与访问停止始终保留。纠正来源缺失或哈希变化时，按任务单列观察审计，next/report 提醒复核，不扣减无关或后来真实完成的内容。此机制不能撤销完成凭据；实际发现新的未完成范围仍用 `new_gap=true`，保留已完成历史。

原帖目标会与实际 URL 的 note ID、作者 UID 或搜索词校验，包含平台二次编码的查询值；页面文字仍须人工核验。重新导入旧清单不会覆盖较新的 next 断点。原始观察和完成凭据都按哈希核对，失效来源同时回到 next 与报告待审清单。

## 原生网页人工恢复登记

此入口只登记已经发生、经Codex审读的原生网页恢复，不发请求、不操作浏览器、不删除STOP。一次ready、首页可见、换标签或经过一段时间仍不足以恢复。出现真实登录失效、验证码或账号限制时，继续原停止/人工处理流程。

已知链接异常经同账号、实际站内卡片打开同一原帖，标题、作者、正文和页面状态一致且没有限制提示时，可以登记已核对范围。保留旧裸链接失败，不继续重试裸链接，不补造平台参数。共享研究可引用另一获授权任务的完整观察，但须读原始记录并明确实际观察者；消息里的“已恢复”本身不算凭据。网页恢复不证明窗口捕捉、点击、滚动故障已修好。

收据必填字段（依据真实观察，不可为了通过校验填true）：

原错误必须已有结构化身份：原事件收据的url/account_uid，或已归档来源的account_public_uid与original_stop.error_url（错误页的redirectPath可还原失败地址）。恢复原文须含observed_at、note.id，以及recovery中的original_account_uid、current_account_uid、original_failed_url、actual_success_url和三项界面核验布尔值。缺这些原始字段时拒绝自动登记，不能仅在恢复收据中补填一组自洽的账号/帖子。当前共享STOP还须绑定该原事件来源，恢复时间晚于STOP时间；不允许重算新STOP摘要来复用更早恢复证据。

- kind为same_note_native_web_recovery；event_id为本项目原access_stop事件；original_event_sha256为该事件绑定的来源SHA256。
- note_id、original_failed_url、recovered_url为同一笔记的实际URL；original_account_uid、account_uid为核对过的同一原账号。
- target_verified、content_ready、view_consistent、no_access_warning、account_verified为实际核验结果；entry_method为observed_in_site_card。
- authorization_basis为现有任务授权依据；positions为原错误/账号及恢复后标题、作者、正文在原记录中的位置；observed_at为晚于停止的带时区时间。
- observation_file、observation_sha256为原始恢复记录绝对路径与哈希；project_stop_digest用xhs_project.digest计算当前项目stop元数据，防止误用旧快照。

调用：

    python "$XHS_SKILL/scripts/xhs_native_recovery.py" --project "$XHS_PROJECT" --receipt '/实际人工恢复收据.json'

脚本归档收据及原始观察，只解决指定的历史原生事件，不增加采集完成量。不修改旧事件、项目STOP、账号/run/API停止文件。仅当项目STOP恰为shared_native_access_state_unknown、代码300017且没有更早STOP时，匹配同一元数据的有效恢复记录可使该标记不再阻止原生UI任务；API采集仍看到原STOP。其他/新停止事件、来源丢失或被改、账号/笔记不匹配均继续阻止；窗口故障计数不因恢复登记清除。

status中的native_web_recovery与channel_health.access显示原生流程有效状态；顶层stop是保留给API的原始元数据，不等于又观察到一次网页限制。格式验证不能证明人工观察真实，Codex仍须审读原记录、身份、入口及当前授权。登记后先重新核对实际窗口，再继续已授权串行读取。

## 复用已保存原文

实时操作故障时，可对明确指定的旧研究 raw 目录作离线召回，不读取临时登录目录。先用姓名与机构词发现线索，再读命中的完整父楼、回复、正文及同一公开作者后续；检索词命中不是归属依据。保存原文件、采集时间、JSON位置和哈希。真实 id/commentId 与公开 UID 可支持一致性核对；没有 ID 的相同文字可集中展示便于审读，不能因此计算为去重后的评论数。全文扫描过的文件数不等于已逐条审读的文件数，也不等于实时检索覆盖。

### 旧完整正文的离线审读入账

已有原始完整正文时，不必为完成文字阶段重新联网。当前限定接受已观察到的原始结构：`operation=note`、原 `note_id / url / captured_at`、唯一 `data.items[0]` 的 `model_type=note / ignore=false`，根、item和note_card的ID一致；完整文字在 `/data/items/0/note_card/title` 与 `/data/items/0/note_card/desc`。人工总结、搜索卡、错误、空正文、截断或不确定省略源不能使用本范围。

先实际读完标题和完整desc，再以原 `record --task T --receipt R` 提交：`review_scope=saved_original_body_text_v1`，绑定 `task_id / target / source_file / source_sha256 / url`、原 `source_captured_at`、固定 `title_pointer / body_pointer` 和原文字UTF-8的 `title_sha256 / body_sha256`。`reader=codex`，实际全文阅读后才声明 `title_read / full_body_read / source_body_complete=true`、`body_text_truncated=false`、`unread_body_segments=[]`。

该回执的 `action / outcome` 均为 `offline`；`ui_observed / content_ready / view_consistent` 均为false。正文文字确已读完时才用 `content_observed / reviewed / complete=true`、`end=body_text_read`、`gaps=[]`，并填写实际带时区 `observed_at`、源位置和内容总结。原捕获时间与这次阅读时间分别保留，不复制示例值或为通过校验代填实际阅读声明。

接口冻结并核对源文件与回执字节，归档变化时不发布完成记录。它只完成已保存版本的正文文字，不证明当前网页可访问，不推进图片、视频、评论、身份、成人或任职核验，也不清通道hold或任何STOP。媒体、无姓名和其他机构等边界仍在各自任务；后续发现正文变化保留旧版再审新版本。代码能核格式和来源链，不能代替Codex实际阅读或证明提供方未隐去内容。
