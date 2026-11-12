# 原 Chrome 的固定配置连接

用于用户明确选择官方 OpenCLI 扩展、沿用原 Chrome 登录的检索。此通道不依赖原生 App 前台窗口；没有可用扩展连接时报告 `SPECIFIED_PROFILE_OFFLINE`，不回退到整窗读取、其他配置或临时 Cookie 副本。

## 安装与能力门槛

先在隔离副本完成离线测试，再通过官方 Chrome Web Store 接入扩展。扩展 1.0.26 申请 debugger、tabs、cookies、activeTab、alarms、storage、tabGroups、downloads 及所有网站权限；任务封装只操作指定公开小红书页面，但不能称扩展本身只有该网站权限。实际授予新访问权限时完成系统要求的当次确认，不能把持续读取授权当成安装确认。

官方安装入口：https://chromewebstore.google.com/detail/opencli/ildkmabpimmkaediidaifkhjpohdnifk 。在用户指定的 Chrome 配置中安装；扩展弹窗显示并可复制 contextId，可据此核对配置。不因整窗读取被拒而通过脚本枚举其他账号页面。

锁定 npm `@jackwener/opencli@2.0.2` 的发布完整性摘要、安装后 dist/clis/package.json 哈希及 Node 二进制哈希。扩展版本、包内容或 Node 变更必须重新检查。`xhs_bridge_driver.mjs` 直接导入已锁定包的浏览器模块，避免上游 CLI 启动时全局 adapter discovery、兼容链接改写及默认配置回退。不使用其网络截获、Cookie、任意命令透传或页面隐身注入。

运行时 JSON 在研究环境之外独立保存：`node`、`node_sha256`、`package_root`、`package_tree_sha256`、`config_dir`、`cache_dir`。它不进入分享包。首次由安装维护流程依据实际安装生成；不可填写虚构哈希以绕过检查。

## 接口

命令均以实际 Python、skill 和项目路径运行：

```text
xhs_bridge.py --project P doctor --runtime R
xhs_bridge.py --project P doctor --runtime R --start-connector
xhs_bridge.py --project P profiles --runtime R
xhs_bridge.py --project P bind --runtime R --binding profile_alpha --context-id OBSERVED_CONTEXT --profile-label profile_alpha --account-uid OBSERVED_UID --profile-guard-key EXISTING_GUARD_KEY --authorization "本任务明确授权依据"
xhs_bridge.py --project P status --binding profile_alpha
xhs_bridge.py --project P recover --binding profile_alpha
xhs_bridge.py --project P release --binding profile_alpha
xhs_bridge.py --project P review --task TASK_ID --receipt REVIEW_JSON
xhs_batch.py --project P work --transport bridge --binding profile_alpha --units 10 --max-batches 12 --max-seconds 1800
```

`profiles` 只返回连接 ID 和扩展版本，不读取标签正文。扩展 ID 不等于 Google 配置名；安装位置、用户指定配置与小红书公开“我”链接 UID 必须一致。`profile-guard-key` 使用已核对原配置的既有 account.json key；没有旧记录时由原 `bind_account` 的 Chrome 根目录＋配置目录规则计算，不能随意新建不关联旧 STOP 的键。

`doctor --start-connector` 仅在本地没有 daemon 时启动校验过的包；现有 daemon 版本不符就停止，不升级、不终止其他任务的进程。普通采集不隐式替换连接进程。

每次操作固定 contextId/session/page。一个项目会话前缀下，搜索和详情各有一个持久 adapter 子会话，因为官方扩展的 tabs() 每个租约只返回一个 preferredTab。两个标签共用账号锁，作者历史复用详情标签。首次绑定逐个创建、登记标签，核对公开账号后才能采集。绑定中断不再盲目创建标签。普通重连只核验原目标，连接器不能证明标签已关闭时保持 `TARGET_MISSING`，不以缺失为依据创建替代会话。释放只关闭登记为本项目创建且身份核对成功的目标；不会注销 Chrome 或小红书。

