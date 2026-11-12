# 运行说明

深查默认使用 [项目级 v2](project-v2.md)。本文的单项采集命令保留兼容性；其页数是单批预算，不代表项目完成。

恢复已有任务先用[自主运行与故障分流](autonomy.md)的本地只读检查。预算、审读交接和可修复缺口继续复用本次会话；不要直接要求用户重新登录。

## 已验证的组合与边界

2026-10-29 至 2026-10-30 的实际检索使用：

- [xiaohongshu-cli](https://github.com/jackwener/xiaohongshu-cli) 0.6.4，Python ≥ 3.10：搜索、正文、一级评论分页。
- [OpenCLI](https://github.com/jackwener/opencli) 2.0.2，Node ≥ 20.18.1：Chrome 中展开楼中楼。
- macOS Chrome 指定已登录配置的小红书会话。Chrome 配置名称与小红书昵称是不同概念。

上面的实际测试是本 skill 的来源，不表示未来接口永久可用。通用脚本还需离线验证；涉及实际账号的验收必须处于用户已授权的检索任务中。

不记录动态 star 数，也不把这两项开源项目称为小红书官方接口。依赖升级后先检查帮助、响应字段、评论上限和停止行为。脚本使用的内部方法需要与上游版本一起维护。

## 路径与依赖

所有路径按当前机器实际情况选择，不能继续引用上一次任务的临时目录。将 skill 路径、运行目录、Python 和 Node 路径分开；运行证据目录不要放在 skill 文件夹里。

下面代码中的路径只是示例，执行前替换成真实路径。遵守当前环境的 RTK 约定；不覆盖 HOME 或 CODEX_HOME 等系统变量。

```bash
XHS_SKILL='/实际路径/xiaohongshu-research'
XHS_WORK='/当前项目/小红书检索-本次日期'
XHS_PY='/本地运行环境/venv/bin/python'
XHS_NODE='/本地运行环境/bin/node'
XHS_OPENCLI_ROOT='/本地运行环境/node/node_modules/@jackwener/opencli'
```

优先复用已安装且符合版本的运行环境。确需安装时，使用任务独立目录：创建 Python 虚拟环境，安装 `scripts/requirements.txt`；用 npm 在独立前缀下安装 `@jackwener/opencli@2.0.2`。此临时浏览器流程不需要扩展，不顺手全局修改 Chrome 或默认 Python。用户明确选择原 Chrome 扩展方案时，改按[固定配置连接](bridge.md)接入及确认权限。

可参考：

```bash
python3 -m venv '/本地运行环境/venv'
'/本地运行环境/venv/bin/python' -m pip install -r "$XHS_SKILL/scripts/requirements.txt"
npm install --prefix '/本地运行环境/node' --ignore-scripts '@jackwener/opencli@2.0.2'
```

只需要本任务小红书适配器时不必运行可选的安装后下载脚本。安装后检查指定 OpenCLI 目录内的 `dist/src/main.js`、`dist/src/browser/cdp.js` 和 `clis/xiaohongshu/comments.js` 确实存在。

## 有授权的 API 路径

`init` 创建全新证据目录；已有目录不会覆盖。`login` 只在已获指定账号凭据读取授权后执行。仅按显示名称匹配一个 Chrome 配置，名称歧义会停止。`--chrome-root` 可覆盖默认 macOS 路径；其他系统须先核实工具兼容性。

```bash
"$XHS_PY" "$XHS_SKILL/scripts/xhs_research.py" --run "$XHS_WORK" init
"$XHS_PY" "$XHS_SKILL/scripts/xhs_research.py" --run "$XHS_WORK" login --profile-name '用户指定的配置名'
"$XHS_PY" "$XHS_SKILL/scripts/xhs_research.py" --run "$XHS_WORK" search '城市 机构 正畸' --pages 2
"$XHS_PY" "$XHS_SKILL/scripts/xhs_research.py" --run "$XHS_WORK" search '人物全名 正畸' --sort latest --pages 2
```

搜索不会自动打印全部正文；在 `catalog.json` 选候选并阅读 `raw/search-*.json`。搜索中观察到的 token 与 `pc_search` 绑定保存。

命令行的 `latest` / `popular` 必须映射为底层接口的 `time_descending` / `popularity_descending`；直接传别名可能返回结果却未按预期排序。记录同时保存显示选项和 `api_sort`，缓存键使用实际接口值。不要把旧版错误传参的结果当成有效排序对比。

```bash
"$XHS_PY" "$XHS_SKILL/scripts/xhs_research.py" --run "$XHS_WORK" read '已观察的noteID'
"$XHS_PY" "$XHS_SKILL/scripts/xhs_research.py" --run "$XHS_WORK" comments '已观察的noteID' --pages 3
```

也可传从网页得到的完整小红书笔记 URL；保留 `xsec_token` 与 `xsec_source`，不编造 token、不把所有来源强制改为 `pc_search`。访问上下文可能过期，失败应记录，不无休止换 token。

如同一作者的相关前文可能解决指代问题，从已读来源中取得公开 user ID：

```bash
"$XHS_PY" "$XHS_SKILL/scripts/xhs_research.py" --run "$XHS_WORK" author-notes '已观察的公开userID'
```

旧命令默认只读第一页，可用 `--pages` 扩展；项目级 author 任务会保存游标并继续公开列表。返回上下文按 `pc_user` 保存。先按主题筛选，只精读相关笔记，不扩成整个人的背景调查。

缓存按查询、排序、页码或笔记记录。将 `--pages 2` 增加到 `--pages 4` 会复用前两页并继续；再次传相同参数复用本次缓存。新日期或需要刷新证据时新建运行目录，不混淆不同时间的快照。

## 楼中楼路径

如果已有充分授权的连接浏览器/OpenCLI Browser Bridge，可直接使用其只读能力，无需额外复制凭据。保存返回数据与覆盖限制。无此能力、但已获临时登录副本授权时，用下面的独立浏览器路径：

```bash
"$XHS_PY" "$XHS_SKILL/scripts/xhs_browser.py" --run "$XHS_WORK" start \
  --opencli-root "$XHS_OPENCLI_ROOT" --node "$XHS_NODE"
"$XHS_PY" "$XHS_SKILL/scripts/xhs_browser.py" --run "$XHS_WORK" comments '已观察的noteID' --limit 50
"$XHS_PY" "$XHS_SKILL/scripts/xhs_browser.py" --run "$XHS_WORK" deep '已观察的noteID' \
  --roots 150 --reply-clicks 120 --budget-seconds 150 --resume
```

默认 Chrome 路径仅适用于 macOS，可传 `--chrome` 指定可执行文件。程序为本次运行新建独立浏览器目录，从该目录的 DevToolsActivePort 获取本机端口；不重启原 Chrome，不修改原登录状态。

研究标签页按 Chrome target ID 固定绑定，不再由 OpenCLI 从调试列表选择第一个页面。首次 `start` 只接受本次新建浏览器中唯一的空白/公开研究页，并在 Cookie 初始化前绑定；连接和初始化都使用这个标签页的专属 WebSocket。每批核对本次 PID、专属浏览器目录、DevToolsActivePort、目标 ID 及同一 `127.0.0.1` 端口，随后只对绑定页做 URL 优先检查。

旧 run 未绑定时，唯一公开研究页可兼容绑定；若已有多个标签页，程序返回 `BROWSER_TARGET_BINDING_REQUIRED`。先只读检查该临时浏览器 `/json/list` 的 URL 与 ID，再明确指定已观察的研究标签：

```bash
"$XHS_PY" "$XHS_SKILL/scripts/xhs_browser.py" --run "$XHS_WORK" bind-target --target-id '已观察的研究标签targetID'
```

这个动作只读标签 URL 元数据并写本地绑定记录，不读取其它标签正文、不切换账号，也不关闭新开的作者主页。绑定记录保存在本次 run 的 `browser.json` 与 `diagnostics/browser-target-binding-*.json`。之后标签列表顺序变化不影响连接；已绑定目标消失、身份或地址无法确认时停止，不自动改用其它标签。绑定页进入作者/私信路径时返回已知 `NOTE_SCOPE_CHANGED`，不读该页正文；未知站点、验证链接或无法确认的访问状态仍保留 STOP。不能通过 `bind-target` 替换已有绑定来绕过目标丢失或访问限制。

上游 OpenCLI 2.0.2 的 `--limit` 上限 50，展开回复还受轮数和按钮文字去重限制。本封装的两个入口均调用 `deep_comments.mjs`，复用其字段解析，但采用有界滚动、重复展开和立即停止访问限制的逻辑，不使用上游的限制后自动重试。

`comments` 保留 1–50 楼层的简便参数；`deep` 的 `--roots 1..200` 是单批实际处理的父评论预算，不是整个帖子最多可读的楼层数。`--reply-clicks 1..120`、`--budget-seconds 10..150` 也只约束本批。深查使用 `--resume`：再次运行相同参数会接着未完成楼层处理，可以跨批越过第 200 楼，不靠加大预算才能重读。当前 DOM 中所有已加载楼层及可见回复都会进入快照，未处理楼层另列。

默认进度保存在 `progress/browser-<noteID>.json`；项目级续跑通过 `--progress-path '/项目/browser-progress/<noteID>.json'` 指向独立于临时登录会话的进度文件。该文件可包含 `expectedRootIds`、`apiRootsComplete` 和 `expectedReplyCounts`，分别记录 API 已发现一级评论的真实 ID、一级评论分页是否结束及各楼已知回复数。只用采集证据填写，不能为触发完成而假设这些值。旧的非 `--resume` 调用仍保留参数缓存行为。

滚动只把真实容器移到底部，不随后把末条评论移入视野；每轮异步新楼层最多等待 6 秒，并在等待中留存新出现的行。回复展开后的新楼层会加入后续队列。连续回复展开至少间隔 3 秒，仍不保证不会限流。这些都是读取预算，不用于绕过平台限制。

检查 `coverage` 的 `rootsRendered`、`rootsProcessed`、`rootsInDomAtEnd`、`remainingThreads`、`threads`、`rootPaginationState` 及预算字段。逐楼状态包括 `not_processed`、`pending`、`stalled`、`visible_replies_read`；快照中的 `previously_read` 仅表示沿用既有处理记录。

`rootPaginationState=complete` 只在明确的评论区末尾标识、已知 API 一级评论 ID 全部出现，或页面明确零评论时成立。没有末尾标识、没有可靠 API 一级评论清单而滚动不再增长，应记为 `stalled`，不能靠平台总数相等或没有展开按钮宣布完整。总体进度状态为 `visible_scope_complete`、`pending` 或 `stalled`；只有一级楼层范围已核对、所有已发现楼层已处理、没有缺失 ID 或身份歧义时，才是当前可见范围完成。`missingExpectedRootIds`、`unresolvedRootKeys` 必须进入缺口报告。`platformCommentTotal` 仅用于提示差异，可能包含删除、折叠或媒体评论，不是可靠的已读分母。

搜索的可选预热/推荐接口实测会返回 `success:false/code:0/msg:成功` 或 `code:3002/msg:方案执行结果为NULL`。仅对指定可选接口的这两个精确响应保留上游容错；真正的搜索失败和所有登录、验证、限流错误仍停止。不要将任意错误一律忽略。

`No inspectable targets` 表示没有可用浏览器页面，不是没有评论。检查本次临时窗口是否被关闭；不要因此重启用户原 Chrome。登录墙（包括“登录查看全部评论内容”“登录查看更多评论”等底部提示）、验证、限流则在任何回复点击前写入停止标记，停止所有自动采集。不要自动删除停止标记重试。

后台遮挡会使计时器和评论加载暂停，导致读取超时。深读脚本先检查访问状态，再仅对本次临时页面启用 DevTools 焦点保持，完成后释放；不操控原浏览器、不改变登录或网络，也不用于处理验证/访问限制。恢复后仍不可见则记录 `BROWSER_BACKGROUND`，停止该次读取；超时不是零评论。

读取中出现 `Inspected target navigated or closed` 等上下文中断，或整批进程超时，访问状态可能已改变。此类 `ACCESS_STATE_UNCONFIRMED` 写入停止标记并保留原进度；不能立即改用 API 继续采集，只能先只读检查当前地址/提示。平台明确限制后结束本轮并清理。一级评论已由 API 分页读完而浏览器楼层更少时，写明缺失楼层，不因可见展开按钮清空而宣称完整。

每个 `--resume` 批次保存新的不可变快照，进度累计所有已观察的真实评论 ID 及逐楼状态；较小的后续快照不会删掉此前覆盖。无真实 ID 的行保存 `identityKind=uncertain_fingerprint`、来源指纹与快照位置，不把指纹当平台 ID，不跨快照强行去重。API 与浏览器只有真实 ID 等可靠依据一致时才能合并，否则分别报告，不能直接相加。回复对象仅有昵称时保存该限制，不升级为确定身份。读取成功与当前可见范围完成是不同状态。

展开前后及每次采集前均核对目标笔记 ID。连接时先只检查 URL；若已进入私信等非笔记路径，不读取该页正文。仅点击不属于链接、作者区或私信/互动控件的可见回复展开器；有歧义的控件记为 `unsafe_expander_not_clicked`，不擅自尝试。

已知站内页面变化（如离开笔记进入私信）返回 `NoteScopeChanged`／`NOTE_SCOPE_CHANGED`，保存此前笔记的部分证据并结束本批；它不等于平台限制，不写账号永久 STOP。代理必须先只读确认范围及原因，再显式返回已授权笔记并重试，不能自动重试或切到 API。未知跳转、访问状态无法确认、验证码及限流仍按原停止规则处理。

`stalled` 楼层在项目自动批次中通过 `progress.retryStalled=false` 暂缓，其他未处理楼层继续；只有明确复核后重试才设为 `true`。手动 `deep --resume` 默认允许重新尝试前次普通加载停滞，单批无增长点击次数仍有上限；任何 STOP 都不因此解除。

历史上已见全部一级评论不代表新批次已经到达待查楼层；续跑会继续滚动，直到本批访问过仍待处理的楼层或触发预算/停滞。界面暂显零评论不能覆盖历史待查楼层。首次展开完成与后续迟到回复采用同一核对标准：真实根评论、正文或图片位置均已采集、回复真实 ID 去重数量达到已知预期数，且没有安全或不安全展开器。预期数或身份不确定、内容未捕获、唯一回复数不足时保留缺口；不能只按 DOM 行数或平台总数判定完成。

### 纯表情、昵称前缀与旧进度核对

纯表情可能以图片呈现，`textContent` 为空。只有当前评论内容节点内实际渲染、可见的图片标签才可补充文字：依次检查实际 `alt`、`aria-label`、`title`，且必须是可识别的表情标签；来源分别保留为 `rendered_emoji_alt` 或 `rendered_emoji_accessible_label`。不从图片 URL、文件名或 API 的同条文字猜出网页表情，也不在已有正常文字后擅自拼接标签。可见 `img.note-content-emoji` 的真实 `currentSrc`／`src` 单独保存为 `renderedInlineImages`；缺少标签时保留 `media_placeholder`，不编造文字。没有任何可见标签或真实图片来源时，仍保留 `root_content_not_rendered` 或 `reply_content_not_rendered`。

“回复 某昵称：”可能只有平台引用前缀，没有回复正文。仅在实际 DOM 中存在直接昵称包装、其前后节点明确构成空引用前缀时，才将其识别为正文缺失；普通用户写出的相同句子不会按字符串规则删除。嵌入表情有真实有效标签时可采集文字，有可见原图来源时可采集媒体；两者均无则不输出伪正文覆盖旧记录。回复对象、原显示前缀与真实评论 ID 保留在行记录或 `currentReplyBodyGaps`／`replyBodyGaps` 中。`currentReplyBodyGaps` 描述本批未呈现的正文，`replyBodyGaps` 描述仍无可靠历史正文可承接的缺口。已有媒体来源只能在真实评论 ID、父楼及公开 UID 均匹配时承接较小快照。

评论条目可见、真实 ID 数量达到预期、没有更多展开按钮，都不等于正文已经齐全。检查逐楼 `replyContentCaptured`、`replyBodyGaps` 和停止原因；不要只比较 `visibleReplies` 或 `uniqueObservedReplyCount`。普通图片评论保留真实图片位置与 `media_placeholder`，这只是媒体已采集，仍须进入图片阅读与归属分析流程，不等于已读懂内容。

未被完整采成有效可见标签文字的内联表情，按 `comment/<cid>/inline_emoji/<DOM序号>` 建独立媒体任务，不与普通配图的位置合并。纯表情已完整采得有效标签时不重复建立图片任务；已有正文导致标签未采入、或混合标签不完整时保留相应图片序列。同一个 URL 连续出现三次仍保留三个位置。采集完成后可继续下一楼，图片未下载、未阅读、不可读取或缺失任务仍留在媒体缺口中，并阻止评论任务被标为已读；`[图片评论]` 占位和 `visible_replies_read` 采集状态都不能充当视觉阅读。实际查看后保存原图／截图、来源绑定及文件哈希，不从表情推断情绪、身份或治疗评价。

`deep --resume` 会从当前进度明确链接的、同一 run 的原始 `browser-deep-<noteID>-*.json` 核对旧回复正文，不遍历其他会话或个人浏览器。只接受同笔记、成功的浏览器快照；拒绝符号链接、错位路径、API 记录及来源不符的文件。首次迁移为所读取的原始字节登记 SHA256，之后源文件哈希变化不再继承其正文凭据。只有真实评论 ID、父评论 ID 与公开作者 UID 能对应，且确有正文或已保存的实际评论图片，才建立 `replyBodyCaptures`；昵称、旧完成布尔值、空值和仅引用前缀均不是正文证据。哈希登记证明本次读取的字节，不能追溯证明首次登记以前的文件未被改动。

旧楼缺少足够可复核的凭据时会重新进入待查范围，包括旧楼本批尚未进入 DOM 的情况。`legacyReplyBodyIds` 与 `replyBodyMinimumCount` 保留已发现的真实回复和数量下界；优先使用唯一 ID 数量，重复 DOM 行不抬高下界。较小的新快照或暂时缺席的回复不能清除这些待查项。同 ID、同作者的真实旧正文可以承接当前较小视图；缺失来源、哈希变化或身份冲突则不能承接。旧原始快照保留，不将旧 `previously_read`／`visible_replies_read` 本身当作正文已核实的证明。

重新打开旧进度只是本地覆盖纠错，不会清除 run、项目或账号 STOP，也不授权重试登录、验证或访问限制。保持固定研究标签与原串行节奏；普通加载停滞仍按 `retryStalled` 的显式复核边界处理。报告应列明新增识别出的正文缺口及其来源，而不是把可见计数增长或历史状态纠正写成“全量已完成”。

## 完成与清理

在实际任务结束、用户中断或确认平台访问限制后执行清理；普通预算断点、Codex 审读、仍可修复的单项缺口保留同一会话。不要把所有 `blocked` 当作会话结束。若已无法继续并准备交付，则结束临时会话，保留研究队列。

```bash
"$XHS_PY" "$XHS_SKILL/scripts/xhs_research.py" --run "$XHS_WORK" summary
"$XHS_PY" "$XHS_SKILL/scripts/xhs_browser.py" --run "$XHS_WORK" cleanup
```

即使没有启动浏览器，也用同一个 `cleanup` 删除临时登录副本。它检查运行标记、浏览器 PID 和专属目录，只关闭自身浏览器，不执行 logout；证据和 `coverage.json` 保留。遇 PID 不匹配或浏览器仍未退出会保留浏览器目录并报告，不能声称清理成功。

运行结束后应有 `cleanup.json`。临时目录 `.session/`、`browser/` 不得进入分享包或版本库。不要复制整个 Chrome 配置、Cookie 数据库或本次研究原始资料到 skill 中。

记录一次正常退出和异常退出后的清理状态。若采集被中断，下次首先检查原运行的 `browser.json`、`STOP.json` 和临时目录，不再创建一堆无人管理的会话。

## 本地验证与维护

```bash
"$XHS_PY" "$XHS_SKILL/scripts/test_research.py"
"$XHS_NODE" --check "$XHS_SKILL/scripts/prepare_browser.mjs"
"$XHS_NODE" "$XHS_SKILL/scripts/test_deep_comments.mjs" "$XHS_OPENCLI_ROOT"
```

测试使用合成样本和模拟传输，不联网、不读真实 Cookie、不启动用户 Chrome。验证重复页去重、部分评论覆盖、访问上下文保留、预热阶段遇验证停止、接口白名单、账号域过滤与清理隔离。测试不证明小红书未来实时可用。

上游关键位置：`xiaohongshu-cli/xhs_cli/client.py`、`client_mixins.py`、`cookies.py`；OpenCLI 的 `clis/xiaohongshu/comments.js`、`dist/src/browser/cdp.js`。只在观察到接口变化时调整对应逻辑，不不断追加未经验证的“技巧”。
