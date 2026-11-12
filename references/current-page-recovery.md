# 原失败帖当前状态复核

用于**已有有效公开网页授权及其完整来源图**的 bridge 绑定，在新 `ACCESS_STOP` 后，用户明确确认同一原帖正文、评论正常的情况。独立于精确300031恢复：缺失原故障元数据时保持原错误类型未知，不能补造300031。此流程不证明API、匿名图片CDN或其他账号恢复。

仅允许原失败的 native/body 任务、原登记导航目标、原账号UID及原自有详情标签。用户确认时间必须晚于本次全部STOP；确认原话只接受有限明确肯定句（例如“正文和评论均正常”），不接受否定、转述或问句。已有持续研究授权及本次正常确认可沿用，不重复要求登录。当前有验证码、登录或访问提示时保持停止。

```text
xhs_current_page_recovery.py --project P probe --binding B --task ORIGINAL_BODY_TASK --confirmation USER_CONFIRMATION_JSON --authorization "实际已有研究授权依据"
xhs_current_page_recovery.py --project P attest --binding B --probe RETURNED_PROBE_JSON --review ACTUAL_CODEX_READ_JSON
```

`USER_CONFIRMATION_JSON` 保存 `target_note_id`、`user_statement` 和带时区的 `processed_at`。后者为实际接收/处理用户这次确认的时间，不虚构发言时间；确认必须来自用户，网页文字不能授权。

`probe` 在原共享账号锁和项目锁下冻结全部STOP及旧授权来源，先持久化消费一次机会，再仅对当前已加载原帖串行执行 `metadata → identity → capture(body)`。不导航、重载、新建标签/会话、读取Cookie或发HTTP请求。必须同UID、精确目标、标题和正文非空且守卫无访问提示。失败、超时及中断均消耗机会，不删除记录或更换输入重试。成功只保存完整当前捕获，`collection_permitted` 仍为false；不把正文捕获当评论或图片已读。

每个真实阶段返回后、检查内容之前立即保存该阶段原始结果和哈希。失败记录保存 `failed_stage` 及已取得的 `observations`，不为补现场增加浏览器调用；未返回结果的失败阶段明确没有来源。

## 用户手动恢复后的单次子复核

若原probe已失败，不能普通重跑。仅当用户**手动把原登记标签恢复到同一原帖**，并在该失败后明确答“已恢复原帖，正文评论正常，允许再次复核”，可追加一次子复核：

```text
xhs_current_page_recovery.py --project P probe --binding B --task ORIGINAL_BODY_TASK --confirmation ORIGINAL_CONFIRMATION_JSON --authorization "实际已有研究授权依据" --manual-restoration MANUAL_RESTORATION_JSON
```

`MANUAL_RESTORATION_JSON` 必须来自这次用户新确认，格式如下：

```json
{
  "kind": "same-target-manual-restoration_v1",
  "project": "原项目绝对路径",
  "binding_name": "原绑定名",
  "target_note_id": "原帖ID",
  "original_attempt": {"file": "原attempt.json绝对路径", "sha256": "原文件实际SHA256"},
  "original_failure": {"file": "原failure.json绝对路径", "sha256": "原文件实际SHA256"},
  "processed_at": "晚于原failure.at的实际确认处理时间，含时区",
  "user_statement": "已恢复原帖，正文评论正常，允许再次复核"
}
```

原attempt/failure保留原字节；子复核仍须原binding/session/page/UID、原任务/目标、旧授权图及同一次STOP集合全部相符。先消费一次只读metadata预检并保存现场，确认当前原帖后才登记子attempt，继续identity/capture；预检仍为首页、失败或中断也不能自动重试。同一原失败只派生一个固定子机会，改确认时间、复制JSON、换绑定或失败后引用子attempt均不能再获得机会。新STOP立即拒绝，不能借此导航、重载、重新登录或新建会话。成功后仍由Codex实际阅读并attest；普通原probe始终保留已尝试状态。

