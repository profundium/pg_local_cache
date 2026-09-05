#!/usr/bin/env python3
"""Execute the source-install documentation against a fresh, isolated database."""

import json
from pathlib import Path
import re
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def blocks(document: str, heading: str, language: str) -> list[str]:
    section = document.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]
    result = re.findall(rf"```{language}\n(.*?)\n```", section, re.DOTALL)
    if not result:
        raise ValueError(f"No {language} block under {heading}")
    return result


def docker(*args: str, input: str | None = None) -> str:
    result = subprocess.run(
        ["docker", *args], input=input, text=True, capture_output=True, timeout=60
    )
    if result.returncode:
        raise RuntimeError(f"docker {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def main() -> None:
    document = (ROOT / "docs/INSTALL_EXISTING.md").read_text()
    config = blocks(document, "Configure before restart", "conf")[0]
    options = []
    for line in config.splitlines():
        key, value = line.split("=", 1)
        value = value.strip().strip("'")
        options.extend(("-c", f"{key.strip()}={value}"))
    image = docker("compose", "-f", str(ROOT / "examples/compose.yaml"),
                   "images", "-q", "postgres")
    if not image or "\n" in image:
        raise ValueError("Build the examples/compose.yaml PostgreSQL image first")
    name = f"pglc-install-docs-{uuid.uuid4().hex[:12]}"

    def sql(statement: str, role: str = "postgres") -> str:
        return docker("exec", "-i", name, "psql", "-X", "-Atq",
                      "-v", "ON_ERROR_STOP=1", "-U", role, "-d", "app",
                      input=statement)

    try:
        # The image contains the compiled extension, but no demo SQL is mounted.
        docker("run", "--detach", "--name", name, "--network", "none",
               "--tmpfs", "/var/lib/postgresql/data",
               "-e", "POSTGRES_DB=app", "-e", "POSTGRES_PASSWORD=disposable-test-only",
               image, "postgres", *options)
        for _ in range(60):
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1",
                 "-U", "postgres", "-d", "app"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("The documented configuration did not start PostgreSQL")

        sql("\n".join(blocks(document, "Initialize a source installation", "sql")))
        # Fixtures represent the existing application table and role in the guide.
        sql("CREATE TABLE public.items (id bigint PRIMARY KEY, value text);\n"
            "INSERT INTO public.items VALUES (1, 'one');\n"
            "CREATE ROLE app_user LOGIN;")
        sql("\n".join(blocks(document, "Attach a table", "sql")))
        sql("\n".join(blocks(document, "Verify cold fill and warm hit", "sql")))
        before = json.loads(sql("SELECT local_cache.stats();"))
        for _ in range(2):
            rows = json.loads(sql(
                "SELECT to_json(local_cache.mget('public.items'::regclass, "
                "ARRAY[1, NULL, 999]::bigint[]));", "app_user"))
            assert json.loads(rows[0]) == {"id": 1, "value": "one"}, rows
            assert rows[1:] == [None, None], rows
        after = json.loads(sql("SELECT local_cache.stats();"))
        assert after["sql_cache_hits"] > before["sql_cache_hits"], after
        print("Source-install documentation passed: startup, role setup, attachment and non-superuser cache hits")
    finally:
        subprocess.run(["docker", "logs", name], check=False, timeout=15)
        subprocess.run(["docker", "rm", "--force", name], check=False, timeout=15)


if __name__ == "__main__":
    main()
