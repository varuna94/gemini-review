---
name: gemini-review
version: 1.0.0
description: Gemini 3.x 로 변경분을 교차 리뷰한다 (Antigravity CLI). 커밋 직전 독립 리뷰어로 쓴다. 같은 모델이 짠 코드를 같은 모델이 리뷰할 때 생기는 맹점을 잡는다.
allowed-tools:
  - Bash
  - Read
  - Edit
  - Write
  - Grep
  - Glob
---

# Gemini 교차 리뷰

Claude 가 작성한 변경분을 **Gemini 3.1 Pro** 에게 독립적으로 리뷰시킨다.

## 왜 쓰는가

같은 모델이 짠 코드를 같은 모델이 리뷰하면 **같은 맹점을 공유한다.** 도입 당일
실측에서, "silent-dead 를 막겠다"며 넣은 가드가 **두 번 연속 dead** 였고 둘 다
이 교차 리뷰가 잡았다. 자기 코드를 다시 읽는 것만으로는 잡기 어려운 계열이다.

## 실행

⚠ **아래 배너가 보이지 않으면 리뷰는 수행되지 않은 것이다.** 종료 코드만 보고
판단하지 말 것 — 이 스킬의 조용한 실패는 전부 여기서 걸러진다.

```
범위: staged / 변경 파일 N개 / diff N자
```

리눅스·맥:

```bash
# 경로와 인터프리터를 스스로 찾는다. 못 찾으면 리뷰를 돌리지 않고 죽는다.
GR="${CLAUDE_PLUGIN_ROOT:-}/skills/gemini-review/gemini_review.py"
[ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "$GR" ] || GR=$(ls -1dt \
  "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/*/gemini-review/*/skills/gemini-review/gemini_review.py \
  2>/dev/null | head -1)
PY=$(command -v python3 || command -v python)
[ -f "${GR:-}" ] && [ -n "${PY:-}" ] || {
  echo "!! gemini_review.py 또는 python 을 찾지 못했다 — 리뷰가 수행되지 않았다"; exit 2; }

"$PY" "$GR" --staged
```

**범위를 바꿀 때는 마지막 줄의 인자만 바꾼다.**

| 목적 | 인자 |
|---|---|
| 커밋 직전 (스테이징된 변경) | `--staged` |
| 마지막 커밋 | (없음) |
| 최근 3개 커밋 | `--base HEAD~3` |
| 브랜치 전체 (PR 전) | `--base main` |

⚠ **여러 명령을 한 블록에 이어 붙이지 말 것.** `set -e` 가 없으므로 앞 명령이
exit 5 로 죽어도 뒤 명령이 계속 돌고, 마지막 명령의 종료 코드만 남아 **지적이
조용히 사라진다.** 한 번에 하나만 돌린다.

**`--base` 는 merge-base 기준(3-dot)이다** [26.08.13]. `--base main` 은 내가
브랜치를 딴 지점 이후의 **내 변경만** 본다 — 그 사이 main 에 들어온 남의
커밋은 섞이지 않는다. 2-dot 이 필요하면 `--two-dot` 이지만, 브랜치 리뷰에서
쓰면 동료 커밋이 **삭제로 뒤집혀** diff 에 들어가 유령 지적을 만든다.

Windows PowerShell:

```powershell
$CFG = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { "$env:USERPROFILE\.claude" }
$GR = "$env:CLAUDE_PLUGIN_ROOT\skills\gemini-review\gemini_review.py"
if (-not (Test-Path $GR)) {
  $GR = (Get-ChildItem "$CFG\plugins\cache\*\gemini-review\*\skills\gemini-review\gemini_review.py" -EA SilentlyContinue |
         Sort-Object LastWriteTime | Select-Object -Last 1).FullName
}
$PY = (Get-Command py, python -EA SilentlyContinue | Select-Object -First 1).Source
if (-not $GR -or -not $PY) { throw "gemini_review.py 또는 python 을 찾지 못했다 — 리뷰가 수행되지 않았다" }

& $PY $GR --staged
exit $LASTEXITCODE   # ⚠ 없으면 exit 5(지적 있음)가 0 으로 삼켜진다
```

