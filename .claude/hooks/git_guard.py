"""PreToolUse hook — bloquea comandos Git sensibles y borrados destructivos.

No depende de `jq` (no está instalado en esta máquina) — parsea el JSON de
stdin con la stdlib. Ver docs/decisions.md (secc. Git) y
.claude/skills/orchestrator/policies/git.md para el diseño completo.

Bloquea: git commit / push / reset / clean (cualquier variante, incluida
--force/-f), y borrados recursivos (`rm -rf`/`rm -fr`,
`Remove-Item ... -Recurse ... -Force`). No bloquea git status/diff/log/
branch/add/stash — son reversibles o de solo lectura.
"""

import json
import re
import sys

BLOCKED_GIT = re.compile(r"\bgit\s+(commit|push|reset|clean)\b", re.IGNORECASE)
BLOCKED_RM = re.compile(r"\brm\s+(-\w*[rf]\w*\s+-\w*[rf]\w*|-\w*[rf]{2}\w*)\b", re.IGNORECASE)
BLOCKED_REMOVE_ITEM = re.compile(
    r"\bremove-item\b(?=.*-recurse)(?=.*-force)", re.IGNORECASE | re.DOTALL
)


def es_bloqueado(command: str) -> str | None:
    if BLOCKED_GIT.search(command):
        return "git commit/push/reset/clean requieren autorización explícita del Arquitecto en este turno."
    if BLOCKED_RM.search(command):
        return "rm recursivo/forzado requiere autorización explícita del Arquitecto en este turno."
    if BLOCKED_REMOVE_ITEM.search(command):
        return "Remove-Item -Recurse -Force requiere autorización explícita del Arquitecto en este turno."
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # entrada no parseable: no bloquea, deja el flujo normal

    if payload.get("tool_name") != "Bash":
        return 0

    command = payload.get("tool_input", {}).get("command", "")
    if not isinstance(command, str) or not command:
        return 0

    razon = es_bloqueado(command)
    if razon:
        sys.stderr.write(f"[git_guard] Bloqueado: {razon}\n")
        sys.stderr.write(f"[git_guard] Comando: {command}\n")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