已核对的上游能力差异：扩展把多个租约放在共享的 OpenCLI 窗口，并非每项目一个窗口；tabs() 不返回 Chrome windowId，也不能仅凭空列表区分已关闭标签和丢失租约。绑定中的 window_id 因此允许 null，隔离以 contextId＋每角色 session＋page 为依据。这三个缺口由 doctor 明列，不能写成已满足独立窗口或已自动重建标签。需要额外能力时先重新核对官方接口。

同一个 UID 的不同配置登记到无凭据的共享锁映射，所有已关联配置的 STOP 都保留；旧 API 路径同样取得关联锁。关联过旧账号的配置不会因更换登录而隐去历史限制。并发任务等待持有者结束，不删除别人的锁。

用户明确同意将某次匿名图片 CDN 403 与公开网页分通道核验时，可在项目 bridge/web-channel-authorization.json 保存该次授权、context_id、account_uid、scope=rendered_public_web_only、approved=true 和 preserved_media_stops（原 guard key→原 STOP 字节 SHA256），绑定记录保存此授权文件哈希 web_channel_authorization。这仅对精确匹配的 Public media access restriction: HTTP_403 生效，不清除原 STOP，也不解除 API 或图片下载。worker 在实际取得同账号的原帖正文前只允许正文核验；新 STOP 或授权文件变化仍阻止网页采集。没有明确的分通道授权时仍执行原停止规则。

## 本机连接诊断（先于浏览器恢复）

`profiles` 只检查连接器状态和扩展配置ID，不证明窗口是否打开或小红书是否登录。

- `CONNECTOR_PERMISSION_DENIED`：本机环回访问被执行环境阻断。不要让用户重开浏览器或登录，也不要启动新连接器。按工具正常审批机制，在获准的执行环境重跑同一状态检查；审批拒绝则保留诊断，不换通道绕过。
- `CONNECTOR_OFFLINE`：明确的连接拒绝；允许显式执行一次 `doctor --start-connector`。启动后仍不可用返回 `CONNECTOR_START_FAILED`，不能报告成功。
- `CONNECTOR_STATUS_TIMEOUT/UNAVAILABLE/HTTP_ERROR/INVALID`：连接状态未知，不能当作未运行并重复启动。先检查本机连接诊断。
- 只有连接器可达且指定contextId确实不在连接列表中，才报告 `SPECIFIED_PROFILE_OFFLINE`。版本不符继续停止，不关闭或替换其他任务的连接器。

上游 `fetchDaemonStatus` 会吞掉网络异常并返回null；本skill使用同一锁定SDK的 `requestDaemon('/status')` 保留错误分类。诊断不改变账号STOP或网页访问授权，连接恢复后仍须核验原登记目标与账号。

## 采集、审读与恢复

worker 消费现有 SQLite 中的 native 研究任务。API 任务仍保留原入口，不能把旧 kind 静默改成新类型。搜索用已登记关键词和排序；笔记、作者必须有实际观察 URL，否则交给 Codex补来源。页内代码再次校验 URL、查询和公开账号，防止元数据检查后发生导航竞争。

作者来源中的 `authorHrefRaw` 可以是实际观察到的相对主页链接。仅在作者阶段、原路径精确匹配目标 UID 的 `/user/profile/UID` 时，按固定 `https://www.xiaohongshu.com` origin 规范化；来源审计保留原 href、JSON 位置及原文件哈希。这是补接既有来源，不是新网页观察；不能仅凭 UID、昵称或正文拼出主页链接。

`prepare_target` 使用导航就绪后的最终页面可见状态。实际为 false 时只选择原角色标签一次，再完整核验账号、目标与可见性；仍为 false 返回 `TARGET_NOT_VISIBLE`，未知返回 `TARGET_VISIBILITY_UNCONFIRMED`，保留局部缺口。命令路由和选择标签都不证明其 Chrome 窗口已前置；连接器无法完成窗口前置时不据动作成功继续。用户恢复窗口后，仍须选中精确登记标签并重新核验。

