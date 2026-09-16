#!/usr/bin/env bash
# gemini-review 커밋 게이트: Claude Code PreToolUse(Bash) 진입점 (1.6.0).
#
# 판단은 commit_gate.py 가 한다. 이 파일은 두 가지만 맡는다.
#   1. 빠른 경로 — Bash 도구 호출마다 돌므로, `commit` 도 `gemini-review` 도 없는 명령은
#      파이썬을 띄우지 않고 여기서 허용한다.
#   2. 파이썬 3 찾기 — Windows 의 `python3` 는 Microsoft Store 안내 스텁일 수 있어, 후보마다
#      실제로 실행해 확인한다(SKILL.md 실행 블록과 같은 이유).
#
# ⛔ hook 규약에서 막는 코드는 2 뿐이다. 그 밖의 비정상 종료는 Claude Code 가 **통과시킨다.**
#   그래서 파이썬을 못 찾았거나 게이트가 0 · 2 가 아닌 코드로 죽으면, 게이트가 켜진
#   저장소에서는 여기서 2 로 바꾼다(fail-closed). 꺼진 곳에서는 아무것도 막지 않는다.

input=$(cat)
# ⚠ 입력 JSON 에는 명령 말고도 `transcript_path` · `cwd` 가 있고, 거기에 프로젝트 경로가 들어간다.
#   `*gemini-review*` 처럼 넓게 거르면 이름이 그런 저장소에서는 Bash 호출마다 파이썬이 뜬다.
#   게이트 설정 키(`gemini-review.gate`)와 구역 조작(`--remove-section gemini-review`)만 본다.
# ⚠ git 설정 키는 대소문자를 가리지 않는다 — 거르기도 가리지 않는다.
shopt -s nocasematch
case "$input" in
  *commit*|*gemini-review.gate*|*-section*gemini-review*) ;;
  *) exit 0 ;;
esac
shopt -u nocasematch

export PYTHONUTF8=1
gate="$(dirname "${BASH_SOURCE[0]}")/commit_gate.py"
if command -v cygpath >/dev/null 2>&1; then
  gate=$(cygpath -w "$gate")          # Git Bash 의 /c/… 경로를 Windows 파이썬은 못 연다
fi

why="파이썬 3.7 이상을 찾지 못했다"
for cand in python3 python py; do
  command -v "$cand" >/dev/null 2>&1 || continue
  if [ "$cand" = py ]; then set -- py -3; else set -- "$cand"; fi
  "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' >/dev/null 2>&1 || continue
  printf '%s' "$input" | "$@" "$gate"
  rc=$?
  if [ "$rc" -eq 0 ] || [ "$rc" -eq 2 ]; then
    exit "$rc"
  fi
  why="게이트 스크립트가 종료 코드 $rc 로 끝났다"
  break
done

# 여기 왔다면 게이트가 판단하지 못했다. 명령을 해석하지 못했으니 대상 저장소도 모른다 —
#   현재 폴더와 전역 설정 가운데 **엄한 쪽**으로 정한다(commit_gate.unknown_target_state 와 같다).
command -v git >/dev/null 2>&1 || exit 0
value=$(git config --bool --get gemini-review.gate 2>/dev/null)
state=$?
global=$(git config --global --bool --get gemini-review.gate 2>/dev/null)
if [ "$global" != "true" ] && { [ "$state" -eq 1 ] || { [ "$state" -eq 0 ] && [ "$value" != "true" ]; }; }; then
  exit 0
fi
printf '%s\n' "⛔ 커밋 게이트(gemini-review)가 이 명령을 막았다." \
  "   원인: ${why} — 게이트가 켜진 저장소라 확인하지 못한 명령을 통과시키지 않는다." \
  "   해결: 사용자에게 이 문구를 그대로 보고한다(파이썬 3 설치 · 게이트 결함 확인)." >&2
exit 2
