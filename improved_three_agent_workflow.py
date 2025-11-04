import asyncio
import os
import sys
import argparse
import json
from typing import Optional, List, Dict, Any
import datetime
import re
import glob

# 尝试加载.env文件中的环境变量
from dotenv import load_dotenv

load_dotenv()

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat, SelectorGroupChat
from autogen_agentchat.conditions import (
    TextMentionTermination, 
    MaxMessageTermination, 
    ExternalTermination,
    TimeoutTermination,
    SourceMatchTermination
)
from autogen_agentchat.ui import Console
from autogen_agentchat.messages import TextMessage, BaseAgentEvent, BaseChatMessage
from autogen_agentchat.base import TaskResult
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_core import CancellationToken


def build_model_client(api_key: Optional[str] = None, base_url: Optional[str] = None) -> OpenAIChatCompletionClient:
    """Create an OpenAI-compatible chat completion client targeting Mistral.

    Priority of configuration:
    - MISTRAL_API_KEY env var (required unless api_key is provided explicitly)
    - MISTRAL_BASE_URL env var or default "https://api.mistral.ai/v1"
    Model is fixed to "mistral-medium-latest" per requirements.
    """
    key = api_key or os.environ.get("MISTRAL_API_KEY")
    if not key:
        print("[ERROR] Missing MISTRAL_API_KEY. Please set it in your environment.", file=sys.stderr)
        sys.exit(1)

    url = base_url or os.environ.get("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")

    # Use OpenAI-compatible client with base_url override to call Mistral's Chat Completions API
    return OpenAIChatCompletionClient(
        model="mistral-medium-latest",
        api_key=key,
        base_url=url,
        model_info={
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "family": "mistral",
            "structured_output": False,
        },
        # You may tune these defaults if needed
        temperature=0.2,
    )


def build_agents(model_client: OpenAIChatCompletionClient):
    """Define the three-role workflow: coder -> reviewer -> integrator."""
    coder = AssistantAgent(
        name="coder",
        model_client=model_client,
        description=(
            "初始代码编写专家。负责根据用户需求编写第一版完整可运行的代码实现。"
            "擅长选择简单稳健的技术方案，处理需求歧义，快速产出可工作的代码原型。"
            "当收到新的开发任务时，应该首先由该代理开始工作。"
        ),
        system_message=(
            "你是资深开发工程师(coder)。\n"
            "任务: 基于用户的开发需求，编写满足需求的完整、可运行代码。\n"
            "要求:\n"
            "- 尽量选择简单、稳健、无外部依赖或仅使用标准库的实现(除非需求明确)。\n"
            "- 若需求存在歧义，请做出最多两条合理假设并继续实现。\n"
            "- 输出仅包含最终代码，放在单个完整代码块中，不要添加解释或多余文本。\n"
            "- 在代码块外不要输出任何内容。"
        ),
    )

    reviewer = AssistantAgent(
        name="reviewer",
        model_client=model_client,
        description=(
            "代码审查与质量保证专家。负责对 coder 生成的代码进行深度审查，"
            "从性能、安全性、可读性、健壮性、边界条件、测试覆盖等多个维度提供改进建议。"
            "仅在 coder 完成初始代码后才开始工作。"
        ),
        system_message=(
            "你是代码审查专家(reviewer)。\n"
            "任务: 针对 coder 提供的代码，提出具体、可操作的改进建议(性能、可读性、健壮性、安全性、边界条件、测试等)。\n"
            "要求:\n"
            "- 请仅输出改进建议清单，不要粘贴或重写完整代码。\n"
            "- 如有明显缺陷，请明确指出并给出修复方向。\n"
            "- 建议使用有序或无序列表，每条建议尽量简洁。\n"
            "- 输出仅包含建议列表，避免其他冗余文本。"
        ),
    )

    integrator = AssistantAgent(
        name="integrator",
        model_client=model_client,
        description=(
            "代码集成与优化专家。负责整合 coder 的初始代码和 reviewer 的审查建议，"
            "产出经过优化和完善的最终生产级代码。确保所有建议被合理采纳，代码质量达到最高标准。"
            "仅在 reviewer 完成审查后才开始工作，完成后输出 TERMINATE 结束流程。"
        ),
        system_message=(
            "你是集成与优化专家(integrator)。\n"
            "任务: 基于 coder 的初版代码和 reviewer 的改进建议，输出优化与完善后的最终代码。\n"
            "要求:\n"
            "- 最终输出仅包含完整、可运行的最终代码，放在单个完整代码块中。\n"
            "- 吸收 reviewer 的合理建议，修复缺陷并补充必要的注释/类型/错误处理。\n"
            "- 若需要轻微调整需求以确保可运行，请直接做并在代码注释中简述原因。\n"
            "- 在代码块外最后追加一行文本: TERMINATE\n"
            "- 除上述 TERMINATE 行外，不要输出其他任何解释或文字。"
        ),
    )

    return coder, reviewer, integrator


