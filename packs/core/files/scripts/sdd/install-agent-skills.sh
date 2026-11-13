#!/usr/bin/env bash
# Espelha .agents/skills (fonte canônica) num repo de implementação aninhado e em .claude/skills.
# Uso: scripts/sdd/install-agent-skills.sh <repo-alvo|.> [--check]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE_DIR="${ROOT_DIR}/.agents/skills"
TARGET_REPO="${1:-}"
MODE="${2:-sync}"

if [[ -z "${TARGET_REPO}" ]]; then
  printf 'Uso: %s <repo-alvo> [--check]\n' "$0" >&2
  printf '  %s repo/<servico>          # copia .agents/skills e gera .claude/skills\n' "$0" >&2
  printf '  %s .                        # raiz: só gera o espelho .claude/skills\n' "$0" >&2
  printf '  %s repo/<servico> --check  # só compara; sai 1 se houver drift\n' "$0" >&2
  exit 2
fi
if [[ "${MODE}" != "sync" && "${MODE}" != "--check" ]]; then
  printf 'Modo desconhecido: %s (use --check ou omita)\n' "${MODE}" >&2
  exit 2
fi
[[ "${TARGET_REPO}" != /* ]] && TARGET_REPO="${ROOT_DIR}/${TARGET_REPO}"
[[ -d "${SOURCE_DIR}" ]] || { printf 'Origem não encontrada: %s\n' "${SOURCE_DIR}" >&2; exit 1; }
[[ -d "${TARGET_REPO}" ]] || { printf 'Repo alvo não encontrado: %s\n' "${TARGET_REPO}" >&2; exit 1; }
TARGET_REPO="$(realpath "${TARGET_REPO}")"

IS_ROOT=0
[[ "${TARGET_REPO}" == "${ROOT_DIR}" ]] && IS_ROOT=1
AGENTS_TARGET="${TARGET_REPO}/.agents/skills"
CLAUDE_TARGET="${TARGET_REPO}/.claude/skills"

mapfile -t SKILLS < <(find "${SOURCE_DIR}" -mindepth 2 -maxdepth 2 -name SKILL.md -printf '%h\n' | xargs -r -n1 basename | sort)

if [[ "${MODE}" == "--check" ]]; then
  if [[ "${IS_ROOT}" -eq 1 ]]; then
    printf 'Raiz do workspace — não há cópia própria de .agents/skills/ para comparar.\n'
    exit 0
  fi
  DRIFTED=()
  for s in "${SKILLS[@]}"; do
    if [[ ! -d "${AGENTS_TARGET}/${s}" ]]; then
      DRIFTED+=("${s} (ausente)")
    elif ! diff -rq "${SOURCE_DIR}/${s}" "${AGENTS_TARGET}/${s}" >/dev/null 2>&1; then
      DRIFTED+=("${s}")
    fi
  done
  if [[ "${#DRIFTED[@]}" -eq 0 ]]; then
    printf 'Harness em dia em %s.\n' "${AGENTS_TARGET}"
    exit 0
  fi
  printf 'Harness desatualizado em %s:\n' "${AGENTS_TARGET}" >&2
  printf '  - %s\n' "${DRIFTED[@]}" >&2
  exit 1
fi

mkdir -p "${CLAUDE_TARGET}"
[[ "${IS_ROOT}" -eq 0 ]] && mkdir -p "${AGENTS_TARGET}"
for s in "${SKILLS[@]}"; do
  if [[ "${IS_ROOT}" -eq 0 ]]; then
    rm -rf "${AGENTS_TARGET:?}/${s}"
    cp -a "${SOURCE_DIR}/${s}" "${AGENTS_TARGET}/${s}"
  fi
  rm -rf "${CLAUDE_TARGET:?}/${s}"
  cp -a "${SOURCE_DIR}/${s}" "${CLAUDE_TARGET}/${s}"
done

if [[ "${IS_ROOT}" -eq 1 ]]; then
  printf 'Espelho Claude Code gerado em: %s\n' "${CLAUDE_TARGET}"
else
  printf 'Skills sincronizadas em: %s e %s\n' "${AGENTS_TARGET}" "${CLAUDE_TARGET}"
fi
