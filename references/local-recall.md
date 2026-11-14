# 缩写与历史原文的只读召回

用于已有材料里的隐晦称呼复查。先从在线搜索发现帖子，再对明确选定项目的全部已保存版本检索；此功能不是全站评论搜索，也不替代逐楼阅读。旧 `search-local` 保留，新入口不会初始化数据库、改任务、登记已读或访问网络。

## 公共能力与外部配置

脚本、本文及合成测试属于公共仓库。真实课题姓名、称呼、机构、查询矩阵、歧义说明和资料均存仓库外，作为配置传入；不把真实案例改名后复制进公开测试，也不写入提交消息。`.local` 只保存必要本机映射和维护配置。

外部 JSON 配置示例，所有对象均为虚构示例：

```json
{
  "format": "xhs-recall-v1",
  "projects": [{"path": "/external/research/topic-a", "all_tasks": true}],
  "terms": [
    {"id": "name", "text": "示例医生甲", "mode": "literal"},
    {"id": "initials", "text": "AB", "mode": "initials",
     "ambiguities": ["也可能指另一人或机构，须看原始上下文"]}
  ]
}
```

`projects` 每项必须明确选 `all_tasks: true` 或 `task_ids: [实际任务ID]`。网页任务只读其绑定来源；旧 API 任务按笔记及来源类型取对应历史版本，不扩展同帖未选网页任务。需要更多楼层上下文时显式补选相关任务。不自动扫描其他目录；跨项目须逐个列出。没有项目、但有下述明确参考资料时可传空列表。

`literal` 仅统一大小写与字符宽度，遮字、谐音须以实际观察或明确检索假设另列；`initials` 仅接受 2–12 个拉丁字母，并允许字母间空格、点号或中点。字母数字边界避免把短缩写误命中更长英文单词。不扩展为自动猜人名。`ambiguities` 随命中输出，任何命中都保持待核身份。

## 搜索、上下文与覆盖

```sh
python "$XHS_SKILL/scripts/xhs_recall.py" search --config "$XHS_PROJECT/recall.json" --limit 50
python "$XHS_SKILL/scripts/xhs_recall.py" search --config "$XHS_PROJECT/recall.json" --after '上次next_after'
python "$XHS_SKILL/scripts/xhs_recall.py" thread --config "$XHS_PROJECT/recall.json" --note-id '实际笔记ID' --comment-id '实际评论ID'
python "$XHS_SKILL/scripts/xhs_recall.py" coverage --config "$XHS_PROJECT/recall.json"
```

只向标准输出返回 JSON；保存结果时选择仓库外文件。`more` 和 `next_after` 明示剩余结果，默认 50 是单页大小。下一页绑定配置、来源及记录状态；来源/任务变化后须从首页重新检索，不能用旧游标跳过新内容。

每条结果提供原文、原始显示文字中的字符区间、真实已观察 URL（没有则为 null）、来源文件、哈希、位置及版本。相同真实 ID、文字和身份关系可合并来源；没有真实 ID 的观察保持独立。版本数、任务数、匹配数均不是患者数。

快照的 `sha256` 校验文件字节；媒体索引行使用明确的 `hash_scope: sqlite_row` 和 `row_sha256`，另列媒体/转写文件及各自哈希，不能拿行哈希校验整个可变数据库。选定任务模式下，媒体须明确纳入媒体任务，不因同帖评论被选中而扩展媒体范围。

命中附父评论与回复目标的已保存版本，区分同源上下文和其他来源观察；身份冲突不择一替代。采集器的 `thread_root` 回退仅证明楼层背景，不升级成已观察到的直接回复；缺关系来源保持未知。`thread` 从评论关系取整个关联楼层并分页，不限关键词，以便阅读未点名的澄清和否定；它不能恢复未采集或缺正文的回复，也不证明跨版本关系始终不变。

`coverage` 输出查询、排序、采集/审读的原记录、未审来源及来源完整性缺口。记录的 END 不升级为全帖完成，零命中不证明不存在。它不重新认证在线状态或既有完成回执。正式覆盖报告仍结合原研究工作台及轮次审核。

## 媒体文字和旧记录

已导入的媒体文档只有通过既有媒体读取凭据和文件哈希检查后才参与召回。正文图片的独立视觉回执可在配置加入：

```json
{"media_reviews": [{"project": "/external/research/topic-a", "file": "/external/research/topic-a/visual-review.json", "sha256": "实际回执SHA256"}]}
```

此入口复用 `xhs_media_report.observation`，只适用于其支持的正文图片回执；评论图片、字幕和视频使用已有媒体导入流程。图片观察不是患者逐字原话，字幕阅读不自动证明原音频或视频完整读取。未读/无效媒体列缺口，不自动 OCR 或标记已读。

旧手工笔记或审核摘要可用 `references: [{"path": "/external/research/legacy.md", "sha256": "实际文件SHA256"}]` 显式加入。它们始终标成 `reference_text`，仅指路，不能当成患者原帖或自动继承其结论。不会猜测失效链接、签名或作者身份。
