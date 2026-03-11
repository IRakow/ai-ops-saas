"""
AI Ops Claude Code CLI Helper
Wrapper around the `claude` CLI for running agent prompts.
"""

import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("ai_ops.claude")


def run_claude(prompt, cwd, allowed_tools=None, timeout=300, output_format="json",
               disable_tools=False, model=None):
    """Run Claude Code CLI and return parsed result.

    Args:
        prompt: The prompt to send to Claude.
        cwd: Working directory for the CLI process.
        allowed_tools: Optional list of tool names to allow (e.g. ["Edit", "Write", "Bash"]).
        timeout: Subprocess timeout in seconds.
        output_format: "json" to parse output as JSON, "text" for raw text.
        disable_tools: If True, disable all tools (for text-only agents like clarifier).
        model: Optional model to use (e.g. "sonnet" for faster, cheaper agents).

    Returns:
        Parsed JSON dict (if output_format="json") or raw stdout string.

    Raises:
        RuntimeError: If the CLI exits with non-zero status.
        subprocess.TimeoutExpired: If the process exceeds the timeout.
    """
    cmd = ["claude", "-p", prompt, "--output-format", output_format,
           "--no-session-persistence", "--dangerously-skip-permissions"]
    if disable_tools:
        cmd.extend(["--tools", ""])
    elif allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    if model:
        cmd.extend(["--model", model])

    logger.info(f"Running claude CLI (cwd={cwd}, timeout={timeout}s, tools={allowed_tools})")
    logger.debug(f"Prompt: {prompt[:200]}...")

    # Pass CLAUDE_CODE_OAUTH_TOKEN to subprocess if available
    env = os.environ.copy()
    oauth_token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

    # Force non-interactive mode — prevents Claude Code's tool framework
    # from hanging when there's no TTY (e.g. running under Supervisor)
    env["CI"] = "true"
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"

    result = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=env,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()[:500]
        logger.error(f"Claude CLI failed (exit {result.returncode}): {stderr}")
        raise RuntimeError(f"Claude Code CLI failed (exit {result.returncode}): {stderr}")

    stdout = result.stdout.strip()
    if not stdout:
        logger.warning("Claude CLI returned empty output")
        return {} if output_format == "json" else ""

    if output_format == "json":
        try:
            parsed = json.loads(stdout)
            # Claude --output-format json wraps in {"type":"result","result":"..."}
            # Extract the inner result text if present
            if isinstance(parsed, dict) and "result" in parsed:
                inner = parsed["result"]
                # Try to parse the inner result as JSON too
                if isinstance(inner, str):
                    try:
                        return json.loads(inner)
                    except (json.JSONDecodeError, ValueError):
                        return {"text": inner}
                return inner
            return parsed
        except json.JSONDecodeError:
            logger.warning("Could not parse CLI output as JSON, returning as text")
            return {"text": stdout}

    return stdout


def run_claude_with_fallback(prompt, cwd, allowed_tools=None, timeout=300,
                              fallback_prompt=None, fallback_timeout=None,
                              degraded_result=None, model=None):
    """Run Claude Code CLI with a 3-tier retry strategy.

    Attempt 1: Original prompt with original timeout and tools.
    Attempt 2: Simpler fallback prompt, no tools, shorter timeout.
    Attempt 3: Return a pre-defined degraded result (safe default).

    Args:
        prompt: Primary prompt to send to Claude.
        cwd: Working directory for the CLI process.
        allowed_tools: Optional list of tool names for the primary attempt.
        timeout: Subprocess timeout in seconds for the primary attempt.
        fallback_prompt: Simpler prompt for attempt 2 (no tools).
        fallback_timeout: Timeout for fallback attempt (default: max(timeout//3, 60)).
        degraded_result: Safe default dict/string to return if all retries fail.
        model: Optional model override (e.g. "sonnet").

    Returns:
        Parsed result from whichever attempt succeeds.

    Raises:
        RuntimeError: If all retry attempts are exhausted and no degraded_result provided.
    """
    # Attempt 1: full prompt with tools
    try:
        return run_claude(prompt, cwd, allowed_tools=allowed_tools,
                         timeout=timeout, model=model)
    except (subprocess.TimeoutExpired, RuntimeError) as e:
        logger.warning(f"Attempt 1 failed ({e}), trying fallback...")

    # Attempt 2: simpler prompt, no tools
    if fallback_prompt:
        retry_timeout = fallback_timeout or max(timeout // 3, 60)
        try:
            return run_claude(fallback_prompt, cwd, timeout=retry_timeout,
                            disable_tools=True, model=model)
        except Exception as e:
            logger.warning(f"Fallback also failed: {e}")

    # Attempt 3: degraded result
    if degraded_result is not None:
        logger.warning("Using degraded-mode result")
        return degraded_result

    raise RuntimeError("All retry attempts exhausted")


def run_claude_parallel(prompts_dict, cwd, allowed_tools=None, timeout=300):
    """Run multiple Claude Code CLI invocations in parallel.

    Args:
        prompts_dict: Dict of {name: prompt_string} for each parallel invocation.
        cwd: Working directory for all CLI processes.
        allowed_tools: Optional list of tool names to allow.
        timeout: Per-invocation timeout in seconds.

    Returns:
        Dict of {name: result} for each invocation.
    """
    results = {}
    errors = {}

    with ThreadPoolExecutor(max_workers=len(prompts_dict)) as executor:
        futures = {
            executor.submit(
                run_claude, prompt, cwd,
                allowed_tools=allowed_tools,
                timeout=timeout,
            ): name
            for name, prompt in prompts_dict.items()
        }

        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as e:
                logger.error(f"Parallel claude invocation '{name}' failed: {e}")
                errors[name] = str(e)
                results[name] = {"error": str(e)}

    if errors:
        logger.warning(f"Parallel run had {len(errors)} error(s): {list(errors.keys())}")

    return results
