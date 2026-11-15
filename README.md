# codex-from-scratch

通过一步步构建一个最小 Coding Agent，理解 Codex 的设计。

从一个具体任务开始：让 Agent 读取有缺陷的 Python 文件、修改代码、运行检查，直到结果正确。然后沿同一个系统，逐章加入上下文、记忆、沙箱和多 Agent 等能力。

## 开始

需要 Python 3.12+。在项目根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install jupyterlab ipykernel
python -m jupyterlab notebooks/01-loop.ipynb
```

从 [第一章：写出一个能修复代码的 Loop](notebooks/01-loop.ipynb) 开始，按顺序执行代码单元。第一章直接在本机运行，不需要 Docker 或模型密钥。

## 第一章会做什么

开头先建立本章使用的六个模块，列清接口和依赖，再逐个实现：

| 模块 | 负责什么 | 核心接口 |
| --- | --- | --- |
| `model.py` | 获取模型输出，切换脚本模型与真实模型 | `sample(request)` |
| `history.py` | 保存消息，配对工具调用与结果 | `request()`、`add_tool_result()` |
| `tools.py` | 文件工具、参数解析与调用分派 | `execute(call)` |
| `executor.py` | 启动练习程序，收集进程结果 | `run_python()` |
| `verifier.py` | 持有固定用例，独立比较输出 | `check()` |
| `agent.py` | 推进循环，决定继续或结束 | `run()`、`_step()` |

Notebook 先创建六个空模块，再把每个模块的实现写入文件，并当场调用。最后组装这些对象，完成同一个修复任务。`Agent` 只做编排，文件操作、HTTP、进程执行和结果判定各有归属。

本章全部模块代码都在 Notebook 中。模块目录可以直接跳转，代码可用 JupyterLab 的源码折叠收起或展开。记忆、沙箱等本章尚未使用的模块不提前建立。

## 在 Notebook 里编写项目

**Notebook 是源码，`.py` 是生成结果。** 正常学习只需在一本 Notebook 里操作：

1. 在模块目录中选择要看的部分，阅读完整实现。
2. 修改模块代码单元并运行，再运行“重新加载并组装”。
3. 运行最后的导出单元，自动同步到 [`code/mini_codex/minimal/`](code/mini_codex/minimal/)。

默认运行整本 Notebook 会完成代码生成、组装、实验和导出。每次导出的都是本轮执行过的模块，源文件不用手动复制；生成的包可以脱离 Notebook 使用。

保存 Notebook 后，也可以在终端导出或检查同步状态：

```bash
python code/notebook_tools.py export notebooks/01-loop.ipynb
python code/notebook_tools.py export notebooks/01-loop.ipynb --check
```

后续章节将保留当章完整项目：新增和修改的模块展开讲解，已学模块保留完整代码并可折叠。

先用固定的模型返回看清调用过程：文件读写与程序执行都是真操作，只有模型返回是预设的。第一章末尾换用 `DeepSeekModel`，其余模块和组装方式保持不变。

## 后面如何扩展

后续章节计划沿同一条路径重构：说明当前版本遇到的问题，补上一个模块，再用原任务检查已有能力。

| 阶段 | 重点 |
| --- | --- |
| 01 | 最小 Coding Loop |
| 02–09 | 上下文、模型通信、工具、进程、调度、配置、审批与沙箱 |
| 10–17 | 历史、记忆、计划、Skills、MCP、插件、编排与多 Agent |
| 18–26 | 客户端、服务、工作区，以及云、媒体和外部交互 |
| 27–31 | 观测、缓存、评测、交付与完整系统装配 |

[查看所有章节](notebooks/)

当前先重构第一章及其六模块实现。其余章节和旧版汇总实现暂时保留，待确认这种写法后逐章对齐。

## 项目结构

```text
notebooks/   # 逐步构建、运行和观察
code/
  mini_codex/minimal/  # 从第一章导出的模块化实现
  notebook_tools.py   # 导出与代码折叠设置
  tests/              # 运行测试
README.md    # 学习入口与路线
```

第一章只运行我们自己创建的练习程序，尚未建立操作系统沙箱。隔离边界将在沙箱专题展开；Docker 留作现有 Linux 隔离实验的可选运行方式。

## 参考

内容组织参考 [nanoGPT](https://github.com/karpathy/nanoGPT/tree/3adf61e154c3fe3fca428ad6bc3818b27a3b8291)：具体任务起步，核心代码可读，再逐步增加复杂性。

Codex 行为对照固定于 [`openai/codex@c0cc9fc`](https://github.com/openai/codex/tree/c0cc9fc820f62e4dbdde68214dd6d792d6c8a7ee)。对应源码链接放在各章末尾，Python 教学实现的取舍单独说明。
