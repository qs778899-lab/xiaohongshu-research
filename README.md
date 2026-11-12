# Xiaohongshu Research · 小红书深度检索

面向 Codex 的只读小红书研究 skill：持续搜索分页、逐楼评论、来源定位、上下文归属、媒体待查清单、可续跑队列和可更新报告。支持 macOS + Chrome。

**它不是小红书官方接口，也不保证全站穷尽或无人值守完成。** 动态页面、登录、限流、删除内容和媒体读取会造成明确缺口。正文和评论由模型审读；脚本不自动断言评价对象、医疗因果或“没有负面评价”。

## 安装与使用

需要 Python 3.10+、Node 20.18.1+、Chrome 和用户授权的小红书账号。依赖固定为 `xiaohongshu-cli==0.6.4` 与 `@jackwener/opencli@2.0.2`。不自动安装扩展、不修改全局 Git 或浏览器配置。

```sh
git clone https://github.com/qs778899-lab/xiaohongshu-research.git
cd xiaohongshu-research
python3 scripts/xhs_local.py init
# 只影响当前仓库；克隆后 Git 不会自动启用 hooks。
git config --local core.hooksPath .githooks
```

把此目录作为唯一维护目录。在 Codex 中明确引用这里的 `SKILL.md`，或按自己的技能发现配置引用这个目录，无需维护另一份公开版。运行前阅读 [SKILL.md](SKILL.md) 和[本地配置与安装](references/local-setup.md)。

运行环境应安装在仓库外，例如：

```sh
XHS_RUNTIME="$HOME/.local/share/xhs-research-runtime"
python3 -m venv "$XHS_RUNTIME/python"
"$XHS_RUNTIME/python/bin/python" -m pip install -r scripts/requirements.txt
npm install --prefix "$XHS_RUNTIME/opencli" --ignore-scripts '@jackwener/opencli@2.0.2'
```

已有合规版本运行环境可以复用。浏览器接入按[运行说明](references/runtime.md)或[固定配置连接](references/bridge.md)核对；本地路径初始化不代表账号授权或浏览器就绪。

## 同一仓库的分层

| 层 | 存放内容 | 进入 Git |
|---|---|---|
| 本仓库公共文件 | 通用脚本、技能文档、合成测试、公开清单 | 是 |
| `.local/` | 路径、账号标签、项目别名、本机发布禁词策略 | 否 |
| 仓库外研究目录 | SQLite、帖子、评论、媒体、报告、会话、凭据 | 否 |
| 原有共享账号保护目录 | 跨主题账号锁与停止记录 | 否；保持旧算法 |

原有 `--project`、`--run` 等入口和外部项目路径继续可用。不移动旧研究，不重写已绑定账号，不清除 STOP。项目和通用 JSON 写入口拒绝把研究数据写入代码仓库。其余工具的输出也必须放在外部目录；公开文件白名单是第二道检查。

## 验证与发布

在调用过程中完善 skill 时，遵循 [AGENTS.md](AGENTS.md) 和[阶段提交与同步](references/maintenance.md)：阶段验证通过后详细 commit，并在既有授权范围内及时 push。私有研究数据不参与提交。

```sh
"$XHS_RUNTIME/python/bin/python" scripts/run_tests.py \
  --opencli-root "$XHS_RUNTIME/opencli/node_modules/@jackwener/opencli"
python3 scripts/publication_check.py
python3 scripts/publication_check.py --staged --history
```

`PUBLIC_FILES.txt` 是逐文件公开清单。增加公共代码时显式维护它；不得加入真实研究数据或 `.local`。提交 hook 检查暂存内容，推送 hook 检查所有本地引用可达的提交与标签。敏感规则不会打印命中值。自动扫描不是个人信息识别的完整保证，发布前还需人工审查 diff 和实际 Git 提交身份。

默认安装不会采集真实数据。离线测试不证明当前账号可访问、平台末尾可穷尽，或所有媒体均已验证。旧页面选择器与扩展版本变动可能需要显式兼容核验，禁止为通过检查擅改哈希或停止记录。

Apache-2.0，见 [LICENSE](LICENSE) 和[第三方声明](THIRD_PARTY_NOTICES.md)。
