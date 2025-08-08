import asyncio
import os
import sys
import argparse
from typing import Optional, List, Dict, Any
import datetime
import re
import glob

# 尝试加载.env文件中的环境变量
from dotenv import load_dotenv

load_dotenv()

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_agentchat.conditions import TextMentionTermination, MaxMessageTermination
from autogen_agentchat.ui import Console
from autogen_ext.models.openai import OpenAIChatCompletionClient


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
        description="Agent 1: 根据用户开发需求编写代码",
        system_message=(
            "你是资深开发工程师(Agent 1: coder)。\n"
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
        description="Agent 2: 对 coder 的代码提出改进建议",
        system_message=(
            "你是代码审查专家(Agent 2: reviewer)。\n"
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
        description="Agent 3: 综合 coder 代码与 reviewer 建议产出最终代码",
        system_message=(
            "你是集成与优化专家(Agent 3: integrator)。\n"
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
        if stripped.startswith("```") and stripped.endswith("```"):
            out.append(stripped + "\n\n")
        else:
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
            "检测到 ‘TERMINATE’ 后停止（符合配置）。\n" if self.terminated_by else "未检测到 TERMINATE。\n"))
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
    record_files = glob.glob("task_record_*.md")
    if not record_files:
        return 1
    numbers: List[int] = []
    for filename in record_files:
        match = re.match(r"task_record_(\d+)\.md", os.path.basename(filename))
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers) + 1 if numbers else 1


async def run_workflow(task: str, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
    # 初始化记录器
    execution_number = get_next_execution_number()
    record_filename = f"task_record_{execution_number}.md"
    recorder = TaskRecorder(task, execution_number)

    model_client = build_model_client(api_key=api_key, base_url=base_url)
    coder, reviewer, integrator = build_agents(model_client)

    termination = TextMentionTermination("TERMINATE") | MaxMessageTermination(6)
    team = RoundRobinGroupChat([coder, reviewer, integrator], termination_condition=termination, max_turns=3)

    # 收集事件并打印到控制台
    async for message in team.run_stream(task=task):
        source = getattr(message, "source", getattr(message, "name", "unknown")) or "unknown"
        content = getattr(message, "content", None)
        if content is None:
            # Try common alternative attribute
            content = getattr(message, "text", None)
        if content is None:
            content = str(message)
        recorder.add_message(source, str(content))

        # 控制台轻量输出
        print(f"----- {source} -----")
        preview = content if isinstance(content, str) else str(content)
        print(preview if len(preview) < 2000 else preview[:2000] + "…")
        print()

    recorder.finalize()
    recorder.write(record_filename)

    await model_client.close()


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="3-Agent AutoGen 工作流: coder -> reviewer -> integrator")
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
    task = prompt_if_needed(args.task)
    if not task:
        print("[ERROR] 必须提供开发需求 --task 或在提示符输入。", file=sys.stderr)
        return 2
    asyncio.run(run_workflow(task, api_key=args.mistral_api_key, base_url=args.mistral_base_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
