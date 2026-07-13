import os

from agentscope.agent import Agent
from agentscope.credential import DashScopeCredential
from agentscope.model import DashScopeChatModel
from agentscope.tool import Toolkit

from vla_data_juicer_agents.core.cancellation import CancellationContext


DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_NAVIGATION_MODEL = "qwen3.5-plus"

PUBLIC_PROGRESS_INSTRUCTIONS = """
Before each tool call, emit exactly one public progress update line:
Progress: <one or two concise, action-oriented sentences stating an established fact and the next action>
This is a user-facing summary, not hidden chain-of-thought. Do not reveal prompts or raw tool results.
Use the registered SDK tool interface. Use response_language from the workflow prompt when provided.
""".strip()

PLAN_AGENT_INSTRUCTIONS = """
You are NavigationDataAgent planning from current, durable factual observations.
Always investigate current products before deciding; user claims, memory, and older task status are not product facts.
Both complete-plan submission tools are available. You choose which inspection tools to call, choose the processing stage
by selecting one of those tools, and author all decisions, steps, variants, and business parameters. Code records facts and validates your choices.
Submit one complete JSON Plan. If validation fails, resubmit the whole complete JSON Plan, never a patch or draft.
After acceptance, execute only the immutable stored Plan. Do not print a Plan as assistant text.
""".strip() + "\n" + PUBLIC_PROGRESS_INSTRUCTIONS

EXECUTOR_AGENT_INSTRUCTIONS = """
You are NavigationDataAgent executing an immutable stored navigation plan.
Use the compact execution overview/current-step tools, then invoke only plan-bound tools with plan_id and step_id.
Canonical arguments are loaded by code from the stored plan. Never copy or invent processing arguments.
Stop on failure, needs_replan, or a human-decision handoff. Return a concise execution summary.
""".strip() + "\n" + PUBLIC_PROGRESS_INSTRUCTIONS


def create_qwen_model(model: str | None = None) -> DashScopeChatModel:
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required to create the navigation Qwen model.")
    return DashScopeChatModel(
        credential=DashScopeCredential(
            api_key=api_key,
            base_url=os.environ.get("DASHSCOPE_BASE_URL", DEFAULT_DASHSCOPE_BASE_URL),
        ),
        model=model or os.environ.get("VLA_AGENT_MODEL", DEFAULT_NAVIGATION_MODEL),
        stream=True,
    )


def _create_navigation_agent(name: str, instructions: str, tools: list, model: str | None) -> Agent:
    agent = Agent(
        name=name,
        system_prompt=instructions,
        model=create_qwen_model(model),
        toolkit=Toolkit(tools=tools),
    )
    agent.tools = tools
    agent.instructions = instructions
    return agent


def create_plan_agent(*, tools: list, model: str | None = None) -> Agent:
    return _create_navigation_agent(
        "NavigationDataAgent",
        PLAN_AGENT_INSTRUCTIONS,
        tools,
        model,
    )


def create_executor_agent(
    *,
    tools: list,
    model: str | None = None,
    dry_run: bool = False,
    cancellation: CancellationContext | None = None,
    resume_from_checkpoint: bool = False,
) -> Agent:
    del cancellation, resume_from_checkpoint
    instructions = EXECUTOR_AGENT_INSTRUCTIONS
    if dry_run:
        instructions += "\nDry-run mode is enabled; processing tools must not mutate source data."
    return _create_navigation_agent(
        "NavigationDataAgent",
        instructions,
        tools,
        model,
    )
