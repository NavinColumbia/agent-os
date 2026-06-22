"""demo_eval.py — minimal Inspect AI eval proving the harness runs locally (ADR 0004 K6).

Uses a deterministic custom solver (no API key / no external model needed) so the eval harness
runs fully offline. Real evals swap in a local Ollama model or Claude; the harness is the point.

    inspect eval scripts/demo_eval.py --model mockllm/model
"""
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import solver


@solver
def fixed_answer():
    async def solve(state, generate):
        # stand-in for an agent's response; deterministic so the score is reproducible
        state.output.completion = "the capital of france is paris"
        return state
    return solve


@task
def demo():
    return Task(
        dataset=[Sample(input="What is the capital of France?", target="paris")],
        solver=fixed_answer(),
        scorer=includes(),
    )
