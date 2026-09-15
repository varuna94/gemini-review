# TODOS

미룬 항목과 다음 판 계획. 계획 문서는 커밋하지 않고, 남는 일만 여기에 둔다.
판마다 달라진 점은 `CHANGELOG.md` 에 있다.

## 다음 큰 판 — 2.0 (1.4.x 호환을 걷어낸다)

- 결과 JSON 최상위 `mode` · `model` 제거(`_meta` 만 남긴다).
- 옛 플래그 `--fallback-model` · `--no-fallback` 제거.

## 미룬 항목

### 커밋 게이트 hook
- **What:** `git commit` 직전에 스테이징 diff 와 일치하는 최신 리뷰 결과가 없거나 통과가
  아니면 커밋을 막는 Claude Code hook.
- **Why:** 지금 게이트는 모델이 지시를 기억해 `/gemini-review` 를 부르는 데 기댄다.
- **Cons:** 사용자 hook 설정이 필요하고, 리뷰를 기다리는 동안 커밋이 막힌다.
- **Depends on:** 1.4.0 결과 계약(`_meta.passed` · `exit_code` · `scope`)은 됐다. 남은 것은
  결과 JSON 의 diff 해시(`diff_sha256`, 미룬 결정 5) — **리뷰에 쓴 같은 바이트**로 한 번만 계산한다.

### 실제 Windows 에서 확인 (cp949 콘솔 · PowerShell 실행 블록)
- **What:** Windows 기본 콘솔에서 `python tests/test_gemini_review.py` 와 실제 리뷰 한 번.
  SKILL.md 의 PowerShell 실행 블록을 실제로 돌려 인터프리터 탐지 · 못 찾으면 exit 2 ·
  `exit $LASTEXITCODE` 전달을 본다(지금 테스트는 그 블록의 모양만 본다).
- **Why:** cp949 는 `PYTHONIOENCODING` 흉내로만 확인했다. v1.3.1 에서 테스트 자신이 cp949
  에서 죽던 이력이 있고, 1.4.0 작업 중에도 help 문구의 em dash 가 cp949 에서 `--help` 를
  죽였다. CI 의 Windows 러너는 한국어 로캘이 아니라 대신하지 못한다.

### agy 하위 프로세스 그룹 종료
- **What:** 하드 상한 · 신호 종료 때 agy 의 손자 프로세스까지 정리(POSIX 는 새 세션 +
  그룹 종료, Windows 는 `CREATE_NEW_PROCESS_GROUP`).
- **Why:** `subprocess.run` 은 직접 자식만 죽인다.
- **Cons:** 플랫폼 분기. 현재 agy 는 한 줄 프롬프트에서 하위 프로세스를 띄우지 않는다(실측).
  `--check` 는 아직 하위 프로세스 수를 재지 않는다 — 재게 되면 보이는 순간 우선순위를 올린다.

### 백그라운드 실행이 호스트에 끊길 때
- **What:** Claude Code 의 백그라운드 Bash 작업이 가용 메모리가 충분한데도 "system is running
  low on memory" 로 리뷰를 끊은 일이 있다(26.09.14, 가용 13GB · agy 230MB). 그때 오는 신호가
  SIGTERM 인지 SIGKILL 인지 재고, SIGKILL 이면 남는 임시 폴더를 다음 실행이 치우는 방안을 본다.
- **Why:** SKILL.md 절차가 백그라운드 실행을 권한다. SIGKILL 은 신호 정리로도 못 잡는다
  (`_meta.mode` 가 `in_progress` 로 남아 통과로 읽히지 않는 것이 유일한 방어).

### 기타
- 같은 `--out` 경로로 동시에 돌릴 때의 경고를 문서화.
- 결과 파일 집계 명령(`--stats`) — `_meta` 가 생겼으니 쉬워졌다.
- README 에 실제 출력 예시.
- 새 판 알림.
- 생존 확인에서 주 모델 · 진단 모델을 동시에 찌르기(최악 대기 180초 절약).
- agy 경로를 환경 변수로 지정 — 저장소 설정으로 심을 수 있는 위험을 풀 방법이 생기면.
- `--staged` 와 `--base` · `--two-dot` 을 함께 주면 거부.
- LICENSE 추가(소유자 결정).
