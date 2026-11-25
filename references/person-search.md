# 指定人物的别名检索与逐人覆盖

人物资料少时，先分清未执行、未读完和身份不足，再扩大检索。用 `scripts/xhs_people.py` 把仓库外人物配置连接到既有本地召回和在线队列；不另建采集器，也不替代原文审读。真实姓名、别名、机构和来源只放外部项目。

## 外部配置与规划

配置格式为 `xhs-people-v1`。`entities` 每项提供稳定 `id`、`name`，可提供已核拼音音节 `pinyin`、`surname`、`surname_pinyin` 和 `aliases`。别名分别标注 `basis: hypothesis` 或 `observed`；后者附实际 `sources`（`file`、`sha256`、`locator`），歧义用 `ambiguities` 保存。拼音不从汉字自动猜测；复姓、多音字应先核对。

`contexts` 含明确的 `institutions` 和 `professions` 列表；`titles`、`experiences` 和 `projects` 也由课题显式提供。项目选择沿用本地召回的 `all_tasks` / `task_ids` 规则。称谓组合只是检索假设，不证明人物职称。

```sh
python scripts/xhs_people.py plan --config /external/research/people.json --project /external/research/current
python scripts/xhs_people.py coverage --config /external/research/people.json --project /external/research/current
```

`plan` 只输出 JSON：人物索引、查询及词族、兼容的 `xhs-recall-v1` 配置和原生搜索 `manifest`。既有项目默认传入 `--project`，复用规范化后相同查询的实际拼写和任务身份；同一排序存在多条历史变体时优先精确写法，否则选任务 ID 最小者，其他历史记录仍保留在覆盖中。将输出保存到仓库外，按现有 `xhs_native.py import`、轮次与 `xhs_batch.py` 流程执行；生成清单不等于完成检索。

全名、拼音、缩写、姓氏称谓和机构经历分别召回，不堆叠成全部词语的笛卡尔积。综合、最新、热门分别登记。单姓在线须配机构或专业；本地相关原文可宽匹配单姓。大小写、全角和标点首先通过本地规范化处理；只有新观察或结果差异支持时才扩展对应在线写法。新增查询进入扩展轮次，既有查询断点和来源继续保留。

## 发现目标不等于评价归属

查询携带 `discovery_entity_ids` 和 `discovery_entity_labels`，用于逐人调度；共享查询只执行一次，人物路由合并。同帖下游继承发现来源，但不能据此把每条评论归给该人物。旧任务缺显式标识时继续采用原全名路由规则。

`coverage` 只读项目，分列未安排、待执行、已观察未结束、登记末尾和阻塞；审读与下游阶段另外显示。登记末尾与历史审读状态并非新的实时认证。来源缺失、未读配图和视频不得作为零结果隐藏，也不得据此自动完成任务。

## 别名发现后的深读闭环

1. 对明确选定的历史项目运行[完整本地召回](local-recall.md)，翻完 `more/next_after`，保留旧版本命中。新别名改变召回范围后重新开始游标。
2. 用 `thread` 读取关联整楼，再按[研究工作台](research-desk.md)核对正文、直接回复、澄清和相关作者后续。没有关键词的回复同样可能改变归属。
3. 保留机构帖、比较帖及营销帖的评论入口，不能仅因正文未出现人名就排除；平台推荐词不能当作患者称呼。
4. 对实际读到的新称呼记录来源与歧义，再扩展查询和本地召回。拒绝把检索假设升级成身份事实。
5. 按[多医生覆盖轮换](medical-experience.md)优先补评价不足的人物。先处理已纳入帖的未读下游，避免用无限扩词掩盖积压。

合成测试验证匹配、路由及续跑不变量；真实搜索、排序、回复、媒体仍需独立验证。可核验的交付是明确入口及下游的处理状态和缺口，不能承诺全站零遗漏。

## 逐人机构上下文

`entities` 中可选的 `contexts` 对象提供 `institutions` 和 `professions` 两个列表。提供时完整替代该人物的全局上下文；省略时沿用原全局行为。只影响查询发现路由，不证明任职或评价归属。历史时期及来源仍由外部课题配置保存，不因配置改变重建旧任务或清除故障。
