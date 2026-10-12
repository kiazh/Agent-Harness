"""One command representation for broker identity and subprocess execution."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass

from machina.core.exceptions import ValidationError


@dataclass(frozen=True)
class TerminalCommand:
    argv: list[str]
    payload: str = ""
    shell: str = ""


def _scan(command: str) -> tuple[bool, list[str] | None]:
    """Recognize only literal stages connected by ;, newline, &&, || or |.

    Everything involving expansion, redirection, background jobs or compound
    grammar is opaque. Quotes/escapes are respected; quoted operators are data.
    This classifies for review, not as a sandbox or full shell parser.
    """
    quote = ""
    stages = []
    start = index = 0
    shell = opaque = False
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            if index + 1 < len(command) and command[index + 1] == "\n":
                shell = opaque = True
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
            elif quote == '"' and char in "$`":
                shell = opaque = True
            index += 1
            continue
        if char in "'\"":
            quote = char
        elif char in ";\n&|":
            shell = True
            op = command[index : index + 2]
            length = 2 if op in {"&&", "||"} else 1
            if char == "&" and length == 1:
                opaque = True
            stage = command[start:index].strip()
            if stage:
                stages.append(stage)
            elif char != "\n":
                opaque = True
            index += length
            start = index
            continue
        elif char in "$`()<>#*?[]{}~":
            shell = opaque = True
        index += 1
    tail = command[start:].strip()
    if tail:
        stages.append(tail)
    elif command.rstrip().endswith(("&&", "||", "|")):
        opaque = True
    if quote or len(stages) > 32:
        opaque = True
    return shell, None if opaque else stages


def parse_terminal_command(command: str) -> TerminalCommand:
    if not isinstance(command, str) or not command.strip():
        raise ValidationError("Empty command")
    if "\0" in command:
        raise ValidationError("Command contains a NUL byte")
    shell, _ = _scan(command)
    # Leading environment assignments and builtins need shell semantics too.
    if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*=", command):
        shell = True
    if shell:
        if os.name == "nt":
            return TerminalCommand(["cmd.exe", "/d", "/s", "/c", command], command, "cmd")
        return TerminalCommand(["/bin/sh", "-c", command], command, "sh")
    try:
        argv = shlex.split(command)
    except ValueError:
        raise ValidationError("Failed to parse command: unmatched quoting") from None
    return TerminalCommand(argv)


def simple_shell_commands(command: str) -> list[list[str]] | None:
    """Literal POSIX stages only; unsafe/unsupported grammar requires review."""
    if os.name == "nt":
        return None
    _, stages = _scan(command)
    if not stages:
        return None
    try:
        parsed = [shlex.split(stage) for stage in stages]
    except ValueError:
        return None
    return parsed if all(parsed) else None
