from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def _load(name: str) -> str:
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


CALLER_SYSTEM_PREAMBLE = _load("caller_system_preamble")
DECOMPOSITION_SYSTEM_PROMPT = _load("decomposition_system")
DECOMPOSITION_USER_PROMPT = _load("decomposition_user")
EXECUTE_ATOMIC_SYSTEM_PROMPT = _load("execute_atomic_system")
EXECUTE_ATOMIC_USER_PROMPT = _load("execute_atomic_user")
SYNTHESIS_SYSTEM_PROMPT = _load("synthesis_system")
SYNTHESIS_USER_PROMPT = _load("synthesis_user")
