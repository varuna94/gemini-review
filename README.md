# gemini-review

커밋 직전에 변경분을 **Gemini 3.x 로 교차 리뷰**하는 Claude Code 플러그인이다.

같은 모델이 짠 코드를 같은 모델이 리뷰하면 **같은 맹점을 공유한다.** 도입 당일
실측에서, "조용한 실패를 막겠다"며 넣은 가드가 두 번 연속 죽어 있었고 둘 다 이
교차 리뷰가 잡았다. 자기 코드를 다시 읽는 것만으로는 잡기 어려운 계열이다.

## 설치

```
/plugin marketplace add varuna94/gemini-review
/plugin install gemini-review@gemini-review
```

설치한 뒤 **Claude Code 세션을 새로 열어야** 스킬이 잡힌다.

### 준비물

| 무엇 | 확인 | 없으면 |
|---|---|---|
| `agy` (Antigravity CLI) | `agy --version` | [antigravity.google/cli](https://antigravity.google/cli) 에서 설치하고 Google 계정으로 인증한다 |
| Google AI Pro/Ultra 구독 | — | **필수.** agy 인증에 쓰인다. 무료 계정으로는 리뷰가 돌지 않는다 |
| Python 3 | `python3 --version` (Windows 는 `py --version`) | 대부분 이미 있다 |

`agy` 는 **Google AI Pro/Ultra 구독**으로 인증되므로, 저장소가 `GEMINI_API_KEY`
(무료 tier)를 따로 쓰고 있어도 그 quota 를 잠식하지 않는다.

파이썬 이름은 환경마다 다르다(우분투·맥 `python3`, Windows `py` · `python`). 스킬이
있는 쪽을 골라 실행하도록 안내한다. **`agy` 를 못 찾으면 종료 코드 2 로 끝난다** —
리뷰를 돌리지 않은 채 "지적 없음"으로 지나가지 않는다.

## 쓰는 법

Claude Code 세션에서 `/gemini-review` 를 부르면 된다. 범위는 인자로 정한다.

| 목적 | 인자 |
|---|---|
| 커밋 직전 (스테이징된 변경) | `--staged` |
| 마지막 커밋 | (없음) |
| 최근 3개 커밋 | `--base HEAD~3` |
| 브랜치 전체 (PR 전) | `--base main` |

`--base` 는 **merge-base 기준(3-dot)** 이다. `--base main` 은 브랜치를 딴 지점
이후의 내 변경만 본다 — 그 사이 main 에 들어온 남의 커밋은 섞이지 않는다.

그 밖의 인자: `--model`(기본 `gemini-3.1-pro-high` — **flash 로 낮추지 말 것**),
`--timeout`(기본 `10m`), `--out`(결과 JSON 저장 경로. 생략하면
`~/.local/state/gemini-review/`, Windows 는 `%LOCALAPPDATA%\gemini-review\`),
`--allow-sensitive`(사용자가 전송을 명시적으로 승인했을 때만).

## 결과를 읽는 법

⚠ **배너가 보이지 않으면 리뷰는 수행되지 않은 것이다.** 종료 코드만 보고
판단하지 말 것.

```
범위: staged / 변경 파일 N개 / diff N자
```

| 종료 코드 | 뜻 |
|---|---|
| `0` | 통과 (`approve` · `approve_with_comments`) |
| `1` | 파싱 실패 |
| `2` | 실행 실패 — git · `agy` 를 못 찾음, agy 가 실패를 알림(모델명 · 인증), `--out` 에 못 씀 |
| `3` | 민감 경로가 diff 에 있어 중단 |
| `4` | **빈 응답 — 리뷰가 안 된 것이다.** '지적 없음'으로 읽지 말고 다시 돌린다 |
| `5` | `request_changes` |
| `6` | 구조화 실패 (텍스트 모드로 리뷰는 받았다) |
| `130` · `143` | 중단(Ctrl+C · 종료 신호) — 리뷰가 안 된 것이다 |

**이 표에 없는 코드는 통과가 아니다.** exit 4 는 몇 분 두고 2회까지 다시 돌리고,
그래도 안 되면 "리뷰가 수행되지 않았다" 고 보고한다(가능하면 다른 계열 리뷰로 대체).

**`0` 도 무조건 통과가 아니다.** 스테이징이 비면 "변경분이 없다"로 끝나며 0 이다.
화면의 `범위:`·`변경 파일 N개` 를 먼저 확인하라.

**지적을 액면 그대로 받지 말 것.** 해당 코드와 데이터를 직접 보고 사실인지
확인한 뒤 고치고, 아니면 근거를 남기고 넘어간다. 리뷰어가 둘이 됐다고 검증을
생략하면 오히려 위험하다.

## ⚠ diff 는 Google 서버로 전송된다

민감 경로(`.env` · `secrets/` · `*.pem` · `id_rsa` 등)가 diff 에 있으면 스킬이
**중단**한다. 다만 **그 가드는 diff 만 검사한다** — gitignore 된 `.env` 나 diff
안에 하드코딩된 키(`sk_live_…`)는 구조적으로 잡지 못한다.

사내 코드나 고객 데이터가 걸린 저장소에서는 **전송 자체가 허용되는지 먼저
판단**하라. `--mode plan` 고정이라 Gemini 가 파일을 수정하지는 못한다.

## 저장소마다 리뷰를 날카롭게 하려면

저장소 루트에 **`.gemini-review.md`** 를 두면 그 내용이 리뷰 지시에 덧붙는다.
없어도 범용 렌즈로 동작한다.

적을 것은 그 저장소에서 **실제로 반복된 실패**다. 일반론이 아니라 사고 이력이다.

```markdown
이 저장소는 결제 정산 배치다. 금액 오류가 즉시 실손실이다.

## 실제로 반복된 실패
1. 통화 단위 혼재 — 원/센트가 세 지점에서 다르게 해석됨
2. 재시도 시 멱등성 깨짐 — 중복 정산 발생 이력
3. 집계 쿼리가 서로 다른 기간 필터를 씀
```

## 고칠 때

```
python3 tests/test_gemini_review.py   # 동작 검사 (수 초)
python3 tests/mutation_check.py       # 가드를 되돌리면 실제로 빨개지는지 (2분 안팎)
```

Windows 는 `python3` 대신 `py` 로 부른다.

가드나 문서의 규칙을 건드렸다면 두 번째도 돌린다. 테스트가 초록이라는 것만으로는
가드가 살아 있다는 증거가 아니다 — 이 저장소에서 두 번 그랬다.

## 업데이트

```
/plugin update gemini-review@gemini-review
```

마찬가지로 **세션을 새로 열어야** 새 판이 로드된다. 리뷰 배너 첫 줄(`Gemini 교차 리뷰 v1.3.2 …`)
과 `스크립트:` 줄로 실제로 돈 판과 경로를 확인할 수 있다.
