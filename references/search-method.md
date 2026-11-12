# 搜索请求方法与兼容

skill 内的 `xhs_search.py` 适配已安装的 xiaohongshu-cli，不修改或升级依赖。它仅在 `/api/sns/web/v1/search/notes` 发出前复制参数，使 `filters` 中唯一 `sort_type.tags` 与顶层 `sort` 一致。其他参数及上游签名方式保持原样，缺少或重复排序字段时停止，不能猜测新格式。

这是参数一致性修复，不是检索效果保证。有限 A/B 返回相同结果不能证明字段永远等价；返回不同结果也不能单独证明召回改善。旧 latest/popular 资料继续保留为研究证据，不能因修复参数就称为无效。

成功的新搜索 raw 保存 `search_method_version` 与 `search_request`：实际未签名请求 body、其 SHA-256、依赖版本、查询、页码、排序及 search_id。它不保存 Cookie、HTTP headers 或签名。采集记录与实际请求不一致时拒绝导入。缺少版本的旧 raw 归入 legacy 方法，不改写原文件。

## 续跑与统计

- `general` 请求参数实质未变，继续使用原任务键、缓存与页链。已结束的 general 保持结束，不能为了升级重搜。
- `latest/popular` 新方法使用独立任务键并从第 1 页开始，不能把新第 24 页接在旧方法 1–23 页后。未选择迁移的旧任务保留原状态；已结束的旧查询不自动重开。
- 对已有项目再次 `plan` 只追加新查询，不批量重建原矩阵。只有明确选择一个需要继续的 query+sort 后，调用 `add search`（payload 含 `query`、`sort`、`provenance`）才建立该组合的新方法任务。
- `add` 将旧 latest/popular 通过 `superseded_by` 关联到新任务，只改变历史关联，不修改旧 status、next_page、页数据或原始资料。历史任务不再调度，也不计入剩余工作；报告单独列出。
- 导入真实新方法 raw 也建立独立页链；A/B 的 `operation=search_ab` 不属于可导入分页。
- `search_coverage.logical_combinations` 按 query+sort 计数，`methods` 分列各方法的原状态、页快照和历史关联。75 个组合升级部分方法后仍是 75 个逻辑组合，不应宣称覆盖了 125 或 150 个组合，也不生成全站召回率。

## 受保护的小样本 A/B

沿用已授权、未 STOP 的同一 run；不要重新登录或创建替代账号会话。脚本复用 project/run/account 锁、原请求限速、访问限制 STOP 和超时处理，串行发送并逐条保留结果。它只读项目数据库，不迁移任务。

```sh
python "$XHS_SKILL/scripts/xhs_search_ab.py" \
  --project "$XHS_PROJECT" --run "$XHS_RUN" --out "$XHS_AB_NEW_DIRECTORY" \
  --query '已经确定的查询' --sort latest --sort popular --repeats 1
```

上例为 4 次主搜索（2 个排序 × 2 种方法，各一次第一页）；依赖可能执行同样受保护的可选预热请求。输出目录必须新建且独立于生产 raw。对同一种排序使用 `--repeats 2` 会采用 v0/v1/v1/v0 顺序。默认不清空或修改 search_id 缓存，记录实际 ID，分析时须说明共享服务端会话或缓存的可能影响。

遇限制立即保存已完成结果并停止，不自动换会话、改 Cookie 或清除 STOP。分析比较请求参数、帖子 ID/顺序及可观察字段；发布时间仅取真实正文 metadata 或明确完整时间，不从 ID 猜日期。点赞数等单一互动指标不能代表平台全部热门排序规则。