# ---- Intelligent Selector Functions for SelectorGroupChat ----
def create_selector_func():
    """创建智能选择器函数，根据消息内容选择下一个发言者"""
    def selector_func(messages: List[BaseAgentEvent | BaseChatMessage]) -> str | None:
        """根据对话上下文智能选择下一个代理"""
        if not messages:
            return "coder"  # 空消息时，从 coder 开始
        
        last_message = messages[-1]
        source = getattr(last_message, "source", None)
        
        # 用户输入后，让 coder 开始工作
        if source == "user":
            return "coder"
        
        # coder 完成后，交给 reviewer 审查
        elif source == "coder":
            return "reviewer"
        
        # reviewer 完成后，交给 integrator 整合
        elif source == "reviewer":
            return "integrator"
        
        # 其他情况让 LLM 自动选择
        return None
    
    return selector_func


def create_candidate_func():
    """创建候选函数，预筛选可能的下一个发言者"""
    def candidate_func(messages: List[BaseAgentEvent | BaseChatMessage]) -> List[str]:
        """根据对话流程预筛选候选代理"""
        if not messages:
            return ["coder"]  # 开始时只有 coder 可选
        
        last_message = messages[-1]
        source = getattr(last_message, "source", None)
        
        # 用户输入后，只能选择 coder
        if source == "user":
            return ["coder"]
        
        # coder 完成后，只能选择 reviewer
        elif source == "coder":
            return ["reviewer"]
        
        # reviewer 完成后，只能选择 integrator
        elif source == "reviewer":
            return ["integrator"]
        
        # integrator 完成后，任务应该结束（已有 TERMINATE）
        elif source == "integrator":
            return ["integrator"]  # 允许但会被终止条件拦截
        
        # 默认返回所有代理
        return ["coder", "reviewer", "integrator"]
    
    return candidate_func


def create_selector_prompt() -> str:
    """创建自定义选择器提示词"""
    return """根据当前对话上下文选择最合适的代理来执行下一步任务。

代理角色说明：
{roles}

当前对话历史：
{history}

请从 {participants} 中选择一个代理。

选择原则：
1. 如果是新任务或用户刚输入需求，选择 coder 开始编码
2. 如果 coder 刚完成代码，选择 reviewer 进行审查
3. 如果 reviewer 已给出建议，选择 integrator 整合优化
4. 如果 integrator 已完成，任务应该结束

只需返回代理名称，不要额外解释。
"""


# ---- Task Record Utilities ----
ROLE_ORDER = ["user", "coder", "reviewer", "integrator"]


def _guess_is_code(text: str) -> bool:
    if "\n" not in text and len(text) > 400:
        return True
    hints = ["def ", "class ", "import ", "from ", "#!/usr/bin", "console.log", "public "]
    return any(h in text for h in hints)


