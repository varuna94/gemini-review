# CHANGELOG

판마다 **달라지는 점**만 적는다. 설치본 갱신은 `/plugin update gemini-review@gemini-review`
뒤 **새 세션**이 필요하다 — 리뷰 배너 첫 줄(`Gemini 교차 리뷰 vX.Y.Z`)로 실제로 돈 판을 본다.

## 1.4.0 — 결과 계약

종료 코드 · 결과 JSON · 플래그의 뜻이 바뀐다. **자동화가 결과를 읽는다면 아래를 먼저 볼 것.**

### 달라지는 점

| 무엇 | 1.3.x | 1.4.0 |
|---|---|---|
| 주 모델이 빈 응답일 때 | 폴백 모델(flash)로 **리뷰를 다시 요청**, 그 approve 가 exit 0 | **판정은 주 모델만.** 진단 모델은 한 줄 생존 확인에만 쓴다 |
| 주 모델만 무응답 · 진단 모델은 응답 | 폴백 모델의 판정 | exit 4 · `primary_model_unavailable` |
| 생존 확인 뒤 재시도 | 폴백 모델 구조화 → 텍스트 | 주 모델 텍스트 재시도 한 번(같은 모델 구조화 재시도 없음) |
| agy 출력 시간 초과 · 래퍼 하드 상한 | "빈 응답" → 생존 확인 · 재시도 · 하드 상한은 exit 2 | exit 4 · `timeout`(`_meta.timeout_kind: agy \| hard`), **재시도 없음** |
| 구독 사용량 한도(쿼터) | exit 2 · `tool_error` | exit 4 · `quota_exhausted` · `retry_after` |
| agy 가 모델명을 모름 | exit 2 · `tool_error` | exit 2 · `model_unavailable` |
| `--staged` 인데 스테이징이 비었음 | exit 0 · `no_changes` | **exit 8** · `no_changes` (`--allow-empty` 면 0) |
| 스크립트 내부 예외 | traceback · exit 1 · 파일은 `in_progress` | exit 1 · `internal_error` 기록(traceback 은 그대로) |
| 결과 JSON | 최상위 `mode` · `model` · `elapsed_seconds` | **`_meta`** 를 모든 종료 경로가 남긴다(아래) |
| LLM 응답의 `model` · `_meta` 같은 키 | 결과 파일에 섞일 수 있었다 | 스키마 키(`verdict` · `summary` · `findings` · `questions`)만 옮기고 나머지는 버린다 |

`_meta` 필드: `mode` · `exit_code` · **`passed`**(주 모델이 리뷰해 통과 판정일 때만 true) ·
`model` · `elapsed_seconds` · `calls`(agy 호출마다 단계 · 모델 · **원인** · 소요) ·
`scope`(범위와 **해석된 SHA**) · `plugin_version`, 경우에 따라 `timeout_kind` ·
`dropped_llm_keys`.

### 플래그

- `--fallback-model` → **`--probe-model`**, `--no-fallback` → **`--no-probe-fallback`**. 뜻이
  "판정도 내는 대체 모델" 에서 "진단 전용 모델" 로 바뀌어 이름을 바꿨다. 옛 이름은 1.4.x
  동안 **경고와 함께** 같은 뜻으로 받는다(2.0 에서 뺀다).
- `--allow-empty` 신설 — 빈 스테이징을 8 대신 0 으로(그래도 `_meta.passed` 는 false).
- `--check` 신설 — 코드를 보내지 않고 python · git · agy · 로그인 · 모델 응답과, 해석 계층이
  기대는 agy 출력 표면(래퍼 필드 · 시간 초과 안내 · 모델 없음 안내)을 점검한다.
- `--help` 에 모든 플래그의 형식 · 기본값과 종료 코드 표를 싣는다.

### 호환

