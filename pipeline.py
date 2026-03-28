"""
nem pipeline — importable module.
Exposes run_pipeline() and run_answer_pipeline().
"""

import asyncio
import re
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgent, ClaudeAgentOptions, query, ResultMessage

AGENTS_DIR = Path(__file__).parent / "agents"


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

def _log(logs: list, step: str, inp: str, out: str, start: float):
    logs.append({
        "step": step,
        "in": inp[:500],
        "out": out[:500],
        "ms": int((time.time() - start) * 1000),
    })


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_section(output: str, header: str) -> list[str]:
    """Shared parser — handles both **HEADER** and ## HEADER styles."""
    pattern = rf"(?:\*\*{re.escape(header)}\*\*|## {re.escape(header)})(.*?)(?=\n(?:\*\*[A-Z]|## [A-Z])|\Z)"
    match = re.search(pattern, output, re.DOTALL | re.IGNORECASE)
    if not match:
        return []
    section = match.group(1).strip()
    lines = [l.strip() for l in section.splitlines() if l.strip()]
    return [l.lstrip("0123456789. ") for l in lines if l]


def parse_questions(output: str) -> list[str]:
    """Extract questions from a QUESTIONS section (** or ## style)."""
    return _parse_section(output, "QUESTIONS")


def parse_behavioral_questions(output: str) -> list[str]:
    """Extract behavioral prerequisite questions from JAL_2 output."""
    return _parse_section(output, "BEHAVIORAL_QUESTIONS")


def parse_context_questions(output: str) -> list[str]:
    """Extract scope/context questions from JAL_2 output (VAL will attempt these)."""
    return _parse_section(output, "CONTEXT_QUESTIONS")


def parse_steps(text: str) -> list[str]:
    """Parse a numbered list into individual step strings."""
    lines = text.strip().split("\n")
    steps, current = [], []
    for line in lines:
        if re.match(r"^\d+[.:]", line.strip()):
            if current:
                steps.append("\n".join(current).strip())
            current = [line]
        elif current:
            current.append(line)
    if current:
        steps.append("\n".join(current).strip())
    return steps if steps else [text]


def extract_section(text: str, header: str) -> str:
    """Extract content under a **HEADER** block from agent output."""
    pattern = rf"\*\*{re.escape(header)}\*\*\s*\n(.*?)(?=\n\*\*[A-Z]|\Z)"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


# ---------------------------------------------------------------------------
# Agent runners
# ---------------------------------------------------------------------------

async def run_agent(prompt_file: str, task: str, options: ClaudeAgentOptions, extra: str = "") -> str:
    """Run a single agent query and return the result text."""
    prompt = (AGENTS_DIR / prompt_file).read_text()
    full_prompt = f"{prompt}\n\n---\n\nTask: {task}"
    if extra:
        full_prompt += f"\n\n{extra}"
    result = ""
    async for msg in query(prompt=full_prompt, options=options):
        if isinstance(msg, ResultMessage):
            result = msg.result or ""
    return result


async def run_shaper(task: str, bal_output: str, jal2_output: str, options: ClaudeAgentOptions) -> str:
    """Run BAL Mode 4 (shaper) — receives THOUGHT_PROCESS only, not raw behavioral patterns."""
    bal_text = (AGENTS_DIR / "bal.md").read_text()
    mode4_match = re.search(r"## Mode 4: Plan Shaper(.*)", bal_text, re.DOTALL)
    mode4 = mode4_match.group(1).strip() if mode4_match else ""

    thought_process = extract_section(jal2_output, "THOUGHT_PROCESS") or jal2_output

    prompt = f"""You are BAL (Breakdown Agent Layer) in Mode 4: Plan Shaper.

{mode4}

---

## Raw first-principles plan:
{bal_output}

## JAL_2 Thought Process:
{thought_process}

## Task:
{task}

Produce the revised ordered step list only — no preamble."""

    result = ""
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            result = msg.result or ""
    return result if result else bal_output


async def run_val(question: str, task: str, options: ClaudeAgentOptions) -> str:
    """Run VAL for a single context question. Returns ANSWER or NOT FOUND."""
    prompt = (AGENTS_DIR / "val.md").read_text()
    full_prompt = f"""{prompt}

---

## Context question:
{question}

## Original task:
{task}"""
    result = ""
    async for msg in query(prompt=full_prompt, options=options):
        if isinstance(msg, ResultMessage):
            result = msg.result or ""
    return result if result else f"NOT FOUND: VAL returned empty for: {question}"


async def flush_step(step: str, step_number: int, thought_process: str, options: ClaudeAgentOptions) -> str:
    """Flush a single step using BAL Mode 3, grounded in JAL_2 thought process."""
    bal_text = (AGENTS_DIR / "bal.md").read_text()
    mode3_match = re.search(r"## Mode 3: Flush a Single Action(.*?)(?=^## |\Z)", bal_text, re.DOTALL | re.MULTILINE)
    mode3 = mode3_match.group(1).strip() if mode3_match else ""

    prompt = f"""You are BAL (Breakdown Agent Layer) in Mode 3: Flush a Single Action.

{mode3}

---

## Action to flush (Step {step_number}):
{step}

## JAL_2 Thought Process (for grounding):
{thought_process}

Produce the flushed step only — no preamble."""

    result = ""
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            result = msg.result or ""
    return result if result else f"**Step {step_number}**\n{step}"


# ---------------------------------------------------------------------------
# Public pipeline functions
# ---------------------------------------------------------------------------