def _strip_fenced_block_if_list(text: str) -> str:
    """If text is a fenced block whose body is mostly list items, strip the fences."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        body = stripped.split("\n", 1)[1]
        if body.endswith("```"):
            body = body[:-3]
        lines = [l.strip() for l in body.strip().splitlines()]
        if lines and sum(1 for l in lines if l.startswith("-") or l[:2] in {"- ", "* ", "+ "}) >= max(1, int(0.6 * len(
                lines))):
            return "\n".join(lines)
    return text


def _clean_code_fences(text: str) -> str:
    """Clean up code fences to avoid duplication and ensure proper formatting."""
    stripped = text.strip()
    
    # If content doesn't start with code fences, return as is
    if not stripped.startswith("```"):
        return stripped
    
    # Extract the content between the first and last code fences
    lines = stripped.split("\n")
    
    # Find the first line that starts with ```
    start_idx = 0
    for i, line in enumerate(lines):
        if line.startswith("```"):
            start_idx = i
            break
    
    # Find the last line that starts with ```
    end_idx = len(lines) - 1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("```"):
            end_idx = i
            break
    
    # If we found proper code fences, extract the content between them
    if start_idx < end_idx:
        content_lines = lines[start_idx + 1:end_idx]
        return "\n".join(content_lines)
    
    # If something went wrong, return the original text
    return stripped


class TaskRecorder:
    def __init__(self, task: str, execution_number: int) -> None:
        self.task = task
        self.execution_number = execution_number
        self.start_time = datetime.datetime.now()
        self.end_time: Optional[datetime.datetime] = None
        self.messages: List[Dict[str, Any]] = []
        self.terminated_by: Optional[str] = None

    def add_message(self, source: str, content: str) -> None:
        role = (source or "unknown").lower()
        self.messages.append({"role": role, "content": content})
        if "TERMINATE" in content:
            self.terminated_by = "TERMINATE"

    def finalize(self) -> None:
        self.end_time = datetime.datetime.now()

    # Formatting helpers
    def _format_message(self, role: str, content: str) -> str:
        role = role.lower()
        out = []
        title_map = {
            "user": "user",
            "coder": "coder（生成初版代码）",
            "reviewer": "reviewer（改进建议）",
            "integrator": "integrator（融合产出最终代码）",
        }
        out.append(f"### {title_map.get(role, role)}\n")

        if role == "reviewer":
            content = _strip_fenced_block_if_list(content)
            out.append(content.strip() + "\n\n")
            return "".join(out)

        # coder/integrator prefer code fences; preserve if already fenced
        stripped = content.strip()
        
        # Check if content already has code fences
        has_code_fences = stripped.startswith("```") and stripped.endswith("```")
        
        if has_code_fences:
            # Content already has code fences, clean them and add proper ones
            cleaned_content = _clean_code_fences(stripped)
            if _guess_is_code(cleaned_content) or role in {"coder", "integrator"}:
                out.append("```python\n" + cleaned_content + "\n```\n\n")
            else:
                out.append("```\n" + cleaned_content + "\n```\n\n")
        else:
            # Content doesn't have code fences, add them based on content type
            if _guess_is_code(stripped) or role in {"coder", "integrator"}:
                out.append("```python\n" + stripped + "\n```\n\n")
            else:
                out.append("```\n" + stripped + "\n```\n\n")
        return "".join(out)

    def _workflow_check(self) -> str:
        roles_in_order = [m["role"] for m in self.messages]

        def first_index(r: str) -> int:
            try:
                return roles_in_order.index(r)
            except ValueError:
                return 10 ** 9

        ok_order = (
                first_index("user") < first_index("coder") < first_index("reviewer") < first_index("integrator")
        )
        lines = ["## 工作流校验\n"]
        lines.append(f"- 顺序：user → coder → reviewer → integrator（{'符合' if ok_order else '不符合'}预期）。\n")
        lines.append("- 终止条件：" + (
            "检测到 'TERMINATE' 后停止（符合配置）。\n" if self.terminated_by else "未检测到 TERMINATE。\n"))
        return "".join(lines) + "\n"

    def _appendix_raw(self) -> str:
        # keep concise raw dump
        lines = ["## 附录：原始消息日志（节选）\n\n", "```text\n"]
        for m in self.messages[:8]:
            snippet = (m["content"][:200] + ("…" if len(m["content"]) > 200 else "")).replace("\n", " ")
            lines.append(f"[{m['role']}] {snippet}\n")
        lines.append("```\n\n")
        return "".join(lines)

    def to_markdown(self) -> str:
        if not self.end_time:
            self.finalize()
        duration = self.end_time - self.start_time if self.end_time else datetime.timedelta(0)
        parts: List[str] = []
        parts.append(f"# 任务执行记录 #{self.execution_number}\n\n")
        parts.append("## 任务描述\n\n" + self.task.strip() + "\n\n")
        parts.append("## 执行时间\n\n")
        parts.append(f"- 开始时间: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        parts.append(f"- 结束时间: {self.end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        parts.append(f"- 执行时长: {duration}\n\n")
        parts.append("## 执行过程\n\n")

        for m in self.messages:
            parts.append(self._format_message(m["role"], m["content"]))

        parts.append(self._workflow_check())
        parts.append(self._appendix_raw())
        parts.append("---\n\n此记录由系统自动生成。\n")
        return "".join(parts)

    def write(self, filename: str) -> None:
        md = self.to_markdown()
        with open(filename, "w", encoding="utf-8") as f:
            f.write(md)


def get_next_execution_number() -> int:
    """获取下一个执行编号"""
    # 确保task_md文件夹存在
    os.makedirs("task_md", exist_ok=True)
    record_files = glob.glob("task_md/task_record_*.md")
    if not record_files:
        return 1
    numbers: List[int] = []
    for filename in record_files:
        match = re.match(r"task_record_(\d+)\.md", os.path.basename(filename))
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers) + 1 if numbers else 1


async def run_workflow(
    task: str, 
    api_key: Optional[str] = None, 
    base_url: Optional[str] = None, 
    use_selector: bool = False, 
    save_config: bool = False,
    resume_from: Optional[str] = None,
    use_console_ui: bool = True,
    timeout_seconds: int = 600
) -> None:
    """运行三代理工作流
    
    Args:
        task: 用户的开发需求
        api_key: Mistral API Key
        base_url: Mistral API Base URL
        use_selector: 是否使用 SelectorGroupChat（智能选择）而非 RoundRobinGroupChat
        save_config: 是否保存团队配置
        resume_from: 从指定状态文件恢复会话（JSON文件路径）
        use_console_ui: 是否使用 AutoGen 的 Console UI
        timeout_seconds: 任务超时时间（秒）
    """
    # 初始化记录器
    execution_number = get_next_execution_number()
    record_filename = f"task_md/task_record_{execution_number}.md"
    state_filename = f"task_md/team_state_{execution_number}.json"
    recorder = TaskRecorder(task, execution_number)

    model_client = build_model_client(api_key=api_key, base_url=base_url)
    
    try:
        coder, reviewer, integrator = build_agents(model_client)

        # 改进的终止条件 - 组合多种条件提供全面保护
        termination = (
            TextMentionTermination("TERMINATE") |           # 检测 TERMINATE 关键词
            MaxMessageTermination(20) |                     # 最多20条消息防止无限循环
            TimeoutTermination(timeout_seconds) |           # 超时保护
            SourceMatchTermination(["integrator"])          # integrator 完成后可结束
        )
        
        # 根据参数选择团队类型
        if use_selector:
            # 使用 SelectorGroupChat - 基于消息内容智能选择下一个发言者
            print("使用 SelectorGroupChat 模式（智能选择）")
            team = SelectorGroupChat(
                participants=[coder, reviewer, integrator],
                model_client=model_client,
                termination_condition=termination,
                selector_func=create_selector_func(),
                candidate_func=create_candidate_func(),
                selector_prompt=create_selector_prompt(),
                allow_repeated_speaker=False  # 不允许同一代理连续发言
            )
        else:
            # 使用 RoundRobinGroupChat - 固定顺序轮流发言
            print("使用 RoundRobinGroupChat 模式（轮流发言）")
            team = RoundRobinGroupChat(
                [coder, reviewer, integrator], 
                termination_condition=termination
            )
        
        # 如果指定了恢复点，加载之前的状态
        if resume_from and os.path.exists(resume_from):
            print(f"从状态文件恢复会话: {resume_from}")
            with open(resume_from, "r", encoding="utf-8") as f:
                saved_state = json.load(f)
            await team.load_state(saved_state)
        
        # 保存团队配置
        if save_config:
            config = {
                "agents": [
                    {
                        "name": agent.name, 
                        "description": agent.description, 
                        "system_message": agent.system_message
                    }
                    for agent in [coder, reviewer, integrator]
                ],
                "termination_condition": {
                    "types": ["TextMentionTermination", "MaxMessageTermination", "TimeoutTermination", "SourceMatchTermination"],
                    "details": {
                        "text_mention": "TERMINATE",
                        "max_messages": 20,
                        "timeout_seconds": timeout_seconds,
                        "source_match": ["integrator"]
                    }
                },
                "team_type": "SelectorGroupChat" if use_selector else "RoundRobinGroupChat",
                "execution_number": execution_number
            }
            
            config_filename = f"task_md/team_config_{execution_number}.json"
            with open(config_filename, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            print(f"团队配置已保存到 {config_filename}")

        # 执行任务
        if use_console_ui:
            # 使用 AutoGen 的 Console UI - 提供更好的格式化输出
            print(f"\n{'='*60}")
            print(f"开始执行任务 (执行编号: {execution_number})")
            print(f"{'='*60}\n")
            
            # 使用 Console UI 流式输出，并收集消息用于记录
            async for message in team.run_stream(task=task):
                # 提取消息信息
                source = getattr(message, "source", "unknown")
                
                # 处理不同类型的消息
                if isinstance(message, TaskResult):
                    content = f"任务完成 - 停止原因: {message.stop_reason}"
                else:
                    content = getattr(message, "content", None)
                    if content is None:
                        content = str(message)
                
                # 记录消息
                recorder.add_message(source, str(content))
                
                # 使用 Console 格式化输出
                print(f"\n{'─'*60}")
                print(f"📤 {source}")
                print(f"{'─'*60}")
                preview = content if isinstance(content, str) else str(content)
                print(preview if len(preview) < 3000 else preview[:3000] + "\n... (内容过长，已截断) ...")
        else:
            # 使用自定义轻量输出
            async for message in team.run_stream(task=task):
                source = getattr(message, "source", "unknown")
                content = getattr(message, "content", None)
                if content is None:
                    content = str(message)
                recorder.add_message(source, str(content))

                print(f"----- {source} -----")
                preview = content if isinstance(content, str) else str(content)
                print(preview if len(preview) < 2000 else preview[:2000] + "…")
                print()

        # 保存团队状态（用于可能的恢复）
        team_state = await team.save_state()
        with open(state_filename, "w", encoding="utf-8") as f:
            json.dump(team_state, f, ensure_ascii=False, indent=2)
        print(f"\n团队状态已保存到 {state_filename}")
        
        # 完成记录
        recorder.finalize()
        recorder.write(record_filename)
        print(f"执行记录已保存到 {record_filename}\n")
        
    except asyncio.CancelledError:
        print("\n任务被用户取消")
        recorder.add_message("system", "任务被用户取消")
        recorder.finalize()
        recorder.write(record_filename)
        raise
        
    except Exception as e:
        print(f"\n执行出错: {e}")
        import traceback
        traceback.print_exc()
        recorder.add_message("system", f"Error: {str(e)}\n{traceback.format_exc()}")
        recorder.finalize()
        recorder.write(record_filename)
        raise
        
    finally:
        # 确保关闭模型客户端连接
        await model_client.close()


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="3-Agent AutoGen 工作流: coder -> reviewer -> integrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  # 基本用法（RoundRobin模式）
  python improved_three_agent_workflow.py --task "编写一个CSV转JSON的Python脚本"
  
  # 使用智能选择器模式
  python improved_three_agent_workflow.py --task "实现快速排序算法" --use-selector
  
  # 保存配置和状态
  python improved_three_agent_workflow.py --task "创建REST API客户端" --save-config
  
  # 从之前的状态恢复
  python improved_three_agent_workflow.py --resume task_md/team_state_1.json
  
  # 自定义超时和禁用Console UI
  python improved_three_agent_workflow.py --task "数据分析脚本" --timeout 300 --no-console-ui
        """
    )
    parser.add_argument(
        "--task",
        required=False,
        help="用户的开发需求描述，例如: '编写一个将CSV转换为JSON的Python脚本'",
    )
    parser.add_argument(
        "--mistral-api-key",
        dest="mistral_api_key",
        default=None,
        help="可选。显式传入 Mistral API Key；如不提供则从环境变量 MISTRAL_API_KEY 读取。",
    )
    parser.add_argument(
        "--mistral-base-url",
        dest="mistral_base_url",
        default=None,
        help="可选。覆盖默认的 Mistral API Base URL，默认 https://api.mistral.ai/v1",
    )
    parser.add_argument(
        "--use-selector",
        dest="use_selector",
        action="store_true",
        help="使用 SelectorGroupChat 替代 RoundRobinGroupChat（智能选择下一个发言者）",
    )
    parser.add_argument(
        "--save-config",
        dest="save_config",
        action="store_true",
        help="保存团队配置到JSON文件",
    )
    parser.add_argument(
        "--resume",
        dest="resume_from",
        default=None,
        help="从指定的状态文件恢复会话（JSON文件路径），例如: task_md/team_state_1.json",
    )
    parser.add_argument(
        "--no-console-ui",
        dest="no_console_ui",
        action="store_true",
        help="禁用 AutoGen Console UI，使用简单的文本输出",
    )
    parser.add_argument(
        "--timeout",
        dest="timeout_seconds",
        type=int,
        default=600,
        help="任务执行超时时间（秒），默认600秒（10分钟）",
    )
    return parser.parse_args(argv)


def prompt_if_needed(text: Optional[str]) -> str:
    if text and text.strip():
        return text
    try:
        return input("请输入你的开发需求: ").strip()
    except EOFError:
        return ""


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    
    # 如果指定了恢复，任务可以为空
    if args.resume_from:
        if not os.path.exists(args.resume_from):
            print(f"[ERROR] 恢复文件不存在: {args.resume_from}", file=sys.stderr)
            return 2
        print(f"将从状态文件恢复: {args.resume_from}")
        task = args.task or "继续之前的任务"
    else:
        task = prompt_if_needed(args.task)
        if not task:
            print("[ERROR] 必须提供开发需求 --task 或在提示符输入。", file=sys.stderr)
            return 2
    
    asyncio.run(run_workflow(
        task=task, 
        api_key=args.mistral_api_key, 
        base_url=args.mistral_base_url,
        use_selector=args.use_selector,
        save_config=args.save_config,
        resume_from=args.resume_from,
        use_console_ui=not args.no_console_ui,
        timeout_seconds=args.timeout_seconds
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))