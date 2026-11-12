# 项目级 v2：覆盖优先与持续续跑

## 项目和会话

项目长期保存 SQLite 队列、不可变原文快照、语义审核和报告；run 只保存一次账号登录与浏览器会话。清理 run 不删除项目，也不把待办标为完成。旧 run 即使 closed=true 仍可导入。不要导入 .session、browser、Cookie 或整个 Chrome 配置。

所有命令以实际 Python 和 skill 路径调用并遵循 RTK。下文 XHS_PROJECT 为研究目录、XHS_RUN 为本次可用会话、XHS_SKILL 为 skill 目录。同一任务普通批次复用会话；恢复工作先按[自主运行与故障分流](autonomy.md)检查，避免重复要求登录或逐批确认。

## 建立或续接项目

配置 JSON 的最小形式：
```json
{"target":"上海某医生正畸体验","queries":["全名 正畸","机构 姓医生","分院 正畸","机构 复诊","机构 正畸 后悔"]}
```

查询由 Codex 结合任务和已观察线索制定，不由脚本猜测。plan 对每个查询自动加入 general/latest/popular 三种排序；再次 plan 可追加查询，不能改变同一项目的目标。
已有矩阵不会因搜索适配升级而批量重建；general 原页可续，latest/popular 只对选定组合以 add 建独立新方法页链。旧采集保留，逻辑覆盖仍按 query+sort 计数，详见[搜索方法兼容](search-method.md)。

```bash
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" plan --config '/查询配置.json'
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" import '/已有研究run'
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" status
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" next --limit 20
```

import 按内容哈希导入 raw 快照，保留原采集日期和来源。首次导入不推断旧报告哪些结论已人工审核。要继承已有判断，阅读可追溯原文和旧审核记录后显式 review/evidence；不能批量把所有导入项标为已读。

## 工作循环

1. next 查看下一项或待阅读内容；status/report 查看整体缺口。原文在项目 snapshots 中，SQLite documents 提供中文子串检索。search-local 只用于定位，仍须读上下文，不能只看命中句。
2. 用原 init/login/start 准备新 run，已有账号授权不重问。resume 每步采一页、一个正文或一批浏览器回复。--steps 是本批预算，结束后继续 resume；--task 可优先补特定缺口，--kind 可选择阶段。
3. 用 review relevant 或 review include 纳入范围并创建一级评论与浏览器回复任务；两者都不把正文标为已读。正文文字读完可另记 text_review，图片、实况照片及视频仍留待核。作者主页发现的笔记需要先主题筛选，避免采集无关生活正文。广告正文不等于其评论无研究价值。
4. 一级评论先分页，浏览器任务再按真实父评论 ID 和预计回复数量续读。超过 200 楼通过多批推进。原浏览器保护和访问停止规则始终有效。
5. 按主题追溯作者，处理全部相关图片与视频，记录审核和证据。新查询/来源继续 add 或 plan 入队。
6. report 输出所有未完成项；普通预算结束继续推进，而不是结束研究。访问限制必须停止，离线阅读已有资料可以继续。

```bash
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" resume --run "$XHS_RUN" --steps 5 --kind search
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" resume --run "$XHS_RUN" --task '实际task_id' --steps 1
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" review --task '实际task_id' --decision relevant --summary '已读正文，治疗地点及主题相关，继续核查所有评论'
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" search-local '待查姓名'
```

add 接受 kind、--key、--payload JSON 和可选 --priority。author payload 为 {"user_id":"已观察的公开ID","provenance":["实际来源"]}；note/comments/browser 为 {"note_id":"已观察ID"}，新 note 须同时保留已观察完整 URL 或 token/source。search payload 必须包含 query、sort 和来源；通常优先 plan 自动生成三排序，避免不一致。不要编造访问 token。

## 审核与证据

已采集文字确实读完，但评论任务仍有未采回复或解析器缺口时，使用[已采集范围人工审读凭据](observed-review.md)的 `observed-review`。该记录单列于报告，不修改采集状态、全量 `review_status`、STOP 或剩余数；后续原文变化自动显示 stale。新增或变化的评论会重新打开同帖受影响的评论/网页审核；原审核历史保留，重复导入同一快照不重置状态。

正文 `reviewed` 要求正文及其媒体均已读或明确排除；明确“只读文字”的 annotations 不能标成整篇完成。程序从保存的当前正文重新枚举媒体，检查遗漏任务、资源变化及未完成媒体；评论配图不作为正文审核门槛，由评论审核独立处理。文字已读、整篇已读和范围纳入是不同状态。

旧审核记录若曾将只有文字已读的正文标成 `reviewed`，`status/report` 会按当前媒体缺口计为有效待审，并列出原存储状态及原因；不改写原审核历史。已审图片还须保有非空原文件，且 SHA-256 与保存记录一致，文件丢失或变化同样保留缺口。每次报告重新核对，一次报告内复用文件校验结果。明确排除的媒体不因此重新打开；补齐并实际阅读后重新生成报告。

