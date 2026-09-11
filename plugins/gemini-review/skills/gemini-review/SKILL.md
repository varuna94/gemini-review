---
name: gemini-review
version: 1.0.0
description: Gemini 3.x 로 변경분을 교차 리뷰한다 (Antigravity CLI). 커밋 직전 독립 리뷰어로 쓴다. 같은 모델이 짠 코드를 같은 모델이 리뷰할 때 생기는 맹점을 잡는다.
triggers:
  - gemini review
  - 교차 리뷰
  - cross review
  - 제미나이 리뷰
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

```bash
python ~/.claude/skills/gemini-review/gemini_review.py --staged   # 커밋 직전
python ~/.claude/skills/gemini-review/gemini_review.py            # 마지막 커밋
python ~/.claude/skills/gemini-review/gemini_review.py --base HEAD~3
python ~/.claude/skills/gemini-review/gemini_review.py --base main # 브랜치 전체 (PR 전)
python ~/.claude/skills/gemini-review/gemini_review.py --model gemini-3.8-flash-high  # 빠르게
```

**`--base` 는 merge-base 기준(3-dot)이다** [26.08.13]. `--base main` 은 내가
브랜치를 딴 지점 이후의 **내 변경만** 본다 — 그 사이 main 에 들어온 남의
커밋은 섞이지 않는다. 2-dot 이 필요하면 `--two-dot` 이지만, 브랜치 리뷰에서
쓰면 동료 커밋이 **삭제로 뒤집혀** diff 에 들어가 유령 지적을 만든다.

Windows PowerShell 에서는 `$env:USERPROFILE\.claude\skills\gemini-review\gemini_review.py`.

표준 라이브러리만 쓰므로 **어떤 Python 3.7+ 로도** 실행된다.

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

**종료 코드**

    0  통과 (approve · approve_with_comments)
    5  request_changes — 고치고 다시 돌릴 것          [26.08.25 신설]
    6  구조화 실패(텍스트 폴백) — 사람이 원문을 읽어야 한다  [26.08.25 신설]
    1 파싱 실패 / 2 실행 실패 / 3 민감 경로 감지 / 4 빈 응답

⚠ **exit 4(빈 응답)를 '지적 없음'으로 읽지 말 것.** 리뷰가 안 된 것이다.

⏱ **[26.09.10] 빈 응답의 원인은 두 갈래다.** 호출 소요가 화면에 찍히고
(`응답: <모델> · N초`), 빈 응답이면 **한 줄 프롬프트로 계층 생존을 먼저
확인**한다.

- 생존 확인 실패 = **도구 계층 장애**. 재시도로 넘어가지 않으므로 즉시 멈춘다
  (exit 4 · `--out` 에 `mode: tool_unavailable`). `agy models` 로 인증을 보고
  **시간을 두고** 다시 돌릴 것.
  ⚠ **"모델을 바꿔도 소용없다" 는 두 모델을 확인했을 때만 참이다.** 기본
    경로는 주 모델이 죽으면 폴백 모델로도 찔러 보고 둘 다 죽었을 때만 그렇게
    말한다. `--no-fallback` 이면 확인한 것이 하나뿐이라 화면이 *"주 모델
    하나만 확인했다"* 로 갈라 적는다 — **확인한 만큼만 말한다.**
- 생존하면 프롬프트·스키마 쪽이다. **폴백 모델로 구조화를 한 번 더** 시도한
  뒤에야 텍스트 모드로 내려간다 (`--fallback-model` · `--no-fallback`).

근거는 실측이다 — 그날 다섯 단어 프롬프트조차 2분 무응답이었고 폴백 모델도
439초 output 0 이었다. 종전 코드는 그 구분 없이 재시도해 400~550초를 더 태웠다.

⚠ 배너의 **`diff N자` 는 프롬프트 길이가 아니다** (diff 는 파일 경로로 넘어간다).

⏱ **바깥 하드 타임아웃이 있다.** `--timeout` 은 agy 에게 맡기는 상한인데,
응답 불능이 의심되는 바로 그 도구에 상한을 맡기는 셈이라 지켜지지 않을 수
있다(재인증 프롬프트 · 자체 타임아웃 실패). 그래서 래퍼가 `--timeout` +
여유분에서 프로세스를 직접 자른다. 생존 확인용 한 줄 프롬프트에도 같은
장치가 걸려 있고, 그쪽 상한은 **짧게 고정**이다 — 계층이 죽었는지 보는
확인에 몇 분을 태우면 그 확인이 존재하는 이유가 사라진다.

