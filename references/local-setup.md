# 本地配置、项目别名与公开边界

运行 `python3 scripts/xhs_local.py init` 创建权限受限的 `.local/config.json`；重复执行保留原配置。只在此私有文件填写本机路径和标签，示例结构如下（路径占位符需替换为仓库外绝对路径）：

```json
{
  "format": 1,
  "defaults": {
    "python": "/external/runtime/python/bin/python",
    "node": "/external/runtime/node/bin/node",
    "opencli_root": "/external/runtime/opencli/node_modules/@jackwener/opencli",
    "runtime": "/external/runtime/bridge-runtime.json",
    "data_root": "/external/research"
  },
  "projects": {
    "topic-a": {"path": "/external/research/topic-a", "binding": "profile_alpha"},
    "topic-b": {"path": "/external/research/topic-b"}
  }
}
```

`binding` 只在该项目已有对应绑定时填写，不能拿配置替代真实绑定流程。不在这里复制 Cookie、账号 UID 或会话令牌。运行环境 JSON 的创建与哈希核验沿用 [bridge.md](bridge.md)，不自行生成虚假摘要。

```sh
python3 scripts/xhs_local.py doctor
python3 scripts/xhs_local.py resolve --project-alias topic-a
python3 scripts/xhs_local.py resolve --project /external/research/topic-a
```

`resolve` 输出 `project`、可选 `binding` 和 `runtime`，供模型或调用者传给现有命令。它不执行 shell 字符串，不隐式发起采集。显式 `--project` 优先；覆盖到其他项目时不继承原项目绑定和专属 runtime，避免串题。旧命令可直接使用旧绝对路径，不必改成别名。`doctor` 只检查本机路径及绑定归属，不连接浏览器、不证明登录成功。

同账号仍沿用原共享保护目录和租约算法，项目别名不会建立新的账号身份或解除 STOP。真实验证和媒体工具输出放在外部目录，`.local` 仅存配置。

## 日常维护和发布

公共修复在本仓库直接开发。新增公共文件先更新 `PUBLIC_FILES.txt`，用合成测试验证。测试输出放临时目录。每个克隆需执行一次 `git config --local core.hooksPath .githooks`；仅影响当前仓库。受管理执行环境如果限制 Git 写入，按环境审批处理。

私有 `.local/publication-policy.json` 可设置 `{"forbidden_strings": ["需要拦截的个人标签"]}`。本机检查会检查公开文件、暂存区或全历史，但不输出命中的值；CI 没有你的私有禁词，因此发布者仍须本地检查。不要把实际禁词复制进公共测试。hooks 是防误操作措施，不能阻止故意使用 `--no-verify`；不要绕过它们。

提交沿用现有 Git 身份；它会出现在公开提交元数据中。发布前检查 `git var GIT_AUTHOR_IDENT`。此仓库不修改你的全局姓名、邮箱或认证。

若扫描发现旧提交有私有信息，不得通过删工作区文件后继续推送；先停止发布，在本地处理完整历史后重新审计。已泄漏的凭据需由所有者撤销。不要强制推送或覆盖其他人的同名仓库。