**`$CLAUDE_PLUGIN_ROOT` 는 이 플러그인이 설치된 디렉토리다.** 캐시 경로에 버전이
들어가므로(`plugins/cache/<마켓>/<플러그인>/<버전>/`) 절대경로를 하드코딩하면
다음 업데이트에서 깨진다. 변수가 비었을 때를 대비해 위 블록이 캐시에서 직접
찾는 폴백을 갖고 있다 — **그 폴백까지 실패하면 리뷰를 돌리지 않고 죽는다.**

⚠ **인터프리터 이름이 플랫폼마다 다르다.** 우분투에는 `python` 이 없고
`python3` 만 있다. 반대로 Windows 기본 설치에는 `python3` 가 없고 `py` 또는
`python` 이다. 위 블록이 있는 쪽을 골라 쓰므로 손으로 고르지 말 것 — 잘못
고르면 command not found 로 죽고, 그 실패가 **'지적 없음' 으로 조용히 오독된다.**

표준 라이브러리만 쓰므로 **어떤 Python 3.7+ 로도** 실행된다.

## 모델 — `gemini-3.1-pro-high` 고정

**flash 계열로 바꾸지 말 것** (사용자 지시, 2026-08-20 · 08-21 재확인). 정답을
아는 diff 로 잰 실측에서 `gemini-3.7-flash-high` 는 **7회 중 5회가 빈 응답**이고
나머지는 검출 0 이었다. 품질 이전에 게이트로 쓸 수가 없다. 통과를 쉽게 내주는
리뷰어는 "리뷰를 받았다" 는 기분만 남기고 실제 방어를 없앤다 — 리뷰가 없는 것보다
나쁘다. pro 등급의 새 버전이 나오면 같은 방식으로 재서 교체한다.

`claude-*` 계열도 쓰지 않는다 — 리뷰를 요청하는 쪽이 Claude 라 교차 리뷰의
전제가 무너진다. 다른 계열이 필요하면 `gpt-oss-120b-medium` 정도이고, 그때도
사람에게 먼저 묻는다.

⚠ pro-high 가 빈 응답으로 실패하면 **모델을 낮추지 말고 재시도**한다. 대개
통과한다. 계속 실패하면 "리뷰가 수행되지 않았다" 고 그대로 보고한다.

## 전제

- `agy`(Antigravity CLI)가 설치·인증돼 있어야 한다.
  설치: `irm https://antigravity.google/cli/install.ps1 | iex`
  인증: `agy` 를 한 번 실행해 Google 계정(AI Pro/Ultra 구독)으로 로그인.
- **Gemini API 키가 아니라 구독으로 인증된다.** 저장소에서 Gemini API 를 별도로
  쓰고 있다면 그 quota 를 잠식하지 않는다.

## 프로젝트별 리뷰 관점 주입 (권장)

저장소 루트에 **`.gemini-review.md`** 를 두면 그 내용이 리뷰 지시에 덧붙는다.
그 저장소에서 **실제로 발생했던 사고**를 적을수록 리뷰가 날카로워진다.

````markdown
# 예시: .gemini-review.md

이 저장소는 결제 정산 배치다. 금액 오류가 즉시 실손실이다.

## 실제로 반복된 실패
1. 통화 단위 혼재 — 원/센트가 세 지점에서 다르게 해석됨
2. 재시도 시 멱등성 깨짐 — 중복 정산 발생 이력
3. 집계 쿼리가 서로 다른 기간 필터를 씀
````

## 판정 결과 읽는 법

`verdict` 는 `approve` / `approve_with_comments` / `request_changes`.
각 지적에는 `failure_scenario`(구체적 입력 → 잘못된 결과)가 붙는다 — 이것이
없는 지적은 프롬프트가 걸러내게 돼 있다.

**종료 코드** [26.08.25 에 5·6 신설]:

| 코드 | 뜻 |
|---|---|
| 0 | 통과 — `approve` 또는 `approve_with_comments` |
| 1 | JSON 파싱 실패 |
| 2 | 실행 실패 (경로·인터프리터·agy 를 못 찾음 포함) |
| 3 | 민감 경로 감지 — 전송 중단 |
| 4 | 빈 응답 — **리뷰가 안 된 것이다** |
| 5 | `request_changes` — 지적이 있다 |
| 6 | 구조화 실패, 텍스트 모드로 받음 — 사람이 원문을 읽어야 한다 |

