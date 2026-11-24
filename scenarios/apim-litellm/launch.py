"""Validate durable credentials before LiteLLM's native database migration/startup."""

import os
import sys
from pathlib import Path


def main():
    scenario = os.environ.get("AUTHENTICATION_SCENARIO", "shared-service")
    if scenario not in {"shared-service", "dedicated-application", "user-obo"}:
        raise RuntimeError("AUTHENTICATION_SCENARIO is invalid")
    for name in ("DATABASE_URL", "LITELLM_MASTER_KEY"):
        if not os.environ.get(name, "").strip():
            raise RuntimeError(f"{name} must be supplied by litellm-auth")
    if not os.environ["DATABASE_URL"].startswith(("postgresql://", "postgres://")):
        raise RuntimeError("DATABASE_URL must reference durable PostgreSQL")
    if not os.environ["LITELLM_MASTER_KEY"].startswith("sk-"):
        raise RuntimeError("LITELLM_MASTER_KEY must start with sk-")
    if scenario == "user-obo":
        if not os.environ.get("OBO_CLIENT_SECRET", "").strip():
            path = os.environ.get("OBO_CLIENT_SECRET_FILE", "/mnt/secrets-store/obo-client-secret")
            try:
                secret = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                raise RuntimeError("OBO client secret file is unavailable") from None
            if not secret:
                raise RuntimeError("OBO client secret file is empty")
            os.environ["OBO_CLIENT_SECRET"] = secret
    os.execvp("litellm", ["litellm", *sys.argv[1:]])


if __name__ == "__main__":
    main()
