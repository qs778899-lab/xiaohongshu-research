# 精确source=404首页的单次页级恢复

仅处理原详情标签停在 `https://www.xiaohongshu.com/explore?source=404`，绑定因 `SCOPE_CHANGED` 阻塞的情况。必须已有仍有效的 `same-target-rendered-page-presence_v1` 网页凭据，完整原授权、账号锁、STOP及源码校验继续通过。这是原网页范围内的导航恢复，不签发新账号授权，也不调用新的访问恢复probe。

```bash
python "$XHS_SKILL/scripts/xhs_source404_recovery.py" \
  --skill "$XHS_SKILL" --project "$XHS_PROJECT" \
  --binding "$XHS_BINDING" --task "$XHS_FAILED_TASK"
```

原任务须为pending笔记任务，原 `navigation_intent` 与其已观察来源完全匹配。仅使用同一自有detail page/session和UID；metadata须为上述精确首页，identity须同账号且无访问提示，角色租约须只有这个自有页面。恢复目标仅为原 `last_url`：它必须是已核验笔记，且有同账号的原始捕获或现有凭据来源支持。其他跳转不适用。

脚本先把原绑定、失败、导航意图、相关任务和STOP摘要保存到 `bridge/source404-recoveries/`，固定机会在任何浏览器调用前消费。前置核验后，将**使用同一失败原链接**的pending任务标为hold，保留评论、正文和媒体历史；原因记录为“该观察链接导向source=404首页，原因未知”，不能据此宣布删帖、完成或无评论。

随后只导航一次到原 `last_url`，等待1秒，再用原 `verify_tab` 核对目标和账号；不改守卫、不重载、不创建或切换会话。完整凭据和STOP仍有效才恢复绑定ready，后续交回正常work队列。API及图片CDN限制保持，未完成内容继续列为缺口。

超时、中断、目标变化或动作不明均保留失败，不重放导航；改变失败时间或重复运行不能获得新机会。登录、验证码或访问提示执行原账号停止规则，不通过此分支处理。原失败链接保持隔离，等待新的实际有效来源核查，不能通过清除hold重试旧链。

离线回归：`python "$XHS_SKILL/scripts/test_source404_recovery.py"`。测试使用合成账号和模拟driver，覆盖单次导航、精确URL/账号/单页校验、原来源隔离、新STOP及动作不明；发布前仍须完成实际恢复和普通work接续验收。
