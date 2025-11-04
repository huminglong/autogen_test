# AutoGen 三智能体工作流改进版本

本文档详细说明了 `improved_three_agent_workflow.py` 相对于原始 `three_agent_workflow.py` 的改进点，以及如何使用新功能。

## 主要改进点

### 1. 使用 AutoGen 0.7.4 新功能

#### SelectorGroupChat
- **原版本**: 使用 `RoundRobinGroupChat`，智能体按固定顺序轮流发言
- **改进版本**: 添加了 `--use-selector` 选项，可以使用 `SelectorGroupChat`，允许基于消息内容选择下一个说话者
- **优势**: 更灵活的对话流程，可以根据上下文动态选择最合适的智能体

#### 团队配置序列化
- **原版本**: 没有保存团队配置的功能
- **改进版本**: 添加了 `--save-config` 选项，可以将团队配置保存到JSON文件
- **优势**: 便于重用和分享团队配置，支持工作流的标准化和版本控制

### 2. 改进的终止条件

- **原版本**: 仅使用 `TextMentionTermination("TERMINATE")`
- **改进版本**: 使用组合终止条件 `TextMentionTermination("TERMINATE") | MaxMessageTermination(6)`
- **优势**: 防止无限循环，确保工作流在合理时间内完成

### 3. 更好的错误处理和日志记录

- **原版本**: 基本的错误处理
- **改进版本**: 增强了错误处理，添加了更详细的日志记录
- **优势**: 更容易调试和监控工作流执行

### 4. 代码结构优化

- **原版本**: 基本的三智能体工作流
- **改进版本**: 更模块化的代码结构，更清晰的函数分离
- **优势**: 更易于维护和扩展

### 5. 文档生成逻辑优化

- **原版本**: 基本的文档生成，可能存在重复代码块标记
- **改进版本**: 添加了 `_clean_code_fences` 函数，处理已经包含代码块标记的内容
- **优势**: 修复了文档中重复代码块标记的问题，确保生成的文档结构良好

## 使用方法

### 基本用法（与原版本相同）

```bash
python improved_three_agent_workflow.py --task "编写一个将CSV转换为JSON的Python脚本"
```

### 使用 SelectorGroupChat

```bash
python improved_three_agent_workflow.py --task "编写一个将CSV转换为JSON的Python脚本" --use-selector
```

### 保存团队配置

```bash
python improved_three_agent_workflow.py --task "编写一个将CSV转换为JSON的Python脚本" --save-config
```

### 组合使用新功能

```bash
python improved_three_agent_workflow.py --task "编写一个将CSV转换为JSON的Python脚本" --use-selector --save-config
```

### 使用自定义 API 配置

```bash
python improved_three_agent_workflow.py --task "编写一个将CSV转换为JSON的Python脚本" --mistral-api-key "your-api-key" --mistral-base-url "https://api.mistral.ai/v1"
```

## 输出文件

改进版本会生成以下文件：

1. `task_record_N.md`: 任务执行记录，与原版本相同
2. `team_config_N.json`: 团队配置文件（仅在使用 `--save-config` 选项时生成）

## 团队配置文件示例

```json
{
  "agents": [
    {
      "name": "coder",
      "description": "Agent 1: 根据用户开发需求编写代码",
      "system_message": "你是资深开发工程师(Agent 1: coder)。\n任务: 基于用户的开发需求，编写满足需求的完整、可运行代码。\n要求:\n- 尽量选择简单、稳健、无外部依赖或仅使用标准库的实现(除非需求明确)。\n- 若需求存在歧义，请做出最多两条合理假设并继续实现。\n- 输出仅包含最终代码，放在单个完整代码块中，不要添加解释或多余文本。\n- 在代码块外不要输出任何内容。"
    },
    {
      "name": "reviewer",
      "description": "Agent 2: 对 coder 的代码提出改进建议",
      "system_message": "你是代码审查专家(Agent 2: reviewer)。\n任务: 针对 coder 提供的代码，提出具体、可操作的改进建议(性能、可读性、健壮性、安全性、边界条件、测试等)。\n要求:\n- 请仅输出改进建议清单，不要粘贴或重写完整代码。\n- 如有明显缺陷，请明确指出并给出修复方向。\n- 建议使用有序或无序列表，每条建议尽量简洁。\n- 输出仅包含建议列表，避免其他冗余文本。"
    },
    {
      "name": "integrator",
      "description": "Agent 3: 综合 coder 代码与 reviewer 建议产出最终代码",
      "system_message": "你是集成与优化专家(Agent 3: integrator)。\n任务: 基于 coder 的初版代码和 reviewer 的改进建议，输出优化与完善后的最终代码。\n要求:\n- 最终输出仅包含完整、可运行的最终代码，放在单个完整代码块中。\n- 吸收 reviewer 的合理建议，修复缺陷并补充必要的注释/类型/错误处理。\n- 若需要轻微调整需求以确保可运行，请直接做并在代码注释中简述原因。\n- 在代码块外最后追加一行文本: TERMINATE\n- 除上述 TERMINATE 行外，不要输出其他任何解释或文字。"
    }
  ],
  "termination_condition": {
    "type": "TextMentionTermination",
    "text": "TERMINATE"
  },
  "team_type": "SelectorGroupChat"
}
```

## 进一步改进建议

1. **添加更多团队类型**: 考虑添加 `Swarm` 团队类型，这是 AutoGen 0.7.4 的另一个新功能
2. **动态终止条件**: 根据任务复杂度动态调整终止条件
3. **智能体性能监控**: 添加智能体响应时间和质量监控
4. **工作流模板**: 创建预定义的工作流模板，适用于不同类型的任务
5. **集成外部工具**: 添加代码执行和测试功能，自动验证生成的代码

## 总结

改进版本充分利用了 AutoGen 0.7.4 的新功能，提供了更灵活、更强大的三智能体工作流。通过 `SelectorGroupChat` 和团队配置序列化，用户可以更精细地控制工作流，并保存和重用成功的配置。这些改进使得工作流更加适应不同的任务需求，提高了开发效率和代码质量。