排序只接受筛选面板的实际选中状态，不把“综合”等推荐词标签当排序。入口仅识别实际“筛选”或“已筛选”按钮，不把已选筛选误当控件消失；每次排序动作后重新观察面板。目标排序已选中时只关闭面板，不再次点击该选项，以免重查并回到首屏。排序修复须通过原任务的位置或笔记ID推进核验，不因代码修改或审读回执清除原停滞。当前 UI 结构不符合锁定适配器时输出具体缺口，禁止猜坐标或声称搜索成功。只读滚动诊断可核实际搜索输入和结果区/相邻布局文字；相关搜索、模糊匹配或推荐标题本身不证明目标相关或末尾。楼中楼复用 deep_comments 的逐楼续跑，项目内同帖共用进度，保留较小快照以前的楼层。

搜索与作者列表记录已观察的滚动位置、笔记 ID 与原文快照。切换目标后采用有限次位置恢复；懒加载导致位置被截短时先保存中途新结果，再继续推进，不把恢复到某个像素位置当作已覆盖前面所有内容。只有滚动位置或后台可见性改变时不新增证据快照。

原文按哈希保存到 bridge/raw，同时登记 native_events 和正文检索索引；没有真实评论 ID 的观察继续按快照区分。媒体定位保存顺序与来源并进入独立媒体任务，不自动标已看图、已转写。普通批次接续到时间/审读阈值；到审读点由 Codex读取原文并用既有 native 回执登记，不让用户逐批确认。

`bridge.awaiting_review` 只表示已取得待审资料，native task 仍 pending。Codex 实际阅读后，用 review 命令提交原 native 格式回执，并附 bridge_snapshot_hashes（本次确实阅读的哈希列表）。脚本核对原文哈希、归属和剩余未读快照，再释放该阶段续跑；只有显式结束且满足原完成规则才完成任务。部分审读仍保留未读状态，不手改 payload 或批量伪标 reviewed。

故障区别：