📄 **`--out` 은 시작 시점에 무효화된다.** 어떤 경로로 끝나든 **직전 실행의
낡은 JSON 이 남지 않는다**(리뷰 중에는 `mode: in_progress`).
⛔ 자동화가 이 파일을 읽는다면 그 계약에 기대도 된다 — 종전에는 파싱 실패·
실행 실패·민감 경로·**변경분 없음**이 파일을 손대지 않아, `--staged` 인데
스테이징을 빠뜨리면 종료코드도 0 이고 파일도 어제의 `approve` 라 **양쪽에서
통과로 읽혔다.**

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
- **diff 를 담은 임시 디렉터리는 종료 시 지워진다.** agy 에는 파일 경로로
  넘기므로 `%TEMP%`/`/tmp` 에 `changes.diff`(저장소 코드 전문)가 잠시 놓인다.
  ⛔ **이 정리는 두 번 잃어버린 적이 있다** — 처음엔 50개 1.5MB, 두 번째는
    **3,116개**(26.09.11 실측). 사람이 리뷰를 돌린 횟수와 무관하다: 회귀
    테스트가 진입점을 반복 호출하므로 스위트 1회당 수십 개씩 늘어난다.
    용량보다 **내용**이 문제다 — 비공개 저장소의 소스가 평문으로 남는다.
    가드는 `tests/test_gemini_review_guards.py::
    test_the_temp_dir_is_registered_for_cleanup` 이고, 등록 **여부**만이 아니라
    등록된 경로가 **그 diff 를 담은 디렉터리인지**까지 본다.

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
- 리뷰 1회에 **1~2분** 걸린다. 급하면 `--model gemini-3.8-flash-high`.
  ⚠ 폴백 기본값(`gemini-3.6-flash-high`)과 **같은 값을 주지 말 것** — 그러면
  폴백 재시도가 건너뛰어진다(스크립트가 자동으로 갈아타되 화면에 남긴다).
- 결과 JSON 은 기본적으로 임시 디렉토리에 저장된다. 저장소 안에 남기려면
  `--out` 을 쓰되 **그 경로를 gitignore 하라** (리뷰 대상 코드가 담긴다).

## 스킬 실행 절차 (Skill Execution Steps)

사용자가 \gemini review\, \교차 리뷰\ 등을 요청하여 이 스킬이 트리거되면 다음 절차를 따르시오:

1. **상태 확인 (Check State):** \git status\ 및 \git diff\를 통해 현재 스테이징된 변경사항인지, 작업 트리 변경사항인지 파악합니다.
2. **스크립트 실행 (Execute Script):**
   - 스테이징된 변경분 리뷰: \python ~/.claude/skills/gemini-review/gemini_review.py --staged\
   - 마지막 커밋 리뷰: \python ~/.claude/skills/gemini-review/gemini_review.py\
     ⚠ 인자 없는 실행은 **`--base HEAD~1`**, 즉 직전 커밋이다. **커밋되지 않은
     작업 트리 변경은 어떤 인자로도 리뷰되지 않는다** — 스테이징한 뒤
     `--staged` 를 쓸 것. 종전 이 줄은 이것을 "작업 트리 리뷰" 라고 적어,
     오늘 변경이 한 줄도 안 실린 채 어제 커밋에 `approve` 가 나오고 커밋 전
     필수 요건이 충족된 것으로 기록될 수 있었다 [26.09.10 정정].
   - 특정 브랜치(예: main) 대상 리뷰: \python ~/.claude/skills/gemini-review/gemini_review.py --base main\
3. **결과 출력 (Present Results):** 스크립트 실행 후 출력되는 JSON 혹은 텍스트 형태의 지적 사항(findings)을 가공하거나 생략하지 말고 **원문 그대로(verbatim)** 사용자에게 전달하십시오. 
4. **후속 조치 (Follow up):** 지적 사항 중 \[CRITICAL]\이나 \[HIGH]\ 심각도의 문제가 있다면, 사용자에게 해당 부분을 즉시 수정할지(Fix) 물어보고 조치하십시오.