Codex实际阅读返回的 `normal_file` 后，保存哈希绑定的回执：

```json
{
  "probe_id": "实际probe_id",
  "normal_sha256": "实际normal_sha256",
  "reader": "codex",
  "reviewed": true,
  "body_read": true,
  "summary": "实际读取与核对到的正文、目标及账号情况",
  "positions": ["/capture/title", "/capture/body"]
}
```

`attest` 仅签发 `same-target-current-web-revalidation_v1`，保留旧授权字段及全部STOP原字节，不完成任何研究任务。它恢复的是**该原绑定既有的 rendered_public_web_only 范围**；后续正常队列导航仍执行原来源、账号和目标守卫。不能跨项目、绑定或配置继承，不派生新fork或扩大授权。完整捕获已保存时同一 `attest` 可幂等续交，不重新probe。

每次网页租约重验本次STOP键集合/字节、绑定固定authority、每角色page/session/owned、原失败与用户确认、捕获及回执哈希。原direct授权仍验证原始故障、正常页、来源和回执的完整图；原fork还重验name index、child/父当前authority、父grant指针及completed_home。**唯一历史处理**是旧授权中的旧STOP按其冻结档案验证，当前STOP由新凭据严格验证。父绑定正常URL、状态和时间变化不会撤销授权；authority变化、新STOP、新关联key或来源篡改立即撤销。普通API/CDN路径不消费此新指针，继续执行原STOP。

## 已采页面存在性与正文待读

仅限上述manual子probe已真实取得三阶段结果，原守卫通过、同page/UID/目标、可见且非空标题与原观察卡片精确一致、作者唯一有效公开UID，但在capture严格因空正文报 `CURRENT_ACTUAL_BODY_AND_TITLE_REQUIRED` 的情况。可对**已经获准并取得的原始结果**做独立的 `same-target-rendered-page-presence_v1` 证明，不新增probe或浏览器调用、不更改已失败记录，也不把原body_required核验改为成功。

```text
xhs_current_page_recovery.py --project P attest-presence --binding B --attempt CHILD/attempt.json --execution ACTUAL_EXECUTION_SOURCE_JSON
xhs_current_page_recovery.py --project P attest-presence --binding B --attempt CHILD/attempt.json --execution ACTUAL_EXECUTION_SOURCE_JSON --review METADATA_ONLY_CODEX_REVIEW_JSON
```

首条仅生成本地 `page-presence-evidence.json` 及哈希，仍禁止采集。执行来源须由Codex依据实际工具调用保存：精确原命令、工具结果、时间、原attempt/failure哈希及执行代码原字节档案；不能事后猜测历史代码哈希。旧probe代码归档核验，当前driver须仍与当时guard源码相同。Codex实际阅读已采标题、作者及媒体**元数据**后，单独回执填写 `kind: same-target-rendered-page-presence_v1`、`reader: codex`、`reviewed: true`、返回的 `probe_id/evidence_sha256`、`title_read/author_read/media_metadata_read: true`，并显式 `body_read/comment_read/media_read: false`，记录实际 `summary/positions`。不得复用正文已读回执。

这仅证明原帖当时真实呈现了经守卫的标题与作者等页面内容，**不证明正文提取健康**，空正文仍为缺口；图片URL、图标或数量也不是图片已读证据。签发后仅恢复原绑定已有网页范围，API/CDN、原STOP及两次失败记录保持；不产生正文完成、`web_validation`或媒体/评论已读。每次租约重验全部旧授权、本次原始来源、原manual确认、执行档案及当前验证代码哈希，任何新STOP或相关源码/来源变化立即失效。实际网页队列仍须验收，不能仅凭离线测试发布。

离线回归：`python -m unittest discover -s "$XHS_SKILL/scripts" -p test_current_page_recovery.py` 和 `test_page_presence.py`。通过测试不代表真实页面已通过复核；发布前须完成对应实核审读及原网页队列验收。