- `CONTENT_LOADING`：有限等待；后台未渲染时最多激活一次本角色标签，不请求窗口前置。仍失败时同一目标最多一次持久化重载，进程重启不重置次数。
- `TARGET_MISSING`：先检查原角色租约，空列表不证明标签已关闭。`recover`对本项目自有目标使用已核对的扩展1.0.26精确ID探测：只有明确返回该ID不存在，才归档旧目标并在同一contextId、项目及角色session中重建、重新核验UID；创建结果不明不重复。目标仍在但租约丢失、探测异常或身份不符时保持断点。不能由用户未关闭窗口推断标签ID仍存在，也不能由两个对话并行推断另一对话关闭了标签。
- `SCOPE_CHANGED`、`UNEXPECTED_TARGET_CHANGE`、`ACCOUNT_MISMATCH`：保持原绑定和断点，不切换目标。上次位置与本次已登记导航意图都不匹配时，先于正文读取停止。仅精确 `explore?source=404` 首页且原网页凭据仍有效的情况，可按[单次页级恢复](source404-recovery.md)核对同一自有标签并隔离原失败链接。
- `SPECIFIED_PROFILE_OFFLINE`：同配置连接恢复后 recover；没有默认账号回退。
- `CONNECTOR_OFFLINE`：检查本地 daemon，必要时用 doctor 的显式启动选项；不等同于需要重新登录。
- `ACCESS_STOP`：写共享账号 STOP，停止采集；不因更换连接器解除。精确单帖300031另按下节的 `probe / attest` 核验；原错误类型未知、用户确认当前原失败帖正常时，仅按[原失败帖当前状态复核](current-page-recovery.md)的一次原页核验处理。两类流程独立，诊断本身不恢复采集。 搜索导航后的identity失败、用户确认当前页正常且原查询仍在时，另见[原失败搜索页的单次只读复核](current-search-recovery.md)；不冒用笔记页恢复或清除STOP。
- `DEEP_READ_FAILED`：先读项目 `bridge/last-driver-error.json` 的实际原因；CDP超时提示中的弹窗只是错误模板，不能当已观察到弹窗。受限bridge深读在需要等待时保存当前结果、点击意图和逐楼断点后立即交接，等待交给串行调度；后台页内定时器不再占满整次求值。父楼与回复扫描均守预算，预算交接始终保持pending。单个同步DOM调用阻塞或页面无法执行JavaScript仍可能失败。
- 原评论任务零行且缺少可信空态时，可按[同帖评论结构诊断](comment-structure-diagnostic.md)做一次独立取证；不运行deep或分页，不解除hold，诊断代码的hash变化不算采集器修复。
- 对已保存的分页故障或 `DEEP_READ_FAILED`，有具体源码修复及离线验证后，才调用 `xhs_bridge.py --project P verify-local-fix --binding B --task T --basis 实质修复依据`。只读诊断代码的哈希变化或激活返回成功不算修复；原任务须实际内容、笔记位置或可靠渲染断点推进，才能解除对应故障。评论分支每个采集器版本最多两次、间隔10秒，版本含deep源码；纯yield、重复文字、点赞或相对时间变化不解故障。原fault及观察归档到 `bridge/local-fix-probes/`；访问限制或动作不明立即停止，不删除旧hold或STOP。成功也仍需Codex增量审读，不能据此声称全帖完成。
- 同帖一级分页停滞时，可为已采集且有正文的真实父楼建立独立 `replies` 范围。现有 `verify-local-fix` 会按不可变原文生成父楼允许清单；首次最多两次核验，只在真实回复新增、文字变化或已知回复重新呈现的凭据齐全时接续。原一级分页故障保留，不将回复推进算作父楼分页结束。
- 若上述合规续读随后实际观察到冻结范围以外的父楼 ID，先保存该快照及 ID 清单，作为具体的新页面证据。允许对原一级任务使用尚未消耗的有次数上限核验，核对是否确有新的父楼正文；这条路径不声称源码已改变，不通过改文件哈希重置尝试次数。必须取得真实新正文或可靠进度才解除原故障；仅有 ID、激活成功、重复页或观察到按钮均不够。失败保留缺口，不循环恢复。恢复后仍逐项审读，原账号和 STOP 规则不变。
- `COMMAND_OUTCOME_UNKNOWN`：动作可能已执行，先读取原目标状态，不重复创建/关闭/导航。
- 工具审批拒绝：保留具体动作和原因；现有授权明确时不反复询问，不换技术通道绕过。

系统休眠、Chrome 退出或后台页面不渲染仍可能中断。只承诺断点和目标校验，不承诺永久后台运行或锁屏后渲染。首次真实验收前将 bridge 标为未验证连接，不写“已稳定自动化”。

## 已验证标签的再次缺失

已有 fork 角色再次丢失时，普通 `recover` 只确认精确缺失并归档，不沿用旧首页证明直接创建。出现 `FORK_REPLACEMENT_HOME_REVERIFICATION_REQUIRED` 后，使用 `xhs_fork_role_recovery.py --project P --binding B --role search`（或实际缺失的 detail）；前提是同角色、同 session 的 exact absent 记录与原网页授权仍有效。

恢复记录只追加：后代证明链接前代文件与 SHA，逐代核对原账号、首页、grant 和 STOP，旧证明不覆盖。创建结果未知不重试；只有已归档确切 created ID、但首页尚未就绪时，才允许一次 `--resume-known-created ID`，仅续核该 ID。成功后先普通 `recover`，再实际读取已知原帖并审读，之后接续搜索。

仅当用户已授权该 Chrome 研究任务的应用前置时，可在本绑定 `app_foreground_authorization` 记录依据。同账号与原目标先通过校验、原标签激活后仍不可见，才尝试一次固定 Chrome 应用激活；随后必须再次验证同账号、同目标和实际可见性。不会枚举其他窗口；失败保留原断点，未记录授权的绑定维持原行为。