⚠ **exit 4(빈 응답)를 '지적 없음'으로 읽지 말 것.** 리뷰가 안 된 것이다.
재시도하거나 범위를 좁힌다. 모델은 `gemini-3.1-pro-high` 를 유지한다.

⚠ **exit 0 도 무조건 통과는 아니다.** 스테이징이 비어 "변경분이 없다" 로 끝나도
0 이다. 판정을 믿기 전에 배너의 `범위:`·`변경 파일 N개` 를 먼저 볼 것.

⚠ **모르는 판정값은 통과로 치지 않는다**(fail-closed). 모델이 스키마 밖 문자열을
내면 5 로 죽는다 — '판정 불가' 를 '정상' 으로 읽는 계열을 막는다.

## 안전

- **`--mode plan` 고정** — read-only. Gemini 가 저장소 파일을 수정할 수 없다.
  이 플래그는 절대 풀지 않는다.
- 민감 경로(`.env`·`secrets/`·`credentials/`·`*.pem`/`*.key`·`id_rsa`·`.npmrc`
  등)가 diff 에 있으면 **전송을 중단**한다. 의도한 것이면 `--allow-sensitive`.
- 판정은 **경로 세그먼트·확장자·단어경계** 단위다 [26.08.13]. 종전 부분문자열
  매칭은 `design-tokens.ts`·`TokenService.php` 같은 평범한 파일을 상시 차단했고,
  그 오탐이 `--allow-sensitive` 를 습관으로 만들어 **가드를 영구히 끄는**
  경로였다. `secret`·`key` 같은 단어는 **데이터·설정 확장자**(`.json`·`.yml`·
  `.txt`·확장자 없음 …)에서만 보므로 `key.svg`·`keys.tf`·`apiKey.mjs` 는
  통과하고 `client_secret.json`·`credentials.txt` 는 막힌다.
  `.env.example` 계열은 차단 대신 경고만, `prod.env.bak` 같은 백업은 원래
  파일로 되돌려 판정한다.
- **강행해도 조용하지 않다** — `--allow-sensitive` 는 전송되는 파일 목록을
  화면에 남긴다. 종전엔 목록 출력 자체가 사라져 사후 추적이 불가능했다.

⚠ **이 가드는 diff 만 검사한다.** Gemini 는 저장소 전체를 읽을 수 있고
프롬프트가 "다른 파일도 읽어 맥락을 확인하라"고 지시하므로, **gitignore 된
`.env` 는 구조적으로 가드 밖**이다 (Laravel·Rails·Node 처럼 작업트리에 비밀이
상주하는 스택에서 특히 그렇다). 파일명만 보므로 diff **내용**에 하드코딩된
키(`sk_live_…`)도 잡지 못한다. 코드가 Google 서버로 전송된다는 점은 변하지
않는다 — 비공개 저장소에서 쓸 때 이 점을 인지하고 판단할 것.

## 지적을 다루는 원칙

**액면 그대로 받지 마라.** 도입 당일 받은 7건 모두 소스·DB 실측으로 확인한 뒤
반영했다. 리뷰어가 둘이 됐다고 검증을 생략하면 오히려 위험하다. 특히:

- 지적이 **사실인지** 먼저 확인한다(해당 코드·데이터를 직접 본다).
- 사실이면 고치고, **회귀 테스트로 고정**한다.
- 아니면 그 근거를 남기고 넘어간다.

## 알려진 함정

- **`--effort` 를 따로 주지 마라.** agy 는 effort 가 모델명에 내장돼 있다
  (`-high`/`-low`/`-medium`). 모델 접미사와 다른 값을 주면 즉시 `status=ERROR`
  로 죽는다(실측: 0초, tokens 0).
- 리뷰 1회에 **1~2분** 걸린다. 그 시간을 줄이려고 모델을 낮추지 않는다(아래 참조).
- 결과 JSON 은 기본적으로 임시 디렉토리에 저장된다. 저장소 안에 남기려면
  `--out` 을 쓰되 **그 경로를 gitignore 하라** (리뷰 대상 코드가 담긴다).
