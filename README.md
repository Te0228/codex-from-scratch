# codex-from-scratch

从零实现一个最小 Codex：**31 个可执行 Notebook，一套共享的 Python Coding Agent。**

第一章先跑通完整 Loop，随后逐章理解上下文、工具、沙箱、记忆、协作与评测，最后运行组装后的同一个 Agent。面向已有 Agent 经验、希望理解系统设计的读者。

## 开始学习

需要 Python 3.12+。在项目根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e "./code[notebooks]"
.venv/bin/python -m jupyterlab notebooks
```

打开 [01-loop.ipynb](notebooks/01-loop.ipynb)，从上到下逐格运行。每章按“设计问题 → 机制 → 正常实验 → 失败或取消实验 → Codex 源码对照”展开；可以修改输入，重新观察断言与状态变化。

Notebook 直接导入 `code/mini_codex`。章节实验、命令行与最终样例使用同一套实现。默认不调用付费模型；真实模型单元需要主动启用。

## 运行最终 Agent

完整工具执行需要 Linux 与 bubblewrap；macOS 使用已启动的 Docker Desktop：

```bash
python3 code/run.py doctor
python3 code/run.py replay
python3 code/run.py test
```

`replay` 只替换模型输出，文件操作、命令、隔离、补丁和最终验证仍真实执行。示例修复 `task_list.py` 的筛选缺陷：先复现两个失败用例，再修复，最后对冻结快照独立检查五个固定输入。

使用 DeepSeek：

```bash
cp code/.env.example .env.local
chmod 600 .env.local
# 在 .env.local 填写密钥；不要写入 Notebook。
python3 code/run.py live --env-file .env.local
```

处理自己的项目：

```bash
python3 code/run.py live \
  --workspace /path/to/your-project \
  --prompt "修复这个项目的问题，并运行测试" \
  --verify-command "python -m unittest discover" \
  --env-file .env.local
```

工作区必须与 Agent 的可信代码和状态目录分开。当前快照验证面向小型项目：最多 100 个文件、单文件 2 MiB，拒绝符号链接和特殊文件。自定义验证命令的退出状态按实际结果记录；未提供验证依据的完成回复保留为 `needs_review`。

## 目录

```text
README.md
notebooks/                 # 31 章教材与逐格实验
code/
  mini_codex/              # 统一内核、扩展与服务
    components/            # 各章共享实现
    assets/                # 最小任务与受保护验证器清单
  tests/                  # 单元、集成与失败测试
  run.py                  # Linux/Docker 运行入口
  verify_notebooks.py     # 全部 Notebook 的执行验证
  pyproject.toml
  Dockerfile
```

## 章节

| 章 | 主题 |
| --- | --- |
| 01 | [最小 Codex：先跑通一个完整 Loop](notebooks/01-loop.ipynb) |
| 02 | [上下文：决定每一步让模型看到什么](notebooks/02-context.ipynb) |
| 03 | [模型通信：请求、流式响应与重试](notebooks/03-model-transport.ipynb) |
| 04 | [工具系统：从模型意图到受控调用](notebooks/04-tools.ipynb) |
| 05 | [执行器：进程、终端与资源生命周期](notebooks/05-processes.ipynb) |
| 06 | [回合调度：追加输入、等待与取消](notebooks/06-scheduling.ipynb) |
| 07 | [运行配置：身份、模型与生效快照](notebooks/07-config-snapshots.ipynb) |
| 08 | [权限与审批：把意图变成执行许可](notebooks/08-permissions.ipynb) |
| 09 | [沙箱：文件、进程、网络与凭据边界](notebooks/09-sandbox-planning.ipynb) |
| 10 | [会话与历史：持久化、恢复、分叉和回退](notebooks/10-history.ipynb) |
| 11 | [长期记忆：提取、检索、修订与遗忘](notebooks/11-durable-memory.ipynb) |
| 12 | [计划与持续任务：什么时候继续运行](notebooks/12-plans-and-goals.ipynb) |
| 13 | [Skills：按需装载方法知识](notebooks/13-skill-loading.ipynb) |
| 14 | [MCP：连接外部工具、资源和App UI描述](notebooks/14-mcp-resource-origin.ipynb) |
| 15 | [插件与工具发现：从安装到模型可见](notebooks/15-plugin-catalogs.ipynb) |
| 16 | [CodeMode与工具并发：把编排交给代码](notebooks/16-codemode-concurrency.ipynb) |
| 17 | [多Agent：委派、协作与结果核验](notebooks/17-multi-agent-coordination.ipynb) |
| 18 | [App-server：让内核成为可连接的服务](notebooks/18-app-server-rpc.ipynb) |
| 19 | [客户端与SDK：同一任务的不同入口](notebooks/19-clients-and-sdk.ipynb) |
| 20 | [执行环境与工作树：动作在哪里发生](notebooks/20-environments-worktrees.ipynb) |
| 21 | [后台宿主与远控：让任务脱离前台连接](notebooks/21-host-lifecycle.ipynb) |
| 22 | [云任务：提交、观察与结果落地](notebooks/22-cloud-artifact-gate.ipynb) |
| 23 | [媒体与图像任务：把附件和产物接入Loop](notebooks/23-media-artifacts.ipynb) |
| 24 | [实时语音：双向流和媒体会话](notebooks/24-realtime-streams.ipynb) |
| 25 | [Web检索：查询、证据与引用](notebooks/25-web-evidence.ipynb) |
| 26 | [浏览器与Computer Use：观察、动作、核验](notebooks/26-observation-action-protocol.ipynb) |
| 27 | [可观测性与诊断：解释一次运行](notebooks/27-observability.ipynb) |
| 28 | [Prompt缓存与性能：哪些优化真的有效](notebooks/28-cache-statistics.ipynb) |
| 29 | [评测与回归：独立判断Agent是否完成任务](notebooks/29-evaluation-gates.ipynb) |
| 30 | [数据迁移与交付：从代码到可升级产物](notebooks/30-migration-delivery.ipynb) |
| 31 | [完整Mini Codex：装配与系统验收](notebooks/31-assembly-acceptance.ipynb) |

## 依据与验证边界

Codex 源码对照固定于 [`openai/codex@c0cc9fc`](https://github.com/openai/codex/tree/c0cc9fc820f62e4dbdde68214dd6d792d6c8a7ee)。重要结论在对应 Notebook 给出源码链接；Python 接口和教学简化单独说明。

完整样例验证真实 Coding Loop。浏览器、云任务、图像、语音等章节目前提供本地协议和状态实验，相应真实服务适配器尚未实现。

本次发布已在仅含仓库文件的干净副本验证：

- 31 个 Notebook、192 个代码单元全部执行通过。
- Linux 下 70 项测试通过，0 失败、0 跳过；其中包含真实 bubblewrap 隔离测试。
- DeepSeek 实测完成修复，最终五个固定用例通过。
- Wheel 在独立虚拟环境安装、导入和验证资源检查通过。

Notebook 附本次实际执行输出，本机根路径已隐藏。重新验证：

```bash
.venv/bin/python code/verify_notebooks.py
```