扩展兼容证据由绑定可选字段 `extension_compatibility_policy` 的绝对文件路径 `file` 与 `sha256` 引用。没有引用时沿用绑定原版本，不读取外部策略；显式引用无效、绑定指纹或源文件 SHA 改变时停止。通用 skill 不内置任何账号或研究项目的策略；兼容策略不放宽网页、账号和 API 停止边界，也不自动升级依赖。

## 单帖300031的网页恢复

若用户明确已在原账号恢复或新开正常标签，旧失败帖无法定位且其他帖子可读，另用[用户标签的网页接续](manual-tab-recovery.md)。它不把未知旧错误改写为300031，也不据首页可达解除停止；实际查询、正常正文和审读均验证后才建立独立的网页绑定。

`xhs_bridge.py diagnose-error` 只读既有精确标签元数据，只有嵌套原帖路径与已登记导航意图一致、错误码 `300031`、消息“当前笔记暂时无法浏览”且无验证提示，才分类为 `NOTE_UNAVAILABLE`。它仍禁止采集和重试，不改变STOP、账号或绑定。不要据这个名称断言删帖、登录失效或账号已恢复。

用户已授权自行打开正常研究页时，可使用下列正规入口；同一任务的该项授权可继承，无需逐批重问。缺少这项授权、原始错误证据或同账号正常页实核时，继续保留断点。验证码、登录失效、限流、真实账号限制及其他错误码均不适用，不能通过改写错误记录进入此流程。

```text
xhs_bridge.py --project P diagnose-error --binding B --role detail --file OBSERVED_ERROR_METADATA_JSON
xhs_note_recovery.py --project P probe --binding B --original-error ORIGINAL_ERROR_JSON --normal-source NORMAL_TARGET_JSON
xhs_note_recovery.py --project P attest --binding B --probe RETURNED_PROBE_JSON --review CODEX_REVIEW_JSON
```

输入和回执必须来自实际观察，留在研究项目，不放入skill包：

- `ORIGINAL_ERROR_JSON` 保存故障时的 `binding_snapshot`、`observed_at`、精确 `metadata.page/url` 及其 `source_file/source_sha256`。旧导航意图、ACCESS_STOP时间、原始元数据及同一账号关联STOP集合须相符；不拿新摘要补造旧现场。
- `NORMAL_TARGET_JSON` 的 `scope` 为 `one_normal_public_research_note_health_check_only`，列出与当前绑定一致的 `context_id/account_uid/profile_label`，实际已观察卡片的 `url/target_note_id/source_file/source_sha256/source_position`，以及 `user_request.text/processed_at`。正常页须为另一篇已观察到的公开研究笔记，不能是错误原帖、裸ID猜链、首页或其他账号页面。
- `probe` 在原账号锁下只检查这一篇正常笔记：固定扩展、同一配置与公开UID，核对metadata、identity及有正文的body捕获一致且无访问提示；不会搜索翻页或展开其他研究内容。成功后返回 `probe_file`、`normal_file` 和正常观察文件哈希，仍需Codex审读。
- Codex读完 `normal_file` 的真实正文后，回执绑定 `probe_id/normal_sha256`，填写 `reviewed: true`、实际阅读 `summary` 和 `positions`，再调用 `attest`。不能把工具成功、首页可达或已有登录摘要当原文已读。

`attest` 仅签发 `rendered_public_web_only` 凭据，正常已核标签成为详情入口；错误原帖及其待查阶段隔离为 `NOTE_UNAVAILABLE`，等待新有效来源，不自动重试旧链。原STOP字节保留，API、二级评论端点及图片CDN下载仍受原限制。每次网页采集重新校验凭据、原始来源/回执哈希、账号及STOP集合；任一来源变更、出现新STOP或不同账号立即使凭据失效。

每个原故障与STOP指纹只允许一次 `probe`，动作前即写尝试记录；超时或结果不明不得再开正常页重复探测。若正文捕获和绑定均已保存，仅 `attest` 交接中断，继续对同一 `probe_file` 和原回执提交；它支持幂等续交。缺少完整捕获凭据时保持断点，不能删除尝试记录重跑。此流程通过局部实测不代表所有访问故障或全量检索已解决。

