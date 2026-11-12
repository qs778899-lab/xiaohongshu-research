# 当前帖子中的单个评论附件

用于已采集原文里具有真实评论 ID、已观察图片 URL 的附件。脚本只处理当前绑定的同一篇帖子；不会导航、翻楼、点击 viewer、更新签名或下载 CDN。暂不接入自动 worker。

```sh
PYTHON "$XHS_SKILL/scripts/xhs_bridge_comment_media.py" \
  --project COLLECTION_PROJECT --binding REGISTERED_BINDING \
  --task IMAGES_TASK_ID --source REGISTERED_RAW_SHA256 \
  --comment-id OBSERVED_REAL_COMMENT_ID --image-index 1
```

`--source` 是项目 `bridge/raw/<SHA>.json` 的真实 SHA256。来源须已登记在同帖 images 来源档案或 roots/replies 的采集记录中。作者、父楼、回复对象和图片序号保留原定位。旧原文没有明确采集账号字段时记作 `unknown`；评论作者及模糊顶层 `uid` 不充当历史采集账号。明确采集账号与当前账号不符则拒绝。

脚本沿用原账号锁、网页授权及 STOP；每步核同一页面、session、账号和目标。只有当前 DOM 中与原 CID、URL、父楼精确对应的已加载图片可进入显示事务。focus、enlarge、restore 各最多一次，动作前先 fsync 意图；未知结果、新 STOP、身份／来源／CAS 变化立即停止，不在 finally 中强行恢复。

显示仅复用既有三节点、六属性受限 primitive。竖长图、响应式资源、不支持的布局、未加载、未找到评论或现有 viewer 等返回具体缺口。矩形适配不证明无遮挡：PNG 与侧车始终 `reviewed=false`，必须由 Codex 实际查看后另行登记，不能凭几何宣称原分辨率、视频、整帖媒体或评论全部读完。

产物只写在采集项目 `bridge/comment-media/`：原定位、追加式事件／意图、PNG、原截图侧车和最终结果。中断和部分成功保留。固定 `(note,CID,image-index)` 的机会不因换 source SHA 或签名重开；失败／未知事务不自动重发。成功缓存须核 PNG 和侧车 SHA、当前同资源绑定及原账号，明确返回原采集时间和 `cached=true`，不冒充本次新截图或新版内容已读。

代码分享包只包含脚本、合成测试和本说明；真实患者原文、PNG 和运行回执留在研究项目。