- 0 · 1 · 2 · 3 · 5 · 6 · 130 · 143 의 뜻은 그대로다. **4 의 원인이 여럿으로 갈렸고**, 8 이 새로 생겼다.
- 결과 JSON 최상위 `mode` · `model` 은 1.4.x 동안 `_meta` 와 같은 값으로 함께 쓴다(2.0 에서 뺀다).
  최상위 `elapsed_seconds` 는 `_meta.elapsed_seconds`(전체 소요)와 `_meta.calls[].seconds` 로 옮겼다.
- 최악 소요(`--timeout 10m`)는 약 27분이다(구조화 720 + 생존 확인 180 + 텍스트 720초).
  1.3.x 는 폴백 구조화 720초가 더 붙었다.

### 안에서 바뀐 것

- agy 호출이 `_run_agy` 한 곳으로 모였다(`--mode plan` · 하드 상한 · stdin 차단).
- 원인 판정이 `_classify_run` 한 곳으로 모였다(구조화 · 생존 확인 · 텍스트 재시도가 같은 규칙).
- 테스트: 결과 계약표의 모든 행을 가짜 agy 로 도는 매트릭스, 실측 출력으로 원인 분류,
  cp949 도움말, 해석된 SHA, `--check`. 변이 검사 38개가 전부 기대한 이유로 빨개지는지 판마다 본다.
- CI(GitHub Actions): ubuntu · windows 에서 스위트, ubuntu 에서 변이 검사.

## 1.3.2 — 병합에서 사라진 가드 복구 (2026-09-14)

동작 계약(종료 코드의 뜻 · `--out` 키)은 그대로인 결함 수정이다.

- 텍스트 재시도가 agy 실패 · 출력 시간 초과의 부분 출력을 리뷰 원문으로 쓰던 길을 막았다(v1.3.0 병합에서 사라졌던 가드).
- `--out` 을 인자 해석 **전에** 무효화하고, 쓸 수 없으면 리뷰를 시작하지 않는다(exit 2). 결과 파일은 원자적 · 사용자 전용(0600, `$XDG_STATE_HOME/gemini-review/`).
- 중단(SIGTERM · SIGINT · SIGHUP) 때 임시 폴더를 정리하고 `mode: interrupted` · exit 130 · 143 을 남긴다.
- agy 를 절대 경로로만 실행한다(현재 폴더 · 저장소 안 PATH 항목 제외). agy 가 실패를 알리면(rc≠0 · `status=ERROR`) 빈 응답으로 읽지 않는다.
- diff 를 사용자 git 설정과 무관하게 만든다(`diff.renames=false` 등 — 이름 변경으로 민감 경로 판정을 비껴가던 길).
- 판정 모델을 코드가 대입한다(LLM 응답의 `model` 이 남던 결함). 리뷰 JSON 이 둘 이상이면 판정하지 않는다.
- SKILL.md: flash 권장 삭제, 모델 고정 절 복구, `${CLAUDE_SKILL_DIR}` + 인터프리터 자동 탐지 블록, 백그라운드 실행 · 파이프 금지, `--allow-sensitive` 자동 사용 금지.
- 배너 · `--version` 에 판과 스크립트 경로. 변이 검사(`tests/mutation_check.py`).
- 리뷰 프롬프트가 셸 명령 시도를 막는다(헤드리스 agy 가 권한을 자동 거부하면 응답 전체가 사라졌다).

## 1.3.1 (2026-09-14)

- SKILL.md 실행 경로를 `${CLAUDE_PLUGIN_ROOT}` 로 — 플러그인 설치본에서 안내된 경로에 파일이 없었다.

## 1.3.0 (2026-09-11)

- 두 PC 에서 갈라진 판을 병합했다(종료 코드 5 · 6, 민감 경로 가드 수정, 생존 확인 · 폴백).
  ⚠ 이 병합에서 텍스트 재시도 가드와 SKILL.md 의 모델 고정 절이 사라졌다 — 1.3.2 에서 복구.

## 1.2.0 이전 (2026-08-27)

- 독립 플러그인 저장소로 분리. 민감 경로 판정 · 결과 파일 무효화 · 빈 응답 진단의 초기 판.
