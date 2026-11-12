# 已采集范围的人工审读凭据

`observed-review` 用于一个仍为 pending/blocked 的评论任务：已有材料确实读完，但未采回复或解析器缺口仍须保留。它只在 `reviews` 中新增独立凭据，不修改任务 `status`、`review_status`、线程、STOP 或剩余任务数量，不等于通用 `reviewed`。

## 先冻结材料，再实际阅读

```bash
"$XHS_PY" "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" \
  observed-review-plan --task '实际browser或comments任务ID'
```

返回清单覆盖该笔记当前已保存的全部一级评论和回复文档，包含真实文档 ID、公开作者 UID、正文与上下文 SHA256，以及关联原始快照的 SHA256。只支持稳定平台评论 ID；身份不足、同一 ID 对应不同 UID、原文来源缺失/篡改或只剩快照局部身份时拒绝生成凭据。API 与浏览器的同身份文字版本可以同时核对，但不会因此把 API 来源当作网页正文已呈现的证据。

这个清单是阅读计划，不是已读记录。阅读所有当前文字、父评论与必要上下文后，保存计划到项目中的 JSON，填写明确的 `reviewer`，将 `all_current_documents_read` 设为 `true`。不要删改 `documents` 来避开不想读的条目；完整集合、UID、文字/上下文和全部关联来源必须仍与数据库一致。计划生成后有新增或变化内容时，重新生成并补读变化。

本接口只证明已冻结的评论文档文字与明确列出的视觉例外已读，不证明所有历史文字版本、其他配图、未采回复或医生归属。普通媒体继续使用媒体模块的逐项审核和报告；不要把 `observed_scope_reviews` 的数量加入完整 `reviewed` 计数。

## 空正文和视觉例外

`requires_visual` 列出空正文、仅引用昵称前缀及图片占位文档。这些条目不能仅凭计数匹配标为已读；须引用已实际查看的截图，或下述严格绑定的原始图片审核记录。例如：

```json
{
  "visual_observations": [
    {
      "document_id": "comment:实际noteID:实际commentID",
      "audit_path": "/本项目/审核记录/该评论截图审核.json",
      "audit_sha256": "该审核JSON文件字节的SHA256"
    }
  ]
}
```

审核 JSON 须有非空 `reviewer`、`visual_observation.body`，并包含这些可验证字段：

- `screenshot`：`file`、`sha256`、`actual_view_image: true`、`note_id`、`comment_id`、含时区的有效 `captured_at`。
- `raw_automation_source`：当前 `document_id`、真实 `user_id`、已关联的 `snapshot_id`。
- `capture_source_record`：截图绑定记录的 `file` 与 `sha256`。绑定记录的 `screenshot.path/noteId/commentId` 必须匹配，`capturedAt` 须与审核记录的截图时间一致；`structure` 中该 `commentId` 的唯一记录须为 `matchCount: 1`。

截图、绑定记录及审核文件必须位于当前项目内，不能使用符号链接。凭据保存文件哈希及具体对象，不改写原快照，不从表情外观推断官方名称、医生身份或医疗经历。其他已看过的图片不能代替这个评论的实际截图。纯表情已有真实 API 文字且当前文档保存了该文字时，可按该来源审读；网页解析器是否取得表情仍由采集状态单独说明。

### API 原始图片组成的空正文

`visual_observations` 保持相同的 `document_id/audit_path/audit_sha256` 引用格式。另一种审核文件可使用 `format: xiaohongshu-api-comment-images-review-v1`，仅用于原始 `comments` 快照中正文为空字符串或纯空白、含非空 `pictures/images` 的真实评论。浏览器截图的原校验不会被此分支放宽。

该审核文件包含：

- `reviewer`、`note_id`、`comment_id` 和具体 `observation`。
- `raw_api_source`：`document_id`、稳定 `user_id`、源 `snapshot_id`、同值 `sha256`、当前完整 metadata 的 `context_sha256`。验证会回原快照唯一定位评论，并比较正文、UID、父评论关系和当前 metadata；不能继承楼主身份。
- `images`：原评论的**全部**图片槽，每个保存 `media_id`、`locator`、`resource_fingerprint`、项目内 `manifest_path`、`acquisition_sha256`、下载 `file/sha256`、`actual_view_image: true`、`review_status: reviewed`、实际 `viewed_at` 和具体图面 `observation`。只有确实查看完成的图片才能填 true；partial/unreadable 继续是缺口。

`acquisition_sha256` 是 `sha(canonical(acquisition_binding(manifest_item)).encode())`，对应 `xhs_observed_review.py` 的同名函数。它只冻结该图片的下载、原始来源及资源字段，其他图片的 manifest 更新不会使凭据失效。验证还会回到 manifest 的原快照核对实际观察 URL、CID/UID、资源指纹、当前下载文件及字节哈希；不能用任意本地图片替换。所有路径必须在本项目内且无符号链接。

缺少任意图片、图片缺 URL、ID/UID 不足、资源或文件变化、当前 metadata 改变、只有回复前缀、无图片的空正文均拒绝通过。配对 Live Photo/视频不能仅用静态图片完成此审读；保留其视频核查缺口。已有人工图片回执可明确注明原审核者和来源哈希后转换格式，不能把下载成功或自动生成的字段当作实际看图。

此分支仍只验证所提交的已采范围凭据，不会自动写入审核、改变 `status/review_status`、完成其他空正文、补采楼层或推定医生归属。提交整帖已采范围凭据时，同帖其他无图空正文仍需分别解决。

