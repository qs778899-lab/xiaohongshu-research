# 标签页级受守卫的公开问询执行

本技能默认只读；`xhs_bridge.py` 与 `xhs_gemini_pipeline.py` 始终保持只读。仅在用户明确授权公开问询目标与文案口径时，才可使用独立显式 opt-in 模块 `xhs_public_reply.py` 在已绑定的单个小红书详情标签页中完成单次公开评论或回复。

## 设计与安全红线

1. **严格单标签隔离**：复用既有 OpenCLI 固定配置绑定（`contextId` + `session` + 精确 `CDP targetId` + `uid` + `expectedPath`），不依赖全局窗口焦点或桌面坐标点击，绝不触碰其他 Chrome 窗口或配置。
2. **绝不越界**：只支持已观察公开原帖的一级评论（`top_level_comment`）或指定评论回复（`reply_to_comment`）；硬校验拒绝私信（`private_messages: false`）、关注（`follow: false`）与自动入群（`join: false`）。
3. **跨账号永久去重**：发送前同时核对外置台账（`excluded_accounts` 与已发送/未知结果的 `attempts`）及项目本地不可变发送日志（`bridge/public-replies/by-recipient/<recipient_uid>.json` 与 `by-intent/<intent_sha256>.json`），避免更换账号后重复联系同一对象。
4. **先记账、单次点击、绝不自动重试**：
   - 点击发送前先写入 `send_dispatched_outcome_pending` 不可变记录；
   - 在真实页面 DOM 内定位目标评论或主输入框、回读草稿确认与意图原文一致、确认发送按钮可用后，仅点击一次；
   - 若超时、目标失配或结果不明（`COMMAND_OUTCOME_UNKNOWN`），永久锁定该目标 `never_resend=true`，绝不重发。
5. **验证码与风控立即熔断**：发送前后若观测到扫码/验证码/登录墙/限流弹窗，立即调用共享账号 `STOP.json` 停机并保留断点。
6. **冷却节流与发布复核**：默认两次发送间隔不低于 `90` 秒；每次发送后须使用 `verify-capture` 与 [xhs_public_reply_check.py](public-reply-verification.md) 在新加载原帖中核验本人 UID、完整文案与新评论 ID，确认 `visible_confirmed` 后再登记完成。

## 接口

```text
# 1. 只读预检（绝不修改页面）
xhs_public_reply.py --project P prepare --binding B --intent INTENT.json --ledger LEDGER.json --locate-comment

# 2. 干跑检查（默认不传 --execute 不点击发送）
xhs_public_reply.py --project P send --binding B --intent INTENT.json --ledger LEDGER.json --output RESULT.json

# 3. 显式单次提交（须确认预检通过且已获用户授权）
xhs_public_reply.py --project P send --binding B --intent INTENT.json --ledger LEDGER.json --output RESULT.json --cooldown-seconds 90 --locate-comment --execute

# 4. 发送后只读重新采集，供 xhs_public_reply_check.py 核验
xhs_public_reply.py --project P verify-capture --binding B --intent INTENT.json --output-capture POSTSEND.json --reload
```

## 意图文件字段（留在仓库外）

- 必填：`destination`（`reply_to_comment` 或 `top_level_comment`）、`note_id`、`url`、`recipient_uid`、`account_uid`、`message`、`maximum_send_clicks: 1`、`private_messages: false`、`follow: false`、`join: false`、`authorization`。
- `reply_to_comment` 额外必填：`root_comment_id`、`recipient_comment_id`、`recipient_name`。
- `top_level_comment` 禁止填写 `root_comment_id` 与 `recipient_comment_id`。

## 验证边界

- 离线测试（`test_public_reply.py`、`test_public_reply_dom.mjs`、`test_public_reply_check.py`）覆盖去重、冷却、草稿校验、单次点击、未知结果锁死、验证码触发 STOP 与一级/二级核对。
- 离线通过不代表真实站点 DOM 永不变更或账号不触发平台验证；真实使用须先单条小规模闭环实测并完成发布复核。
