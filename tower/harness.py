"""Code-execution harness: wraps the Ollama model with smolagents CodeAgent.

The model gets a Docker-sandboxed Python executor confined to a dedicated
directory (SANDBOX_DIR): no network, capped RAM/CPU/PIDs, read-only
filesystem everywhere except the bind-mounted workspace. This module
degrades gracefully — if smolagents isn't installed, Docker is unavailable,
or a run fails/times out, ask() raises HarnessUnavailable and the server
falls back to the plain single-shot LLM call.

Note: smolagents' executor kwargs API has shifted across releases; the
requirements pin smolagents==1.26.0. If you bump the version, re-verify the
CodeAgent/DockerExecutor signatures (see SETUP.md §6.1) — a mismatch shows
up as a "harness_fallback" line in the activity log.
"""

import concurrent.futures
import logging
import os
import threading

log = logging.getLogger("harness")

try:
    from smolagents import CodeAgent, OpenAIModel
    _SMOLAGENTS = True
except ImportError:
    _SMOLAGENTS = False


class HarnessUnavailable(Exception):
    """The harness can't answer right now (caller should fall back)."""


# Security defaults for the sandbox container (deliberate, not tuning knobs):
# no network, 512 MB RAM, 1 CPU, 128 PIDs.
_CONTAINER_RUN_KWARGS = {
    "mem_limit": "512m",
    "nano_cpus": 10 ** 9,
    "network_disabled": True,
    "pids_limit": 128,
}


class CodeHarness:
    """One long-lived CodeAgent (built lazily), answering serially.

    The pipeline's llm worker is single-threaded, so one agent/container is
    safe; concurrent run() calls on one instance are not supported.
    """

    def __init__(self, model_id, api_base, api_key="ollama",
                 sandbox_dir="~/llm_server/sandbox", max_steps=6,
                 timeout_seconds=300, system_prompt=""):
        self.model_id = model_id
        self.api_base = api_base
        self.api_key = api_key
        self.sandbox_dir = os.path.abspath(os.path.expanduser(sandbox_dir))
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.system_prompt = system_prompt
        self._lock = threading.Lock()
        self._agent = None
        self._broken = False
        self._consecutive_failures = 0
        self._disabled = False

    def _build(self):
        if not _SMOLAGENTS:
            raise HarnessUnavailable(
                "smolagents not installed "
                "(pip install 'smolagents[docker,openai]==1.26.0')")
        try:
            import docker
            docker.from_env().ping()
        except Exception as e:
            raise HarnessUnavailable("Docker unavailable: %s" % e)
        os.makedirs(self.sandbox_dir, exist_ok=True)
        model = OpenAIModel(
            model_id=self.model_id,
            api_base=self.api_base,
            api_key=self.api_key,
        )
        try:
            agent = CodeAgent(
                tools=[],
                model=model,
                max_steps=self.max_steps,
                additional_instructions=self.system_prompt,
                executor_type="docker",
                executor_kwargs={
                    "container_run_kwargs": {
                        "volumes": {
                            self.sandbox_dir: {"bind": "/workspace", "mode": "rw"},
                        },
                        "working_dir": "/workspace",
                        **_CONTAINER_RUN_KWARGS,
                    },
                },
            )
        except Exception as e:
            raise HarnessUnavailable("CodeAgent build failed: %s" % e)
        log.info("harness ready (model %s, sandbox %s)", self.model_id, self.sandbox_dir)
        return agent

    def _get_agent(self):
        if self._disabled:
            raise HarnessUnavailable(
                "harness disabled after repeated failures (see logs)")
        with self._lock:
            if self._agent is None or self._broken:
                self._agent = self._build()
                self._broken = False
            return self._agent

    def _note_failure(self, agent):
        with self._lock:
            self._broken = True
            self._consecutive_failures += 1
            if self._consecutive_failures >= 5:
                self._disabled = True
        try:
            agent.executor.shutdown()  # kill the container; unblocks a stuck run
        except Exception:
            pass

    def ask(self, speaker, text, history=()):
        """Run one user message through the agent; return the final text.

        history: the shared channel's recent turns as (role, content),
        prepended to the task so the agent sees the conversation.
        Raises HarnessUnavailable on timeout/failure so the caller can fall
        back to the plain LLM.
        """
        agent = self._get_agent()
        parts = []
        if history:
            parts.append("Earlier conversation in this channel:")
            parts += ["Assistant: %s" % content if role == "assistant" else content
                      for role, content in history]
        parts.append("The user '%s' said: %s" % (speaker, text))
        task = "\n".join(parts)
        # A throwaway executor per call on purpose: we must NOT call
        # ex.shutdown(wait=True) after a timeout — that would block until the
        # (now container-killed) run dies on its own.
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(agent.run, task, reset=True)
        try:
            result = fut.result(timeout=self.timeout_seconds)
        except concurrent.futures.TimeoutError:
            self._note_failure(agent)
            raise HarnessUnavailable(
                "harness timed out after %ds (container killed)"
                % self.timeout_seconds)
        except Exception as e:
            self._note_failure(agent)
            raise HarnessUnavailable("harness run failed: %s" % e)
        with self._lock:
            self._consecutive_failures = 0
        return str(result)
