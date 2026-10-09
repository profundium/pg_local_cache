#!/usr/bin/env python3
"""Check definitions shared by the fresh install and upgrade SQL scripts."""

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
UPGRADE = ROOT / "sql/pg_local_cache--3.1.0--3.2.0.sql"
INSTALL = ROOT / "sql/pg_local_cache--3.2.0.sql"
FUNCTION_START = re.compile(
    r"(?im)^[ \t]*CREATE[ \t]+(?:OR[ \t]+REPLACE[ \t]+)?FUNCTION[ \t]+"
    r"(?P<name>(?:[A-Za-z_][A-Za-z_0-9$]*|\"(?:\"\"|[^\"])+\")"
    r"(?:[ \t]*\.[ \t]*(?:[A-Za-z_][A-Za-z_0-9$]*|\"(?:\"\"|[^\"])+\"))?)"
)
DOLLAR_QUOTE = re.compile(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$")
RETURNS_LINE = re.compile(r"(?im)^[ \t]*RETURNS\b")


def function_end(sql: str, start: int) -> int:
    """Return end offset for a function using a dollar or quoted AS body."""
    header_end = sql.find(";", start)
    if header_end < 0:
        raise ValueError("unterminated CREATE FUNCTION")
    header = sql[start:header_end]
    body = re.search(r"\bAS\s+(\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$)", header, re.I)
    if body:
        delimiter = body.group(1)
        close = sql.find(delimiter + ";", start + body.end(1))
        if close < 0:
            raise ValueError("unterminated dollar-quoted function body")
        return close + len(delimiter) + 1

    # C-language functions use quoted library/symbol names and end at the
    # first semicolon; neither quoted value contains SQL statement separators.
    return header_end + 1


def normalize_signature(signature: str) -> str:
    """Ignore layout differences while preserving defaults and quoted text."""
    output: list[str] = []
    quote = None
    pending_space = False
    index = 0
    while index < len(signature):
        char = signature[index]
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(signature) and signature[index + 1] == quote:
                    output.append(signature[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            if pending_space and output and output[-1] not in "(),.":
                output.append(" ")
            pending_space = False
            output.append(char)
            quote = char
        elif char.isspace():
            pending_space = True
        else:
            if (
                pending_space
                and output
                and output[-1] not in "(),."
                and char not in "(),."
            ):
                output.append(" ")
            pending_space = False
            output.append(char)
        index += 1
    return "".join(output)


def definitions(path: Path) -> dict[str, list[tuple[str, int]]]:
    sql = path.read_text()
    result: dict[str, list[tuple[str, int]]] = {}
    for match in FUNCTION_START.finditer(sql):
        start = match.start()
        end = function_end(sql, match.end())
        statement_tail = sql[match.end() : end]
        returns = RETURNS_LINE.search(statement_tail)
        if not returns:
            raise ValueError(f"function missing RETURNS in {path}:{sql.count(chr(10), 0, start) + 1}")

        raw_name = match.group("name").rsplit(".", 1)[-1].strip()
        name = raw_name[1:-1].replace('""', '"') if raw_name.startswith('"') else raw_name.lower()
        signature = normalize_signature(statement_tail[: returns.start()])
        definition = signature + statement_tail[returns.start() :]
        line = sql.count("\n", 0, start) + 1
        result.setdefault(name, []).append((definition, line))
    return result


def main() -> int:
    upgrade = definitions(UPGRADE)
    install = definitions(INSTALL)
    mismatches = []
    for name in sorted(upgrade.keys() & install.keys()):
        upgrade_defs = sorted(definition for definition, _ in upgrade[name])
        install_defs = sorted(definition for definition, _ in install[name])
        if upgrade_defs != install_defs:
            upgrade_lines = ", ".join(str(line) for _, line in upgrade[name])
            install_lines = ", ".join(str(line) for _, line in install[name])
            mismatches.append(
                f"  {name}: upgrade line(s) {upgrade_lines}; install line(s) {install_lines}"
            )

    if mismatches:
        print("Shared function definitions differ:\n" + "\n".join(mismatches), file=sys.stderr)
        return 1
    print(f"Function definitions match ({len(upgrade.keys() & install.keys())} shared).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
