# 原停滞评论任务的结构诊断

这是一项独立诊断，不是重试采集、local-fix或恢复权限。仅用于原 `native/roots` 任务仍为 `COMMENT_PAGINATION_STALLED/hold=true`，且原绑定网页授权已实际恢复的情况。

```text
xhs_comment_structure_diagnostic.py --project P --binding B --task ORIGINAL_HELD_ROOT --source REGISTERED_SAME_NOTE_BODY_SHA [--navigate-original-target] [--screenshot]
```

`--source` 只接受项目已登记的同帖正文原始文件SHA，且采集UID与原绑定一致；不能传任意URL。故障来源、任务与fault原字节、binding/session/page及网页授权在实际账号锁下核对。诊断ID由原任务和fault指纹固定；先保存一次意图，再做任何浏览器操作。换时间、输入来源、代码版本或参数不能重复诊断。

默认只读当前原帖。传 `--navigate-original-target` 是明确允许一次正常导航到来源记录中的原帖；当前原角色仍须通过原目标/身份守卫，导航意图先落盘，结果不明不重发。不新建标签或会话、不重载、激活、滚动、展开，也不调用deep或分页。

固定DOM动作只返回评论容器存在/渲染可见性/尺寸、父楼节点数及最多3个稳定ID、加载标记、短计数标题、安全白名单内的空态/关闭/需App提示和末尾标记。不提取正文、评论文字、作者名称、链接、手机号或任意弹窗。未知或含个人信息的UI文字只返回“文本未输出”标记。渲染可见性不等于评论区已在截图视口中。

`--screenshot` 最多取一次当前视口。只有原page/UID/目标守卫及可见性通过才保存；不强制前置或滚动，不可见则留下gap。PNG仅为诊断，不加入媒体清单或正文/评论已读统计。

结果位于 `bridge/comment-structure-diagnostics/ID/`。不清除hold、游标、停滞计数或STOP，不完成任务，不签grant。结构存在、零节点、标签文字或截图都不能自动变成评论已读/平台穷尽；由Codex读实际诊断后，才判断是否发现值得窄修的具体UI差异。

新增诊断动作会改变driver文件hash，**它本身不是评论采集逻辑修复，不构成新的local-fix重试依据**。真正修复须有根因证据、实质采集器改动与离线验证。