async def run_pipeline(
    task: str,
    mcp_options,  # ClaudeAgentOptions
    base_options,  # ClaudeAgentOptions
) -> dict:
    """
    Returns {status, output, logs, session_data}
    status: 'complete' | 'questions' | 'missing_connector' | 'error'
    logs: [{step, in, out, ms}]
    session_data: {task, jal2_output, bal_output, questions} — only set when status='questions'
    """
    logs = []

    # Step 0: JAL_0 — MCP land survey (sequential gate)
    start = time.time()
    jal0_output = await run_agent("jal_0.md", task, mcp_options)
    _log(logs, "jal_0", task, jal0_output, start)

    if "Missing connector:" in jal0_output:
        return {
            "status": "missing_connector",
            "output": jal0_output,
            "logs": logs,
            "session_data": None,
        }

    # Steps 1+2: BAL Mode 1 + JAL_2 in parallel
    bal_start = time.time()
    jal2_start = time.time()

    results = await asyncio.gather(
        run_agent("bal_mode1.md", task, base_options),
        run_agent("jal_2.md", task, mcp_options),
        return_exceptions=True,
    )
    bal_output, jal2_output = results

    if isinstance(bal_output, Exception):
        return {
            "status": "error",
            "output": f"BAL failed: {bal_output}",
            "logs": logs,
            "session_data": None,
        }
    if isinstance(jal2_output, Exception):
        return {
            "status": "error",
            "output": f"JAL_2 failed: {jal2_output}",
            "logs": logs,
            "session_data": None,
        }

    _log(logs, "bal_mode1", task, bal_output, bal_start)
    _log(logs, "jal_2", task, jal2_output, jal2_start)

    # Questions gate: VAL resolves context questions, behavioral questions go to user
    behavioral_qs = parse_behavioral_questions(jal2_output)
    context_qs = parse_context_questions(jal2_output)

    # Run VAL in parallel for all context questions
    if context_qs:
        val_starts = [time.time() for _ in context_qs]
        val_results = await asyncio.gather(
            *[run_val(q, task, mcp_options) for q in context_qs],
            return_exceptions=True,
        )
        for i, (q, result) in enumerate(zip(context_qs, val_results)):
            _log(logs, f"val_{i}", q, str(result), val_starts[i])
    else:
        val_results = []

    # Inject resolved answers into jal2_output; collect unresolved for escalation
    val_unresolved = []
    for q, result in zip(context_qs, val_results):
        if isinstance(result, Exception) or "NOT FOUND" in str(result):
            val_unresolved.append(q)
        else:
            jal2_output += f"\n\n**VAL RESOLVED:** {q}\n{result}\n"

    # Build final question list — max 2, behavioral first
    final_questions = list(behavioral_qs[:2])
    if val_unresolved and len(final_questions) < 2:
        final_questions.append(val_unresolved[0])
    final_questions = final_questions[:2]

    if final_questions:
        return {
            "status": "questions",
            "output": None,
            "logs": logs,
            "session_data": {
                "task": task,
                "jal2_output": jal2_output,
                "bal_output": bal_output,
                "questions": final_questions,
            },
        }

    # Steps 3+4: Shaper — shape the plan against jal_2 thought process
    shaper_start = time.time()
    shaped_plan = await run_shaper(task, bal_output, jal2_output, base_options)
    _log(logs, "shaper", f"{task[:250]}", shaped_plan, shaper_start)

    # Step 5: BAL Mode 3 flush × N — pure reasoning, no MCP
    actions = parse_steps(shaped_plan)
    thought_process = extract_section(jal2_output, "THOUGHT_PROCESS") or jal2_output

    flush_starts = [time.time() for _ in actions]
    flushed = await asyncio.gather(
        *[flush_step(action, i + 1, thought_process, base_options) for i, action in enumerate(actions)],
        return_exceptions=True,
    )

    final_steps = []
    for i, result in enumerate(flushed):
        step_out = result if not isinstance(result, Exception) else f"**Step {i + 1}**\n{actions[i]}"
        _log(logs, f"flush_{i}", actions[i], str(step_out), flush_starts[i])
        final_steps.append(step_out)

    final_plan = "\n\n".join(final_steps)

    return {
        "status": "complete",
        "output": final_plan,
        "logs": logs,
        "session_data": None,
    }


async def run_answer_pipeline(
    session_data: dict,
    answers: str,
    mcp_options,  # ClaudeAgentOptions
    base_options,  # ClaudeAgentOptions
) -> dict:
    """
    Returns {status, output, logs}
    """
    logs = []

    task = session_data["task"]
    bal_output = session_data["bal_output"]
    jal2_output = session_data["jal2_output"]

    if answers:
        jal2_output += f"\n\n**USER ANSWERED THESE PREREQUISITE QUESTIONS:**\n{answers}\n"

    # Shaper
    shaper_start = time.time()
    shaped_plan = await run_shaper(task, bal_output, jal2_output, base_options)
    _log(logs, "shaper", task, shaped_plan, shaper_start)

    actions = parse_steps(shaped_plan)
    thought_process = extract_section(jal2_output, "THOUGHT_PROCESS") or jal2_output

    flush_starts = [time.time() for _ in actions]
    flushed = await asyncio.gather(
        *[flush_step(action, i + 1, thought_process, base_options) for i, action in enumerate(actions)],
        return_exceptions=True,
    )

    final_steps = []
    for i, result in enumerate(flushed):
        step_out = result if not isinstance(result, Exception) else f"**Step {i + 1}**\n{actions[i]}"
        _log(logs, f"flush_{i}", actions[i], str(step_out), flush_starts[i])
        final_steps.append(step_out)

    final_plan = "\n\n".join(final_steps)

    return {
        "status": "complete",
        "output": final_plan,
        "logs": logs,
    }
