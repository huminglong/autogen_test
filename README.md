# 三智能体工作流（AutoGen + Mistral）

本项目实现一个 3-Agent 智能体工作流（单文件），角色如下：

- Agent 1: coder — 根据用户的开发需求编写代码。
- Agent 2: reviewer — 对 coder 的代码提出改进建议。
- Agent 3: integrator — 综合 coder 的代码与 reviewer 的建议，输出最终代码（并以 `TERMINATE` 结束）。

底层使用 AutoGen AgentChat（接近 0.7.x API），通过 OpenAI 兼容客户端对接 Mistral 的 `mistral-medium-latest` 模型。

## 目录结构

- `three_agent_workflow.py` — 主程序，内含 3 个智能体和工作流逻辑。
- `requirements.txt` — 依赖清单。
- `task_record_*.md` — 每次运行自动生成的任务执行记录（含过程与结果）。

## 环境要求

- Python 3.10+
- 有效的 Mistral API Key

## 安装依赖（Windows PowerShell）

```powershell
python -m venv .venv
. .venv\Scripts\Activate.ps1
pip install -U pip
pip install -r requirements.txt
```

> 依赖项包括 `autogen-agentchat`、`autogen-ext[openai]`、`python-dotenv` 等。

## 环境变量

- `MISTRAL_API_KEY`（必需）：你的 Mistral API Key。
- `MISTRAL_BASE_URL`（可选）：默认 `https://api.mistral.ai/v1`。

支持 `.env` 文件自动加载（推荐将密钥写入 `.env`，示例内容如下）：

```
MISTRAL_API_KEY=sk-your-mistral-key
MISTRAL_BASE_URL=https://api.mistral.ai/v1
```

在 PowerShell 中设置示例：

```powershell
$env:MISTRAL_API_KEY = "sk-your-mistral-key"
# 可选：如果使用自建/代理的 OpenAI 兼容网关
# $env:MISTRAL_BASE_URL = "https://your-gateway.example.com/v1"
```

## 运行

交互式提示输入任务：

```powershell
python .\three_agent_workflow.py
```

或用参数传入任务：

```powershell
python .\three_agent_workflow.py --task "编写一个将CSV转换为JSON的Python脚本"
```

也可显式传入 Key 与 Base URL（覆盖环境变量）：

```powershell
python .\three_agent_workflow.py --task "实现一个FastAPI健康检查端点" `
  --mistral-api-key "sk-..." `
  --mistral-base-url "https://api.mistral.ai/v1"
```

## 测试脚本

项目包含一个测试脚本 `test_workflow.py`，用于验证工作流的正确性。该脚本包含三个不同的测试任务：

1. **数据处理任务**: 编写一个Python脚本，读取CSV文件并计算每列的平均值、最大值和最小值，结果输出到新的CSV文件
2. **算法实现任务**: 实现一个快速排序算法，并添加测试用例验证其正确性，包括边界条件测试
3. **文件操作任务**: 创建一个工具，可以递归搜索指定目录中的所有文件，按文件大小排序，并输出到JSON文件

### 运行测试

#### 方法1: 使用批处理脚本（推荐）

在Windows系统中，直接双击运行 `run_tests.bat` 文件，或在命令行中执行：

```
run_tests.bat
```

#### 方法2: 直接运行Python脚本

在命令行中执行：

```
python test_workflow.py
```

### 测试结果

测试完成后，结果将显示在控制台中，包括：

- 每个任务的执行状态（成功/失败）
- 任务执行时间
- 任务输出和错误信息（如果有）
- 生成的记录文件和配置文件信息

测试结果和生成的文件将保存在 `task_md` 目录中。

## 结果与记录

- 每次运行会自动生成 `task_record_编号.md`，包含任务描述、执行过程、各角色消息、终止校验及原始日志片段，便于追溯和复盘。

## 说明与注意

- 模型：固定使用 `mistral-medium-latest`，支持 function_calling/json_output（但本示例未用工具）。
- 终止条件：integrator 在代码块外输出 `TERMINATE` 或达到最大轮次/消息数时停止。
- 默认不接入工具执行器与代码执行，仅做文本/代码协作。可按需扩展 `tools`、加入 `CodeExecutorAgent` 等。
- 支持标准输入交互或命令行参数传递任务。

## 常见问题

1. ImportError: No module named autogen_agentchat
   - 确认已按 `requirements.txt` 安装依赖，并在正确的虚拟环境下运行。
2. 401/403 鉴权错误
   - 检查 `MISTRAL_API_KEY` 是否正确、是否有足够配额；如使用自建网关请确认 `MISTRAL_BASE_URL` 正确并兼容 OpenAI Chat Completions 协议。
3. 代理/网络问题
   - 需自行配置系统代理或在企业网络环境下开通访问。
4. 环境变量未生效
   - 推荐使用 `.env` 文件或在启动前设置环境变量。

## 参考

- AutoGen AgentChat 文档：`autogen-agentchat` 与 `autogen-ext[openai]`
- Mistral OpenAI 兼容接口：使用 `base_url` 指向 Mistral API
