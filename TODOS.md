# TODOS

미룬 항목과 다음 판 계획. 계획 문서는 커밋하지 않고, 남는 일만 여기에 둔다.

## 다음 판 — 1.4.0 (결과 계약이 바뀐다)

1.3.2 는 기존 종료 코드의 의미와 `--out` 키를 바꾸지 않는 결함 수정이다(중단 130 · 143 과
최종 mode 값은 더했다). 아래는 계약이 바뀌어 판을 따로 낸다.

- **판정은 주 모델만.** 폴백 모델(flash)은 진단용으로만 쓰고, 폴백이 낸 판정이 exit 0 이
  되는 전이를 없앤다. 테스트로 그 전이가 불가능함을 고정한다.
- **agy 호출을 한 곳으로(`_run_agy`).** 세 곳이 `subprocess.run` · 하드 상한 · `--mode plan`
  을 각자 적는다. 하나만 빠져도 모른다.
- **원인 분리.** 시간 초과 · 도구 권한 거부 · 쿼터 소진 · 진짜 빈 응답을 `--out` 과 종료
  코드에 다르게 기록한다(화면 표시는 1.3.2 에서 했다). 시간 초과면 같은 모델 재시도를 하지
  않는다.
- **결과 계약(`_meta`).** `mode` · 종료 코드 · 해석된 SHA 를 결과 JSON 에 싣는다.
- **`main()` 분기 매트릭스 테스트.** 빈 응답 · 시간 초과 · 도구 오류 · 폴백 · 텍스트 갈래별
  종료 코드와 `--out` mode.
- **`--check`.** 실제 agy 에 한 줄 프롬프트로 종료 코드 · 래퍼 필드 · 시간 초과 문구를 점검.
- **빈 `--staged` 는 exit 8.** 스테이징을 빠뜨린 커밋이 exit 0 으로 통과로 읽힌다.
  의도한 경우 `--allow-empty` 로 0.
- **CI.** GitHub Actions ubuntu · windows 에서 동작 검사.
- **CHANGELOG 와 태그**(`v1.3.2` · `v1.4.0`), 폐기 경고.
- `--fallback-model` → `--probe-model` 이름 변경(폐기 경고와 함께).

## 미룬 항목

### 커밋 게이트 hook
- **What:** `git commit` 직전에 스테이징 diff 와 일치하는 최신 리뷰 결과가 없거나 통과가
  아니면 커밋을 막는 Claude Code hook.
- **Why:** 지금 게이트는 모델이 지시를 기억해 `/gemini-review` 를 부르는 데 기댄다.
- **Cons:** 사용자 hook 설정이 필요하고, 리뷰를 기다리는 동안 커밋이 막힌다.
- **Depends on:** 1.4.0 결과 계약 · 결과 JSON 의 diff 해시(미룬 결정).

### 실제 Windows 에서 확인 (cp949 콘솔 · PowerShell 실행 블록)
- **What:** Windows 기본 콘솔에서 `python tests/test_gemini_review.py` 와 실제 리뷰 한 번.
  SKILL.md 의 PowerShell 실행 블록을 실제로 돌려 인터프리터 탐지 · 못 찾으면 exit 2 ·
  `exit $LASTEXITCODE` 전달을 본다(지금 테스트는 그 블록의 모양만 본다).
- **Why:** cp949 는 `PYTHONIOENCODING` 흉내로만 확인했다. v1.3.1 에서 테스트 자신이 cp949
  에서 죽던 이력이 있다.

### agy 하위 프로세스 그룹 종료
- **What:** 하드 상한 · 신호 종료 때 agy 의 손자 프로세스까지 정리(POSIX 는 새 세션 +
  그룹 종료, Windows 는 `CREATE_NEW_PROCESS_GROUP`).
- **Why:** `subprocess.run` 은 직접 자식만 죽인다.
- **Cons:** 플랫폼 분기. 현재 agy 는 한 줄 프롬프트에서 하위 프로세스를 띄우지 않는다(실측).
  `--check` 에서 하위 프로세스가 보이면 우선순위를 올린다.

### 백그라운드 실행이 호스트에 끊길 때
- **What:** Claude Code 의 백그라운드 Bash 작업이 가용 메모리가 충분한데도 "system is running
  low on memory" 로 리뷰를 끊은 일이 있다(26.09.14, 가용 13GB · agy 230MB). 그때 오는 신호가
  SIGTERM 인지 SIGKILL 인지 재고, SIGKILL 이면 남는 임시 폴더를 다음 실행이 치우는 방안을 본다.
- **Why:** SKILL.md 절차가 백그라운드 실행을 권한다. SIGKILL 은 1.3.2 의 신호 정리로도 못 잡는다.

### 기타
- 같은 `--out` 경로로 동시에 돌릴 때의 경고를 문서화.
- 결과 파일 집계 명령(`--stats`) — 결과 계약 이후.
- README 에 실제 출력 예시.
- 새 판 알림.
- 생존 확인에서 주 모델 · 진단 모델을 동시에 찌르기.
- agy 경로를 환경 변수로 지정 — 저장소 설정으로 심을 수 있는 위험을 풀 방법이 생기면.
- `--staged` 와 `--base` · `--two-dot` 을 함께 주면 거부.
- LICENSE 추가(소유자 결정).
