# 离线研究工作台与来源一致的报告

已有大量搜索入口、但正文评论未收尾，或需要核对报告来源时，使用 `scripts/xhs_research_desk.py`。它只读指定采集项目 SQLite 和原始快照，不接触浏览器、凭据、STOP 或原任务状态。旧命令继续使用；新增工具输出的是工作建议，不能代替原采集器的实时权限检查、审读回执或完成验证。

## 固定范围与候选清单

传入仅属于当前研究的真实任务 ID 数组。长期复用采集通道时，`--project` 指实际保存 bridge/raw 的采集项目，不能把只存归档或 API 旧任务的研究文档目录当作完整采集项目。不要省略范围后扫描所有旧课题。

```bash
python "$XHS_SKILL/scripts/xhs_research_desk.py" scan --project "$XHS_COLLECTION_PROJECT" --task-list "$XHS_TASKS" --output '/本轮/工作台-001.json'
python "$XHS_SKILL/scripts/xhs_research_desk.py" triage-pack --inventory '/本轮/工作台-001.json' --limit 40 --output '/本轮/候选审读-001.json'
python "$XHS_SKILL/scripts/xhs_research_desk.py" next --inventory '/本轮/工作台-001.json' --output '/本轮/后续建议-001.json'
```

输出使用新文件名，已有文件不覆盖。终端只返回路径、SHA 和简短统计；原文和全部来源留在文件里，按需读取，禁止为了少输出重复执行有副作用的命令。`triage-pack` 按唯一笔记和文字版本返回；用 `--after` 接 `next_after` 连续读取，不截断单条文字。无标题、无关键词、营销正文都不能自动排除评论入口。

`unassessed` 只表示尚无导入本工作台的筛选决定，不表示历史资料从未读过。先复用已有实际审读与筛选记录，不让新工具迫使重读全部旧材料。历史记录缺少可靠的同笔记来源定位时保留未导入原因，不伪造新回执。

Codex 审读后将筛选决定保存在 JSON 数组，扫描时用 `--decisions` 加载，每项包含：

```json
{"note_id":"实际24位笔记ID","decision":"include","reason":"已读原文的范围判断","reviewer":"Codex","reviewed_sources":[{"sha256":"实际来源SHA256","locator":"cards/0"}]}
```

允许 `include / exclude / defer`；决定必须指向同一笔记的真实卡位，直接打开的正文也可用其真实来源及 `locator=body`。候选的其他文字或作者身份版本尚未核过时返回 `needs_reassessment`，旧排除不会覆盖新内容。决定只管理工作台候选，不会调用原项目的 include、标记已读或创建患者结论；需要执行时仍通过原 `review/native` 接口。既有正文审读不等于标题筛选决定，不能为减少待办自动补决定。空来源且已有登记任务时返回原范围登记核查，不猜正文、不按昵称合并。

`next` 优先提示未审来源和已到末尾但尚无完成登记的范围，再处理已入队的下游、纳入后尚无下游任务的候选、候选筛选，最后继续搜索。已有一级评论未完时不建议跳过它执行独立 replies；故障任务隔离并保留。`--preferred` 接真实 ID 数组，同类可执行建议保留显式顺序；依赖检查仍看完整课题范围。建议读取的是快照，执行前仍用原 `xhs_batch.py` 调度与守卫；非搜索新正文需要显式纳入。`register_scope` 只是提示通过原接口登记真实下游，`verify_end_receipt` 只是提示核查真实末尾凭据，均不自动完成任务。媒体交给对应现有处理器，不能用默认 collect 把媒体算读过。该工作台不会宣布研究完成或修复访问限制。

## 增量审读与完整上下文

正文和评论审读仍用现有 `review-next / review`。`review-next` **会保存索引和审读包**，不属于只读盘点。新增包保留父楼的已发现版本和有真实 ID 的直接回复对象上下文；昵称不能代替关系。缺失上下文明确记录，旧 `parent` 字段与回执提交方式保持兼容。

默认读取精简输出中所有新增/修改文字、父楼及直接回复对象；源文件及准确位置留在完整包。相对时间、点赞和签名变化不单独触发文字重读。上下文是归属参考，不自动成为新的已审条目。提交时核查包内正文、上下文和快照 SHA；原 fault、STOP、分页与完成校验保持。

## 媒体与报告

工作台将评论附件、正文已定位截图、未匹配轮播位置的媒体资源分开。未匹配资源与截图可能重复，不相加宣称独立图片数量；没有发现媒体不等于确认无媒体。已有数据库 `reviewed` 仅以 `reading_not_revalidated` 展示，不能由文件存在、截图成功或图片数量推断已读。

新报告使用 `xhs_media_report.render_media_reviews(entries, titles)`；entries 为带 `file/sha256` 的实际视觉回执，titles 可按 note ID 提供准确标题。函数核对回执、图片、捕获侧车中的 SHA、笔记 ID 和序号，仅输出该位置自己的 `observation/summary`。无真实审读声明或缺少观察文本时失败，不套用其他病例文案。它只渲染正文图片审读，不接收评论图片或视频作为正文图；评论附件、字幕和视频仍走原媒体流程。

报告验收同时核对“内容表达与原审读一致”和“来源位置正确”。文件哈希一致不代表摘要语义正确。历史原文与回执保留；修正当前派生文档时保存前版，不制造新的阅读或采集信用。

## 效率统计

`metrics --manifest 测量清单.json --output 新结果.json` 读取真实测量文件。清单为 `{"measurements":[...]}`；每项含 `measurement_id`、`phase`、`file`、`sha256`。phase 使用 `collection / review / recovery / export`。默认读取 checkpoint 的 `wall_seconds / metrics.calls / processed_units`；不同格式用 `fields` 显式映射 `seconds / browser_calls / units` 的点路径。

缺失阶段保持 unknown，重复 measurement_id 拒绝计入；驱动调用时间不能映射成端到端时间，可能重叠的阶段不直接相加。真正的新增评价需由病例证据源核定，搜索卡、CID、媒体位置均不自动计作独立患者。优先比较同范围的新增已审文字、完成的评论范围和解决的归属缺口，不用“批次数”证明效率。