### 同项目的独立检索标签

原研究标签已被其他任务使用，且用户已有自行创建研究标签的授权时，可从有效的原始300031网页凭据派生独立绑定：`xhs_bridge.py --project P fork-binding --parent PARENT --binding CHILD --intent STABLE_INTENT --authorization "已有授权依据"`。新绑定沿用同项目、配置、公开UID及网页授权，使用独立session和新建主页；不读取、导航或关闭父标签，不复制父正文核验状态。每次子网页租约重核父凭据、原始来源、授权和全部STOP哈希；权限仍限公开网页，父绑定、原凭据及STOP保留。

固定本次intent。创建结果已明确、登记为自有created主页而初始化核验未完成时，同命令仅对原ID继续精确metadata与UID核验，不重建已登记页；无page或创建结果不明不得换intent重试。其他URL、角色目标变化或账号不符保留故障，观察归档到该intent。成功也只证明新主页身份；新child仍须实际读取已观察研究帖正文，才能推进其他采集，不能把主页或工具成功当正文已读。

## 回归及发布

运行 test_bridge.py、test_bridge_dom.mjs、test_account_guard.py、test_batch.py、test_autonomous_work.py、test_native.py 和 test_deep_comments.mjs；验证实际版本 doctor。离线测试不能代替 profile_alpha 上的三排序、评论、作者分页及后台切换验收。

只同步经过测试的脚本与说明；同步前比对桌面副本哈希防止覆盖其他任务新修改。运行环境、原文、绑定、账号 ID 和授权记录留在项目外部，不放进 skill。

实测兼容：作者页笔记可使用 /user/profile/UID/NOTE 链接并跳到 /explore/NOTE；只在公开路径和同一笔记 ID 均核验后接受该跳转。列表滚动后先采集并保存位置再切换任务；卡片相同但滚动位置推进不算停滞。评论可见终点包括 THE END；未知回复总数保持 null，只在真实终点且没有折叠、缺失正文或身份缺口时记录可见范围完成，不冒充平台隐藏回复全量。

已绑定详情页可使用驱动的受限 screenshot 动作保存当前笔记网页画面，必须传精确笔记路径并再次核对账号。DOM执行和截图均核对传输层实际返回的CDP目标ID，不能把请求ID补作实际ID；不符即丢弃内容，交互结果不明不自动重放。截图前后还须确认原目标可见；未知或后台状态不能保存为已核图片。此操作是网页渲染取证，不解除匿名图片下载 STOP；不截取整窗或其他标签。图片主导帖允许保存正文为空但已定位媒体的快照，媒体未读仍留在队列。登录守卫UID属于采集账号，不是帖子或评论作者；作者归属使用正文作者链接或真实评论的authorHrefRaw。

没有回复总数、没有折叠但列表尚未结束的楼层使用 visible_scope_pending_end：保存已读内容指纹，下一批继续后续楼层，不反复处理前30楼，也不提前算完成。后续出现折叠或正文缺口会重新进入待查；只有真实终点且逐楼检查通过才完成可见范围。扩展单次深评论预算8秒、最多2次展开，避免超过25秒求值超时；预算结束接续原断点。

## 旧网页项目衔接

已有精确同账号同帖恢复收据的300017原生网页项目，bridge只读检查既有恢复来源、STOP摘要、恢复账号与绑定账号，以及未解决的后续access_stop事件；API仍读取原STOP。缺失/改变恢复归档、账号不匹配或新限制一律继续阻止。它不将恢复判定推广为新账号或新接口恢复。

显式bind支持 `--web-authorization /已存在的网页通道授权.json`：仅引用用户已明确批准的rendered_public_web_only授权，核对contextId与UID，保留原字节和哈希。缺失授权不得自动构造；这不授予API或图片CDN访问权限。新项目须自行创建并验证标签、UID与正文，不能复用另一个项目的tabs/session/web_validation。

