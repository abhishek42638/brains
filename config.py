"""Environment loading — the one place it happens.

Importing this module loads `.env` into os.environ, once, for whichever entry
point pulled it in: the API, the CLI, ingest, or the MCP server. Every entry
point imports it first thing; module caching makes the load idempotent, so the
import order between them does not matter.

Two properties matter more than the convenience:

REAL ENV VARS ALWAYS WIN. `load_dotenv(override=False)` will not overwrite a
variable that is already set. In Cloud Run there is no .env file and the config
arrives as real environment variables (or mounted secrets); a .env that
overrode them would mean a stray file in an image silently replacing production
config. Local .env fills gaps; it never argues.

A MISSING .env IS NORMAL, NOT AN ERROR. `load_dotenv` no-ops when the file
isn't there, which is exactly the deployed case. Nothing here raises, so the
same image runs locally and in Cloud Run unchanged.

`find_dotenv(usecwd=True)` is deliberately NOT used: it walks up from the
current working directory, so where you happened to run the command from would
decide which .env you got. The path is anchored to this file instead — the repo
root — so `python -m agent.cli` from a subdirectory behaves the same as from
the root.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Anchored to the repo root (this file's directory), not the CWD.
ENV_PATH = Path(__file__).resolve().parent / ".env"

#: True when a local .env was found and read. Deployments expect False.
LOADED_DOTENV = load_dotenv(ENV_PATH, override=False)


def require(name: str) -> str:
    """Fetch a required setting, or fail with a message that says what to do.

    Callers used to hand-roll `if not os.environ.get(...)` checks with slightly
    different wording each time; a missing key should read the same however you
    reached it.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Put it in {ENV_PATH} for local runs, or set it "
            f"as a real environment variable when deployed."
        )
    return value