截图时间还必须不早于该文档最新关联原始快照的采集时间；时间缺失或不明则拒绝视觉替代。同 ID 后来再次采集到空正文或前缀时，不能生成新凭据后继续沿用更早的表情截图，因为实际图片可能已经改变。

### 浏览器原始内联表情图片

已有成功 `browser_comments` 原始快照，且可见 `renderedInlineImages` 的每个原图都已下载并实际查看时，可用 `format: xiaohongshu-browser-inline-images-review-v1`，无需为同一批原图虚构或重新拍截图。它保留 `visual_observations` 的原引用接口，与 API 图片分支共用下载来源、实际 URL、manifest、资源及文件哈希校验。

- 顶层包含 `reviewer`、`note_id`、`comment_id`、具体 `observation`；`raw_browser_source` 包含 `document_id/user_id/snapshot_id/sha256/context_sha256`，另明确 `parent_comment_id`（根评论为空字符串）。原始数据数组须唯一匹配真实 CID，当前文字和全部 metadata 哈希须相同；回复须有真实父楼 ID。
- `images` 仍使用上节的逐图字段，并增加正整数 `dom_ordinal` 与当前 `resource_version`。每个实际 DOM 槽必须恰好覆盖一次；同一个 URL 出现三次仍是三个位置。下载来源的 CID、UID、父楼和回复对象必须一致，不能用其他帖、同昵称、旧资源或任意本地图替代。
- 本格式仅处理 `note-content-emoji` 内联图片。缺来源、不可读、缺槽、普通配图混入、未知身份、资源/文件/版本变化、未实际看图或只有占位断言都不通过。包含普通配图时保留该额外范围，不用内联格式假装全部完成。

已有真实逐图回执可以保留原审核者和回执文件哈希后转换字段；必须逐项对齐原图 SHA、资源版本、源 SHA 和实际查看时间，不把转换程序当作阅读者。结果仍为 `whole_task_reviewed=false`，不会改任务、消除“展开”文字缺口、补全其他楼层或推定表情情绪/医生归属。整帖凭据的 `all_current_documents_read` 继续由实际文字与上下文阅读决定，不能因这几张图通过而自动设为 true。

### 浏览器普通评论配图

普通 `images` 图片组成的评论（包括 `[图片评论]` 或仅回复前缀）使用独立的 `format: xiaohongshu-browser-comment-images-review-v1`，不套用 API 或内联表情格式。它复用上述 `raw_browser_source` 的真实 CID、UID、父楼、当前文字/metadata 和原快照绑定，以及项目内 manifest、下载文件 SHA 和实际阅读校验。

- `images` 必须覆盖原评论普通图片数组的全部位置；逐图字段与上节相同，但以正整数 `image_ordinal`（从 1 开始）代替 `dom_ordinal`，并显式保存下载项的 `observed_url` 和当前正整数 `resource_version`。同 URL 出现多次仍分别保存每个位置。
- 每个图片必须已有实际完整阅读记录，且 manifest 的当前 `review_status` 为 `reviewed`；`pending/partial/unreadable`、资源缺 URL、身份不足、来源/父楼/回复对象变化、图片文件或版本变化均不通过。缺姓名或遮挡内容不能补写；只登记可读部分的图片仍保持 partial。
- 该格式仅处理普通配图；同时包含未处理的内联图片、配对 Live Photo 或视频时拒绝将它们混算为完成。它不需要伪造截图，也不认证尚未展开的文字、其他评论或整帖完成。

已有真实原图回执可以注明原审核者、回执 SHA 和具体图片字段后转换；转换程序不能替代看图，也不能为满足协议把复用其他文件的阅读写成再次打开当前文件。无法提供准确实际阅读断言时继续保留缺口。

整帖通用 `reviewed/relevant` 还会核对所有当前及累计保留的评论媒体：API/网页普通图、内联图片和配对视频均须完成或有明确排除。漏建队列、partial、当前文件/资源/manifest 绑定失效都挡住整帖完成；文字的 `observed-review` 仍可独立登记。采集或导入新媒体会使受影响的旧整帖已读状态回到 pending，历史回执保留。尚未重新导入的文件变化也会在只读 `status/report` 中列为有效待办，不把旧数据库状态继续当成当前完成。
旧评论没有真实 CID 时保留 `source_bound_comment_media:…:unknown_comment_identity`，不脱离原始快照和数组位置新造媒体 ID，也不把该身份缺口描述成已确认漏建的图片任务。

## 登记与后续变化

```bash
"$XHS_PY" "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" \
  observed-review --task '实际任务ID' --receipt '/本项目/已读凭据.json' \
  --summary '只说明本次读完的已采集范围；列出仍未采或解析器不能读取的内容'
```

重复相同凭据和摘要不会重复计数。`status` 和 `report` 将结果放在独立的 `observed_scope_reviews` 中：`current` 只代表这份已采集范围凭据仍与原文一致；有新增文档、来源、正文、身份/上下文或截图变化时显示 `stale`，不能继续当作当前范围已读。通用 `review` 不能代替这一校验，也不会因为凭据登记而解除 STOP 或重试受限请求。

本地可验证出处与哈希，无法替代实际阅读判断。所有“已读”断言必须来自确实阅读过对应材料的分析者；不能把自动生成计划直接改标为已读。