采集状态和 review_status 独立。对正文使用通用 reviewed 也会加入评论与浏览器任务；明确不相关必须 exclude 并给依据。媒体审核仅走媒体模块再 import-media，通用 review 不接受媒体的 reviewed/relevant。遇 stale_manifest_items，重新 inspect 最新原文后再同步，不导入旧文件覆盖新资源状态。reviewed 必须对应实际已阅读材料；relevant 是纳入范围；exclude 必须说明地域/主题等排除依据，不能用“没有姓名”简单排除潜在评论；blocked 留下缺口。retry 只能恢复普通技术失败，不能清除项目或账号 STOP。

旧版任务可能缺少 `resource_id_hint/resource_fingerprint`，而新 manifest 从同一原始快照提取了这些字段。`import-media` 只对尚未采集的旧项，在来源为同一已注册项目快照、快照 SHA-256 未变、媒体位置/时间/原地址逐项一致时补齐身份，并返回 `reconciled_legacy_media_items` 数量；无法证明的差异继续列入 stale，不能仅凭相同位置合并。

明确的媒体研究范围 `exclude` 以最新项目审核记录为准；后来的 pending manifest 不撤销排除，也可据此恢复旧版本误覆盖的状态。新的 retry 等决策可取消旧排除。资源更换仍清理旧文件与审核内容；保留的是研究范围排除，不代表新图片或视频已读。同一资源此前的媒体 `irrelevant` 也不会被 pending manifest 自动重置，但新资源必须重新判断。

evidence --file 接受带 summary、attribution 和 sources 的 JSON。attribution 为 explicit/context_supported/insufficient/other_target。sources 引用实际已导入的 snapshot_id 或 document_id，并补充评论 ID、图片序号或时间戳等位置。context_supported 必须填写 missing_confirmation。保留前后反证、既有症状和后续改善；不把置信等级写成临床因果。

媒体先用 xhs_media.py inspect 生成项目 media/manifest.json，按媒体说明下载、视觉核对或转写。再运行：
```bash
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" import-media "$XHS_PROJECT/media"
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" report
```
图片成功下载、视频成功转写均不等于已审读。partial、unreadable、未听校姓名等保留待办。

### 范围纳入与证据后续

`review --decision include` 仅适用于未排除的 note 任务：清除标题初筛标记、保存范围决定并创建评论/浏览器待查项；保留原 `status/review_status/reason`，不会清除 STOP。被排除的任务须先明确重新审议；不要把不同地区、不同账号的帖子因昵称相近纳入同一病例。

```bash
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" review --task '实际正文任务ID' --decision include --summary '同一公开账号的相关正畸记录；配图待读，先查评论'
python "$XHS_SKILL/scripts/xhs_project.py" --project "$XHS_PROJECT" link-evidence --earlier '较早证据ID' --later '后续证据ID' --relation resolves_gap --summary '仅解决该楼回复采集缺口；医生身份及同帖其他楼层仍未确定'
```

`link-evidence` 可记录 `resolves_gap/updates/contradicts`，关联两项已存在证据及其当时内容哈希。它不覆盖原证据，不自动完成任务，也不自动提升归属。报告中的 `current_sources_match=false` 表示关联后来源被更新，必须重新核对关系。报告和下一步规划应结合后续关联解释旧观察，而不是只看早期的 `missing_confirmation`。

大量续跑优先使用[连续批次](continuous-run.md)。访问受限后的新一轮只按[恢复核查](access-recovery.md)处理，普通 `retry` 不承担解锁功能。

## 覆盖和故障

- complete：本项采集达到已观察的结束条件；仍要看 review_status。pending：有下一批或待筛选。blocked：已记录原因，仍是缺口。excluded：有明确理由不属于本次范围。
- 搜索/作者/一级评论只有 has_more=false 才视为该列表结束。重复页、缺游标、空页但仍有更多、排序不确定均记录未完成。
- 浏览器累计观察与原始快照同时保留；旧 prefix 快照不升级成全区完成。真实 ID 去重；没有 ID 的行保留来源特定观察，不冒充不同患者人数。
- status 的分母是已发现任务，不是平台所有内容。不会生成全站召回率。所有采集完成但仍有语义未读时，项目仍为 work_remaining。
- 同账号共享本机锁和 STOP。研究项目与 run 各自保留停止记录，不通过新建项目/会话绕过。程序未提供自动清除限制命令。
- 中断后的 immutable 响应可重新 ingest；断点与证据导入应幂等。原快照不能人工改写以“修复”进度。
- 每次实际会话结束执行 cleanup，并核对 cleanup.json。研究报告、索引和项目可保留；发布 skill 不包含患者原文和凭据。
# Explicit empty terminal pages

Search, author and comment APIs may return only `{"has_more": false}` at the end. A missing or null result list is accepted as an empty page only with the literal Boolean `false`. Missing pagination state, numeric zero, strings, or an incorrectly typed list remain collection gaps. Note bodies never use this exception. Preserve the unmodified response data before propagating schema errors. Rejected responses use diagnostic-project-* files with collection_error and are skipped by ingestion, so an old failure cannot re-block a later successful retry. No failed body is marked collected or reviewed.