## 图片轮播与节省重复读取

驱动新增受限 `media_state / media_select`：仅接受已绑定同账号的明确笔记路径。读取当前渲染轮播的零起始索引和总数；选择时必须同时传 `index / expectedIndex / expectedCount`。仅在页面索引连续、唯一活动项及既有轮播控制均可确认时切换；目标或位置变化立即停止。选图后重新读取 `media_state` 确认目标索引，再用 `screenshot` 保存并由 Codex 阅读，不能把动作返回成功当图片已读。这是已授权网页内翻图，不下载CDN或解除媒体STOP。找不到轮播、单图无序号、视频或不支持的布局保留明确缺口。

已有评论展开较深时，优先保持同一详情标签连续推进至本阶段结束，再切换候选；频繁跨帖会折叠并重载已展开楼层。新批次仍核对真实ID、末尾信号及未展开项。审读只输出未读快照中的新增/变化文字，并保留父楼和必要旧上下文；不重复输出完整旧快照，不用关键词筛掉未读楼层。不以这一顺序规则替代多医生覆盖轮换。

可复用命令：`xhs_bridge_media.py --project P --binding B --task 已登记正文或图片任务ID` 先读图片总数；追加 `--positions 1 2 ...` 按一开始的序号保存实际网页截图。它复用原账号锁、明确来源URL和STOP校验；不自动登记已读，也不将某个视频帧标成已看视频。截图和元数据保存在项目bridge/media，完成阅读后按native媒体回执登记。

图片诊断同时记录实际元素、尺寸和解码状态；空壳或未加载返回 `empty_or_unloaded`，不能据此判定无媒体。已解码也可能尚未绘制：在账号和原帖核验后只激活自身研究详情标签一次；每个位置先取一张受守卫的预热截图并丢弃像素，等待绘制后核同一媒体身份，再取正式图。不请求其他窗口聚焦，仍以Codex实际看到的截图为准。前后核对序号、总数、类型和已加载资源的稳定身份，忽略已知签名变化与非身份的动态尺寸；真实资源变化保存结构化诊断并停止该项，不自动重试。未审旧缓存缺少当前捕获版本的激活和预热凭据时重采，已有视觉审核且哈希有效的成果保留；旧截图与原始记录不删除。

评论附件用真实评论ID和原快照的实际图片URL定位。原生预览点击前保存意图，点击结果不明或预览无法绑定原资产时不重放、不把别的可见图片当来源。受限 `comment_media_display_state / comment_media_enlarge / comment_media_restore` 可对已经解码的横图或方图试做原节点可逆显示：可信保存原状态及意图，核对原状态后仅调整三个节点的六项显示属性，受守卫截图后立即按原值恢复，再由Codex实际阅图。长图、裁剪容器装不下、后台不可见或来源变化保留缺口；不能放宽守卫强行恢复。此分支已有合成测试，真实研究中尚未完成可见图片闭环，不声称已稳定自动处理评论图片。


## 回复前缀与文字收尾的一致性

一级评论分页结束和全部可见回复文字读完分别记录。根分页END不能覆盖未呈现正文、未确认展开、身份或扫描缺口；完整审读回执还会重读同帖且SHA一致的原始来源核对，不能只信旧collection_end布尔。已有同身份正文的保留版本继续有效。

仅剩无可用展开控件的停滞回复前缀时，保存COMMENT_REPLY_BODY_UNAVAILABLE本地hold与原CID／原始来源，继续调度其他任务。提交部分审读不会清除此故障；需要明确代码修复后的有版本、最多两次串行探针并实际补齐原缺正文才可解除。根分页结束仍可启动已单列的replies任务，不把父任务伪标完成。此机制不清账号STOP、不增加网络并发，也不把未取得的文字算已读。


## 后台文字与视觉用途

连续文字任务可按[后台文字采集](background-text-collection.md)启用原绑定的后台模式；后台无需前置Chrome，视觉处理器保持原可见性守卫。原前台模式及全部既有接口保留。
