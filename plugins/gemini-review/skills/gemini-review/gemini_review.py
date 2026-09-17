# [26.08.12] gemini_review — Antigravity CLI(agy)로 Gemini 교차 코드 리뷰 (전역)
"""변경분을 **Gemini** 에게 독립적으로 리뷰시킨다 (Claude 리뷰와 교차검증용).

왜 필요한가: 같은 모델이 짠 코드를 같은 모델이 리뷰하면 같은 맹점을 공유한다.
도입 당일 실측으로, 어시스턴트가 "silent-dead 를 막겠다"며 만든 가드가 **두 번
연속 dead** 였고 둘 다 이 교차 리뷰가 잡았다.

## 왜 API 가 아니라 agy 인가

`agy`(Antigravity CLI)는 **Google AI Pro/Ultra 구독**으로 인증된다. Gemini API
키(무료 tier)를 쓰는 다른 배치가 있는 저장소라면 그 quota 를 잠식하지 않는다.

## 안전

- **`--mode plan` 고정** — read-only. Gemini 가 저장소 파일을 수정할 수 없다.
- diff 는 임시 파일로 넘긴다 (Windows 명령줄 길이 제한 ~8191자 회피).
- 민감 경로(.env·secrets·credentials·*.pem/key 등)가 diff 에 있으면 **중단**한다.
  `--allow-sensitive` 는 사용자의 명시 승인 뒤에만 쓴다.

## 프로젝트별 리뷰 관점 주입

저장소 루트에 **`.gemini-review.md`** 가 있으면 그 내용을 리뷰 지시에 덧붙인다.
그 저장소에서 실제로 반복된 실패 계열을 적어두면 리뷰 품질이 크게 올라간다.
없으면 범용 지시만 쓴다.

사용법:
    python gemini_review.py                    # 마지막 커밋
    python gemini_review.py --base HEAD~3      # 최근 3커밋
    python gemini_review.py --staged           # 스테이징된 변경 (커밋 직전)
    python gemini_review.py --paths a.py b.py  # `git commit -- a.py b.py` 가 담을 변경 (1.7.0)
    python gemini_review.py --staged --record-fallback --reviewer 이름 --summary 요약
                                               # Gemini 가 수행되지 않았을 때 대체 리뷰 기록 (1.7.0)

⚠ 모델은 `gemini-3.1-pro-high` 고정이다 — 느리다고 flash 로 낮추지 않는다(SKILL.md).
  **판정은 주 모델만 낸다.** 진단 모델(`--probe-model`)은 빈 응답일 때 한 줄 생존
  확인에만 쓴다(1.4.0).

종료 코드: 0 통과(주 모델의 approve · approve_with_comments) / 1 파싱 실패 · 내부 오류 /
          2 실행 실패 / 3 민감 경로 / 4 리뷰 안 됨(시간 초과 · 쿼터 · 무응답 · 빈 응답) /
          **5 request_changes** / **6 구조화 실패** / **8 빈 스테이징** /
          130 · 143 중단(신호). 이 목록에 없는 코드는 통과가 아니다.

⚠ [26.08.25] 5·6 은 **신설**이다. 종전엔 판정과 무관하게 0 이었다 — 실측
  리뷰 236건 중 `request_changes` 가 **141건(68%)** 이고 critical 지적이 111건인데
  전부 exit 0 이었다. 그래서 규약이 *"exit 0 도 통과를 뜻하지 않는다"* 는
  **문서 경고**로 막고 있었고, 이제 그것을 코드로 옮긴다.
⚠ [1.4.0] 결과 계약이 바뀌었다(CHANGELOG.md). 폴백 모델의 approve 가 더는 0 이 아니고,
  `--staged` 인데 스테이징이 비면 8 이다. 모든 종료 경로가 `--out` 에 `_meta`
  (mode · exit_code · passed · model · calls · scope)를 남긴다.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

# agy 는 PATH 에 없을 수 있어 알려진 설치 경로를 fallback 으로 둔다.
_AGY_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe"),
    os.path.expanduser("~/.local/bin/agy"),
    # ⚠ bare "agy" 는 두지 않는다 — PATH 는 `_which_agy` 가 **절대 경로**로 해석한다.
]

# diff 에 이것이 섞이면 외부로 나가면 안 되는 것이 있을 수 있다 → 중단.
#
# ⚠ [26.08.13] 부분문자열 매칭을 **경로 세그먼트·확장자 단위**로 교체했다.
# 종전 `_SENSITIVE_PARTS` 는 `any(p in f.lower() ...)` 였고, 그중 `token` 과
# `.key` 가 웹/프론트엔드 저장소의 평범한 파일을 상시 차단했다 (실측: 현실적
# 경로 26개 중 10건 차단 — `design-tokens.ts` · `anim.keyframes.css`(`.key`
# 부분일치) · `TokenService.php` · `useToken.ts` · `api-tokens.md` …).
# 원 저장소(Python 매매 시스템)엔 그런 파일명이 드물어 한 번도 드러나지 않았다.
#
# 오탐이 잦은 것이 왜 '안전'의 문제인가: 유일한 우회가 all-or-nothing 인
# `--allow-sensitive` 라, 매번 막히면 그 플래그가 습관이 된다. 그 시점부터
# 진짜 `.env` 도 무경고 통과한다 — 느슨한 매칭이 가드를 영구히 끄는 경로다.
# ⚠ `.env` 가 **확장자로도** 온다 — `production.env`·`devel.env` 는 배포
# 스크립트의 흔한 관례다. 실측(회사 저장소 cak-front): `.build/devel.env` ·
# `live.env` · `stage.env` 3건이 세그먼트 규칙만으로는 전부 통과했다.
# 종전 부분문자열 구현은 이것을 잡았으므로 **놓치면 회귀**다 [26.08.13].
_SECRET_EXTS = frozenset((
    ".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".ppk", ".asc",
    ".env",
))
# 이 이름의 **디렉터리 안**이면 민감 (파일명 부분일치가 아니다).
# strict = 예외 없음 (그 안에 소스가 있을 리 없고, 있어도 차단이 옳다).
# soft   = 소스·문서 파일은 예외 — `src/auth/credentials/validator.ts` 같은
#          '비밀을 다루는 코드'를 막으면 다시 오탐 습관화로 돌아간다.
_SECRET_DIRS_STRICT = frozenset((".ssh", ".gnupg", ".aws"))
_SECRET_DIRS_SOFT = frozenset((
    "secrets", "secret", ".secrets", ".secret", "credentials", "credential",
))
_SECRET_BASENAMES = frozenset((
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".htpasswd", ".netrc",
    ".npmrc", ".pypirc", "service-account.json", "token.json", "tokens.json",
    ".token",
))
# basename 을 `-`·`_`·`.` 로 쪼갠 **단어**가 이 집합에 걸리면 민감.
# ⚠ 접두 매칭만으로는 `client_secret.json`(Google OAuth 표준 파일명)·
# `app-secret.yml`·`api_key.json` 을 통째로 놓친다 [26.08.13 Gemini 교차리뷰
# 지적 — 실측 확인. 종전 부분문자열 구현도 같이 놓치던 미탐이라 회귀는 아니다].
# 단어 경계로 보므로 `SecretsManager.ts`(구분자 없음)는 걸리지 않고,
# 아래 `is_code` 예외가 `credentials_test.go` 류를 다시 걸러낸다.
_SECRET_WORDS = frozenset((
    "secret", "secrets", "credential", "credentials",
    "password", "passwords", "passwd", "apikey", "key", "keys", "privatekey",
))
# ⚠ `token` 은 **일부러 빼 둔다** [26.08.25 재검토].
# 넣으면 `api-token.json`·`slack_token.yml` 을 잡지만 `design-tokens.json` 처럼
# 프론트엔드에서 흔한 파일도 함께 차단된다. 이름만으로는 둘을 가를 수 없다.
# 지금은 오탐 회피를 택했고, 그래서 **접두사가 붙은 토큰 파일은 놓친다** —
# `_SECRET_BASENAMES` 의 `token.json`·`tokens.json`·`.token` 만 잡힌다.
# 균형을 바꾸려면 여기에 "token" 을 넣고 그 오탐을 감수하면 된다.
# 단어 규칙은 **데이터·설정 파일에서만** 발동한다.
# ⚠ [26.08.13] 종전엔 "소스 확장자가 아니면" 이라는 여집합이었는데, 여집합은
# 세상의 모든 확장자를 포함한다 — `assets/icons/key.svg` · `infra/keys.tf` ·
# `src/utils/apiKey.mjs` 가 전부 차단됐다(실측). 오탐은 이 도구를 못 쓰게
# 만드는 실패 모드이므로, 화이트리스트로 뒤집어 **비밀이 실제로 담기는 형식**
# 에서만 단어를 본다. 확장자 없는 파일(`credentials`)도 포함한다.
_DATA_EXTS = frozenset((
    ".json", ".jsonl", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
    ".config", ".properties", ".txt", ".csv", ".tsv", ".xml", ".enc", ".gpg",
))
# soft 디렉터리 예외용 — '비밀을 다루는 코드'와 '비밀 그 자체'는 다르다
# (`credentials_test.go`·`src/secrets/index.py`·`secrets/README.md`).
# ⚠ `.txt`·`.sql`·`.rst` 는 뺐다. 코드가 아니라 **평문 데이터 컨테이너**라
# `secrets/notes.txt`·`secrets/dump.sql` 을 통과시켰다(조상 구현은 차단).
_CODE_EXTS = frozenset((
    ".go", ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".rb", ".php",
    ".cs", ".rs", ".c", ".h", ".cpp", ".swift", ".md", ".mdx",
    ".css", ".scss", ".less", ".html", ".vue", ".sh", ".ps1",
    # ⚠ `.js` 만 두었더니 `src/secrets/apiKey.mjs` 가 차단됐다(실측). 같은 언어의
    #   같은 코드인데 확장자 표기만으로 갈리던 자리다.
    ".mjs", ".cjs", ".mts", ".cts", ".bash", ".zsh", ".psm1",
))
# placeholder 관례 — 값이 아니라 키 목록만 담는다. 차단하지 않되 **경고**한다
# (관례일 뿐 보증이 아니라서, 통과시키되 사람이 보게 만든다).
_PLACEHOLDER_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")
# 백업 접미는 벗겨서 **원래 파일**로 판정한다 — `prod.env.bak` 은 `.env` 다.
_BACKUP_SUFFIXES = (".bak", ".backup", ".save", ".orig", ".old", ".swp")


def _classify_path(path: str) -> str:
    """민감도 판정. 반환: `""`(안전) / `"block"`(차단) / `"warn"`(통과+경고)."""
    # ⚠ camelCase 경계를 `_` 로 벌린 뒤 소문자화한다. lower() 를 먼저 하면
    #   `clientSecret.json` 이 `clientsecret` 한 덩어리가 되어 아래 단어 규칙의
    #   `secret` 에 걸리지 않고 그대로 외부로 나갔다(실측: `dbPassword.txt` 도 통과).
    #   이 규칙은 데이터·설정 확장자에서만 발동하므로 `apiKey.mjs` 류 오탐은 늘지 않는다.
    # ⚠ [26.08.27 교차리뷰 지적 — 실측 확인] 경계 규칙이 하나면 **약어 뒤**를 못 벌린다.
    #   `(?<=[a-z0-9])(?=[A-Z])` 는 소문자·숫자 뒤의 대문자만 자르므로 `DBPassword` 는
    #   통째로 `dbpassword` 가 되어 `password` 규칙을 빠져나갔다(`AWSCredentials` 도 동일).
    #   두 번째 규칙이 약어와 뒤 낱말의 경계를 자른다: `AWSCredentials` → `AWS_Credentials`.
    norm = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_",
                  re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_",
                         path.replace("\\", "/"))).lower()
    # git 이 인용한 경로에 대한 방어 (근본 해결은 `_git` 의 core.quotePath=false).
    if len(norm) >= 2 and norm.startswith('"') and norm.endswith('"'):
        norm = norm[1:-1]
    segs = [s for s in norm.split("/") if s]
    if not segs:
        return ""
    base, parents = segs[-1], segs[:-1]

    # 하드 디렉터리는 placeholder 강등도 적용하지 않는다.
    if any(p in _SECRET_DIRS_STRICT for p in parents):
        return "block"

    # `prod.env.bak` · `id_rsa.old` — 편집기·배포 스크립트가 흔히 남긴다.
    for _ in range(2):
        if base.endswith("~"):
            base = base[:-1]
        elif base.endswith(_BACKUP_SUFFIXES):
            base = base.rsplit(".", 1)[0]
        else:
            break

    # `.example`·`.template` 등은 관례상 값이 아니라 키 목록이다 → 경고로 강등.
    # ⚠ [26.08.13] 강등을 `.env` 에만 걸었더니 `secrets.yml.example` 은 차단,
    # `server.pem.example` 은 아예 무판정이 되어 규칙이 셋으로 갈렸다. 판정을
    # 한 번에 내고 **마지막에 일괄 강등**하는 형태로 통일한다.
    placeholder = base.endswith(_PLACEHOLDER_SUFFIXES)
    stem = base.rsplit(".", 1)[0] if placeholder else base
    verdict = "warn" if placeholder else "block"

    ext = os.path.splitext(stem)[1]

    if any(p in _SECRET_DIRS_SOFT for p in parents) and ext not in _CODE_EXTS:
        return verdict
    # ⚠ 이름 비교는 구분자를 통일한 뒤에 한다. 목록에 `service-account.json`
    #   하나만 적혀 있어 `serviceAccount.json`·`service_account.json` 이 그대로
    #   통과했다(실측). GCP 키가 실제로 쓰는 이름이라 놓치면 키 전문이 나간다.
    # ⚠ [26.08.27 교차리뷰 지적 — 실측 확인] `_`↔`-` 만 통일하면 **공백·마침표**
    #   변형이 빠져나간다. `service account.json`·`service.account.json` 이 그랬다
    #   — `service` 도 `account` 도 단독으로는 민감 단어가 아니라 이 basename
    #   매칭이 유일한 가드인데, 구분자 한 종류만 보고 있었다.
    _sep = lambda x: re.sub(r"[^a-z0-9]+", "-", x).strip("-")
    if stem in _SECRET_BASENAMES or _sep(stem) in {_sep(b) for b in _SECRET_BASENAMES}:
        return verdict
    if ext in _SECRET_EXTS:
        return verdict
    # `.env` 뒤에 오는 구분자는 `.` 만이 아니다 (`.env-local`·`.env_prod`).
    if stem == ".env" or stem.startswith((".env.", ".env-", ".env_")):
        return verdict
    if ext in _DATA_EXTS or not ext:
        # ⚠ [26.08.27 교차리뷰 지적 — 실측 확인] 구분자가 `-`·`.` 뿐이면 **공백**이
        #   낱말을 가르지 못한다. `my secret.txt` 가 `{"my secret"}` 한 덩어리가 되어
        #   `secret` 규칙을 빠져나갔다. 영숫자가 아닌 것은 모두 구분자로 본다.
        words = set(re.split(r"[^a-z0-9]+", os.path.splitext(stem)[0]))
        words.discard("")
        if words & _SECRET_WORDS:
            return verdict
    return ""

# ⚠ 배포 판. `plugin.json` · SKILL.md frontmatter 와 같아야 한다(테스트가 본다).
#   [26.09.14 DX 교차리뷰] 1.2.0 · 1.3.x 설치 캐시가 함께 있으면 무엇이 돌았는지 알 수
#   없었다 — 배너와 `--version` 에 판과 스크립트 경로를 남긴다.
__version__ = "1.7.0"

_DEFAULT_MODEL = "gemini-3.1-pro-high"
# ⛔ [1.4.0] **진단 전용**이다 — 이 모델로 리뷰를 요청하지 않는다(`_recover_empty_response`).
_DEFAULT_PROBE_MODEL = "gemini-3.6-flash-high"
_CONTEXT_FILE = ".gemini-review.md"

# [26.09.10] **도구 계층 생존 확인.** 빈 응답이 났을 때 원인이 둘 중 어느
# 쪽인지 가른다: ① agy/모델 계층이 통째로 응답하지 않는다 ② 이 프롬프트·스키마
# 조합만 빈 응답을 낸다. 실측 근거는 26.09.10 이다 — 다섯 단어 프롬프트가
# 2분 무응답이었고(`print timeout with turn in progress`), 같은 시각 폴백
# 모델도 439초 output 0 이었다. ①이면 재시도는 전부 낭비다(그날 400~550초
# 짜리 호출이 4회 헛돌았다). 그래서 재시도 **전에** 한 줄을 던져 본다.
_PROBE_PROMPT = "Reply with exactly: OK"
_PROBE_TIMEOUT = "60s"
# ⚠ [26.09.10 리뷰] agy 의 `--print-timeout` 은 **응답 불능이 의심되는 그 도구**
#   에게 상한을 맡기는 것이다. 그것이 지켜지지 않으면(재인증 프롬프트 · 자체
#   타임아웃 실패) 프로세스가 영원히 매달리고, 시간 낭비를 막으려던 확인이
#   가장 큰 낭비가 된다. 그래서 **바깥에서도** 벽시계 상한을 건다. agy 가 스스로
#   끝낼 여유를 주려고 자기 타임아웃보다 넉넉하게 둔다.
_HARD_TIMEOUT_MARGIN = 120


def _duration_seconds(spec: str, default: int) -> int:
    """agy 표기(`1h`·`10m`·`90s`·`600`)를 초로. 해석할 수 없으면 `default`.

    ⚠ [26.09.10 2회차 리뷰] `--timeout` 원문은 agy 에 그대로 넘어가지만 **바깥
      하드 상한은 이 파서가 읽은 값**으로 정해진다. 그래서 못 읽는 표기는 그냥
      기본값이 아니라 **요청보다 훨씬 짧은 상한**이 된다.
      · `1h` 를 못 읽어 720초에 죽였다(agy 는 1시간을 기다린다).
      · `--timeout 0`("제한 없음" 의도)이 120초가 됐다 → 0 이하는 기본값으로 본다.
      · `inf` 는 `int(float("inf"))` 가 **OverflowError** 라 `except ValueError` 를
        비껴가 스택 트레이스로 죽었다.
    """
    text = (spec or "").strip().lower()
    try:
        if text.endswith("h"):
            secs = float(text[:-1]) * 3600
        elif text.endswith("m"):
            secs = float(text[:-1]) * 60
        elif text.endswith("s"):
            secs = float(text[:-1])
        else:
            secs = float(text)
        value = int(secs)
    except (ValueError, OverflowError):
        return default
    return value if value > 0 else default


def _ensure_utf8_stdout() -> None:
    """Git Bash(MSYS)에서 한국어 리뷰가 깨지는 것을 막는다.

    ⚠ [26.08.13] 실측: Git Bash 의 파이썬은 `stdout.encoding=cp949` 인데
    MinTTY 는 UTF-8 로 해석한다 — 한글이 전부 mojibake 로 나오고, cp949 에
    없는 em dash(—)는 `_safe_print` fallback 이 `?` 로 지운다. 프롬프트가
    "한국어로 답하라" 고정이라 **리뷰 본문 전체**가 이 경로를 탄다.

    ⚠ 조건이 `MSYSTEM` 이면 부족하다 [26.08.13 재점검]. Windows 파이썬은
    stdout 이 **진짜 콘솔**이면 PEP 528 로 WriteConsoleW 를 쓰고 encoding 을
    'utf-8' 로 보고한다 — cp949 로 보이는 것은 파이프·리다이렉트된 경우뿐이고
    (Git Bash · CI · 에이전트 캡처), 그 소비자는 대개 UTF-8 이다. 즉
    "UTF-8 이 아니면 전환" 이 정확하며, 진짜 cp949 콘솔을 깨뜨리지 않는다.
    `PYTHONIOENCODING` 이 명시된 경우는 사용자 의도를 존중한다.
    """
    if os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc == "utf8":
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        # ⚠ [26.08.27 교차리뷰 지적 — 실측 확인] 기본값을 utf-8 로 두면 안 된다.
        #   utf-8 은 모든 유니코드를 표현하므로 replace 가 아무것도 바꾸지 않고,
        #   같은 문자열을 다시 print 해 **두 번째 UnicodeEncodeError 로 죽는다**
        #   — 리뷰 결과를 한 줄도 남기지 못한 채 조용히 끝난다.
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def _agy_names() -> List[str]:
    if os.name == "nt":
        return ["agy.exe", "agy.cmd", "agy.bat"]
    return ["agy"]


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:                    # Windows 에서 드라이브가 다르면
        return False


def _which_agy(root: Optional[str]) -> Optional[str]:
    """PATH 항목을 **직접** 돌며 agy 의 절대 경로를 찾는다.

    ⛔ [26.09.14 Eng 교차리뷰] 종전에는 bare `"agy"` 를 그대로 실행했다. Windows 의
      CreateProcess 는 PATH 보다 **현재 폴더**를 먼저 찾으므로, 리뷰 대상 저장소
      루트에 `agy.exe` 가 있으면 그것이 실행된다 — 상대 경로 후보를 건너뛰어 막은
      공격이 옆문으로 들어온다. `shutil.which` 도 기본값에서는 현재 폴더를 먼저 본다.
    → 빈 항목 · 상대 경로 항목(= 현재 폴더 기준)은 건너뛰고, 현재 폴더 자체와
      **리뷰 대상 저장소 안**의 항목(`node_modules/.bin` 등)도 건너뛴다.
    """
    cwd = os.path.realpath(os.getcwd())
    root_real = os.path.realpath(root) if root else None
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        real = os.path.realpath(entry)
        if real == cwd or (root_real and _is_within(real, root_real)):
            continue
        for name in _agy_names():
            cand = os.path.join(entry, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return os.path.abspath(cand)
    return None


def _find_agy(root: Optional[str] = None) -> Optional[str]:
    """agy 의 **절대 경로**. 알려진 설치 경로 → PATH 순. 못 찾으면 None.

    ⚠ 실행해 보지 않는다(종전 `agy --help`). 경로를 절대 경로로 확정하는 것이
      안전의 핵심이고, 실행 확인은 실제 호출이 한다.
    """
    for cand in _AGY_CANDIDATES:
        # ⚠ 상대 경로는 건너뛴다. 리눅스·맥에는 LOCALAPPDATA 가 없어 첫 후보가
        #   `agy/bin/agy.exe` 라는 **작업 디렉토리 기준 상대 경로**로 평가된다
        #   (실측). 리뷰는 대상 저장소 안에서 도므로, 그 경로를 품은 저장소를
        #   clone 해 리뷰하면 저장소가 심은 파일이 agy 대신 실행된다.
        if not cand or not os.path.isabs(cand):
            continue
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return _which_agy(root)


# [26.09.14] diff 를 **사용자 git 설정과 무관하게** 같은 모양으로 만든다.
#   `-c` 설정은 오래된 git 도 모르는 키를 무시하므로, 새 옵션 대신 설정으로 고정한다.
# ⛔ `diff.renames=false` 는 **보안 설정**이다 [26.09.14 실측]. git 기본값은 이름
#   변경을 감지해 `--name-only` 에 **새 이름만** 내보낸다. `.env` 를 `env.txt` 로
#   옮기고 한 줄을 고치자 목록이 `env.txt` 하나뿐이라 민감 경로 판정을 통과했고,
#   바뀐 줄(`API_KEY=sk_live_…`)이 담긴 diff 가 전송될 뻔했다. 끄면 옛 경로 `.env`
#   가 목록에 나와 차단된다.
_DIFF_CONFIG = [
    "-c", "diff.renames=false",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
    "-c", "diff.relative=false",
    "-c", "diff.algorithm=myers",
    "-c", "diff.context=3",
    "-c", "color.diff=false",
]
_DIFF_ARGS = ["--no-ext-diff", "--no-textconv", "--no-color"]


def _git(args: List[str], cwd: str, config: Optional[List[str]] = None,
         env: Optional[dict] = None, stdin: Optional[bytes] = None) -> str:
    """⚠ `core.quotePath=false` 는 **보안 옵션**이다 [26.08.13].

    기본값(true)이면 git 이 비ASCII 경로를 `"\\354\\232\\264\\354\\230\\201.env"`
    처럼 큰따옴표 + 8진 이스케이프로 내보낸다. 그러면 민감 판정의 basename·
    확장자 앵커가 **전부** 빗나가 `운영.env` 가 통과한다 — 실측에서 AWS 키가
    담긴 diff 가 `민감: 없음` 배너와 함께 전송 직전까지 갔다.
    오탐과 달리 fail-open 이라 사후에도 드러나지 않는다. 한국어 파일명만의
    문제가 아니다 — 경로 어디든 비ASCII 바이트 하나면 발동한다.
    """
    try:
        out = subprocess.run(["git", "-c", "core.quotePath=false"]
                             + (config or []) + args,
                             cwd=cwd, capture_output=True, input=stdin,
                             timeout=60, check=False, env=env)
    except FileNotFoundError:
        # ⛔ [26.09.14 실측] 종전에는 traceback · exit 1 이었다. exit 1 은 문서상
        #   "파싱 실패" 라 원인을 오해한다.
        raise RuntimeError("git 을 실행할 수 없다(PATH 에 git 이 있는지 확인). "
                           "리뷰는 수행되지 않았다.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("git %s 이 60초 안에 끝나지 않았다. 리뷰는 수행되지 않았다."
                           % " ".join(args))
    except OSError as exc:
        raise RuntimeError("git 실행 실패: %s. 리뷰는 수행되지 않았다." % exc)
    if out.returncode != 0:
        raise RuntimeError("git %s 실패: %s"
                           % (" ".join(args),
                              out.stderr.decode("utf-8", "replace")[:200]))
    return out.stdout.decode("utf-8", "replace")


def _git_root(start: str) -> str:
    """전역 스크립트이므로 **호출된 위치**의 저장소 루트를 쓴다."""
    return _git(["rev-parse", "--show-toplevel"], start).strip() or start


def _collect_diff(root: str, base: str, head: str, staged: bool,
                  two_dot: bool = False) -> Tuple[str, List[str]]:
    if staged:
        diff = _git(["diff", "--cached"] + _DIFF_ARGS, root, _DIFF_CONFIG)
        files = [f for f in _git(["diff", "--cached", "--name-only"] + _DIFF_ARGS,
                                 root, _DIFF_CONFIG).splitlines() if f]
    else:
        # ⚠ [26.08.13] 기본을 **3-dot(merge-base 기준)** 으로 바꿨다.
        # 2-dot 은 base 가 head 의 조상이 아닐 때 — 즉 브랜치 리뷰
        # (`--base main`)에서 — base 에만 있는 남의 커밋을 **삭제로 뒤집어**
        # diff 에 넣는다 (실측: `git diff master..HEAD` 가 동료 파일을
        # `deleted file mode` 로 표시). 리뷰어는 존재하지 않는 삭제를 보고
        # 유령 지적을 만들고, 진짜 변경분은 그 노이즈에 묻힌다.
        # 선형 이력(`HEAD~1`·`HEAD~3`)에서는 merge-base 가 base 자신이라
        # 결과가 2-dot 과 **동일** 하다 — 기존 사용법은 바뀌지 않는다.
        # 원 저장소가 main 단일 브랜치라 이 결함이 드러난 적이 없었다.
        rng = "%s%s%s" % (base, ".." if two_dot else "...", head)
        diff = _git(["diff", rng] + _DIFF_ARGS, root, _DIFF_CONFIG)
        files = [f for f in _git(["diff", rng, "--name-only"] + _DIFF_ARGS,
                                 root, _DIFF_CONFIG).splitlines() if f]
    return diff, files


_SCHEMA = {
    "type": "object",
    "required": ["verdict", "summary", "findings"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["approve", "approve_with_comments", "request_changes"],
        },
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "title", "detail"],
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "failure_scenario": {
                        "type": "string",
                        "description": "구체적 입력/상태 → 잘못된 결과",
                    },
                },
            },
        },
        "questions": {"type": "array", "items": {"type": "string"}},
    },
}

_GENERIC_LENSES = """## 먼저 볼 각도 (범용)

1. **배선 단절(silent-dead)**: 새 값을 만들거나 설정을 추가했는데 **소비하는
   쪽까지 실제로 이어지지 않는** 코드. 문서·로그·config·롤백 스위치만 갖추고
   실행 계층에 도달하지 않는 경우가 가장 흔하다.
2. **단위·규약 불일치**: 같은 필드를 생산자·검증부·소비자가 다르게 해석
   (%/fraction, 초/밀리초, 0-based/1-based, UTC/로컬).
3. **모집단이 다른 값의 비교·뺄셈**: 서로 다른 조건으로 뽑은 두 집계를 빼서
   비율·차이를 만드는 코드.
4. **부분/전량 비대칭**: 부분 처리(부분체결·분할·페이지네이션)가 있는데
   수량·가중치를 무시하고 건수로만 집계.
5. **문자열 매칭이 실제와 어긋남**: 부분 문자열 포함·느슨한 유사도로 서로 다른
   대상을 같다고 판정.
6. **가드 우회 경로**: 기존 검증을 거치지 않는 새 진입점이 생겼는지.
7. **경계·예외**: 빈 입력, 0/None, 동시 실행, 재시도 시 중복 처리."""

_RULES = """## 규칙

- **확신 없는 지적은 하지 마라.** 근거 없는 추측보다 침묵이 낫다.
- 각 지적에 `failure_scenario`(구체적 입력/상태 → 잘못된 결과)를 반드시 써라.
  그것을 쓸 수 없으면 그 지적은 빼라.
- 스타일·포매팅·네이밍 취향은 **지적하지 마라**. 동작·안전·정확성만 본다.
- 한국어로 답하라.

지정된 JSON 스키마로만 출력하라."""


# `git commit` 이 무엇을 담는지 가르는 형태. 커밋 게이트와 `--paths` 리뷰가 같은 이름을 쓴다.
#   index   — 스테이징 그대로 (`git commit`)
#   all     — 추적 파일의 변경을 모두 더한다 (`-a`)
#   include — 지정 경로의 추적 파일 변경을 스테이징에 더한다 (`-i 경로`)
#   only    — HEAD 에 지정 경로의 작업 트리 내용만 얹는다 (`-- 경로` · `경로` · `-o 경로`)
_COMMIT_MODES = ("index", "all", "include", "only")


def _commit_diff(root: str, mode: str = "index", pathspec=(), pathspec_cwd: Optional[str] = None,
                 adds=()) -> Tuple[str, List[str]]:
    """`git commit` 이 **실제로 담을** 변경의 diff. 반환 (diff, 파일 목록). [1.7.0]

    `adds` 는 같은 명령에서 커밋보다 먼저 실행될 `git add` 들이다 — `[(실행 폴더, 인자 목록)]`.

    ⛔ **진짜 인덱스는 건드리지 않는다.** 인덱스를 임시 사본으로 옮겨 그 위에서 모사한다. 두 세션이
      작업 트리를 공유할 때 남의 스테이징을 바꾸면 안 된다 — 경로 지정 커밋을 쓰는 이유가 그것이다.
    ⚠ git 의 커밋 구현(builtin/commit.c `prepare_index`)을 따른다. `only` 는 HEAD 트리로 새 인덱스를
      만들고, **git 이 아는 파일**(인덱스 항목과 스테이징된 삭제) 가운데 경로에 맞는 것만 작업 트리
      내용으로 갱신한다. 추적하지 않는 새 파일은 담지 않는다(그 커밋은 git 이 거부한다).
      실제로 커밋한 뒤의 `git diff HEAD~1..HEAD` 와 바이트까지 같음을 검사가 고정한다.
    ⚠ 스테이징 그대로이고 앞선 add 가 없으면 `--staged` 와 **같은 함수**를 부른다 — 해시가 갈리지 않는다.
    """
    if mode not in _COMMIT_MODES:
        raise RuntimeError("알 수 없는 커밋 형태: %s" % mode)
    if mode == "index" and not adds:
        return _collect_diff(root, None, None, True)
    where = pathspec_cwd or root
    tmp = tempfile.mkdtemp(prefix="gemini_review_idx_")
    try:
        real = _git(["rev-parse", "--git-path", "index"], root).strip()
        real = real if os.path.isabs(real) else os.path.join(root, real)
        work = os.path.join(tmp, "index")
        if os.path.isfile(real):
            shutil.copyfile(real, work)
        env = dict(os.environ, GIT_INDEX_FILE=work)
        for add_cwd, add_args in adds:
            _git(["add"] + list(add_args), add_cwd, env=env)
        if mode == "all":
            _git(["add", "-u"], root, env=env)
        elif mode == "include":
            _git(["add", "-u", "--"] + list(pathspec), where, env=env)
        elif mode == "only":
            known = set(_git(["ls-files", "--full-name", "-z", "--"] + list(pathspec),
                             where, env=env).split("\0"))
            known |= set(_git(["diff", "--cached", "--name-only", "-z", "--diff-filter=D", "--"]
                              + list(pathspec), where, _DIFF_CONFIG, env=env).split("\0"))
            known.discard("")
            only = os.path.join(tmp, "only-index")
            env = dict(os.environ, GIT_INDEX_FILE=only)
            try:
                _git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], root)
                _git(["read-tree", "HEAD"], root, env=env)
            except RuntimeError:
                _git(["read-tree", "--empty"], root, env=env)   # 첫 커밋 전
            if known:
                # ⛔ [26.09.17 교차 리뷰 HIGH · 실측 재현] 경로를 명령줄로 펼치면 `git commit -- 폴더` 가
                #   인자 길이 상한에 걸린다(Windows 는 32,767자 — 수백 파일이면 넘는다). 표준 입력으로 준다.
                _git(["update-index", "--add", "--remove", "-z", "--stdin"], root, env=env,
                     stdin=b"".join(p.encode("utf-8") + b"\0" for p in sorted(known)))
        diff = _git(["diff", "--cached"] + _DIFF_ARGS, root, _DIFF_CONFIG, env=env)
        files = [f for f in _git(["diff", "--cached", "--name-only"] + _DIFF_ARGS,
                                 root, _DIFF_CONFIG, env=env).splitlines() if f]
        return diff, files
    finally:
        shutil.rmtree(tmp, True)


def _load_project_context(root: str) -> str:
    """프로젝트 맥락은 **본문이 아니라 파일 경로**로 넘긴다.

    ⚠ [26.08.12] 프롬프트가 길어지면 agy 가 **빈 응답**을 낸다 — 진단 결과
    같은 diff·같은 모델이라도 프롬프트를 짧게 하면 정상 응답하고, 컨텍스트
    본문(1,900자)까지 인라인하면 thinking 은 8천~3만 토큰 돌면서 최종 출력만
    비어서 온다. diff 를 파일로 넘기는 것과 같은 이유로, 맥락도 경로만 준다.
    """
    path = os.path.join(root, _CONTEXT_FILE)
    if not os.path.isfile(path):
        return ""
    try:
        if not os.path.getsize(path):
            return ""
    except OSError:
        return ""
    return path


_NO_COMMANDS = ("셸 명령(테스트 · git · python 실행 등)은 시도하지 마라 — 헤드리스라 권한을 물을 수 "
                "없어 거부되고, 그러면 응답 전체가 사라진다. 동작은 코드를 읽어 추론하라.")


def _build_prompt(diff_path: str, files: List[str], ctx_path: str) -> str:
    """리뷰 지시. **짧게 유지한다** — 길면 agy 가 빈 응답을 낸다(위 주석 참조).

    큰 내용(diff·프로젝트 맥락)은 전부 **파일 경로**로 넘겨 모델이 읽게 한다.
    """
    parts = [
        "너는 숙련된 코드 리뷰어다. 아래 diff 를 비판적으로 검토하라.",
        "",
        "## 읽을 파일",
        "1. `%s` — 리뷰 대상 diff 전문 (반드시 읽어라)" % diff_path,
    ]
    if ctx_path:
        parts.append(
            "2. `%s` — 이 저장소의 맥락과 **반복된 실패 계열**. 반드시 읽고 "
            "그 관점을 최우선으로 적용하라." % ctx_path)
    parts += [
        "변경 파일 %d개: %s" % (len(files), ", ".join(files[:15])),
        "필요하면 저장소의 다른 파일도 읽어 맥락을 확인하라 — 지적과 관련된 파일만 (읽기 전용 모드다).",
        # ⛔ [26.09.14 실측] 리뷰어가 테스트를 돌려 보려다 헤드리스 agy 가 명령 권한을 자동
        #   거부했고, 그러면 **응답 전체가 빈 채로** 끝났다(exit 4 네 번 연속). 넓게 읽다가
        #   `--timeout` 에 걸린 회차도 있었다.
        _NO_COMMANDS,
        "",
        _GENERIC_LENSES,
        "",
        _RULES,
    ]
    return "\n".join(parts)


class _Interrupted(BaseException):
    """신호로 중단됐다.

    ⚠ `SystemExit` 을 쓰지 않는 이유: argparse 의 `--help` · 인자 오류도
      `SystemExit` 이라, 그것까지 "중단"으로 오기록한다(26.09.14 Eng 리뷰).
      `BaseException` 이라 코드 곳곳의 `except Exception` 에 삼켜지지 않는다.
    """

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


# SIGINT 는 파이썬이 이미 KeyboardInterrupt 로 올려 준다 — 따로 설치하지 않는다.
_HANDLED_SIGNALS = ("SIGTERM", "SIGHUP")


def _install_signal_handlers():
    """SIGTERM · SIGHUP 을 `_Interrupted` 로 바꾼다. 반환: `(복원 함수, 이후 신호 무시 함수)`.

    ⛔ [26.09.14 실측] 처리기가 없으면 SIGTERM 에 **atexit 가 돌지 않는다**
      (exit 143). Claude Code 에서 사용자가 작업을 멈추면(TaskStop) 오는 신호가
      SIGTERM 이었고, 그 결과 `changes.diff`(저장소 코드 전문)가 담긴 임시 폴더가
      남고 `--out` 은 `in_progress` 로 굳었다.
    ⚠ 예외로 올리면 `subprocess.run` 이 대기 중이던 agy 자식을 `kill()` 한다
      (CPython `run` 의 `except:` 절). 손자 프로세스까지는 못 죽인다(범위 밖 —
      agy 는 실측상 하위 프로세스를 띄우지 않았다).
    ⚠ 메인 스레드가 아니면 설치하지 않는다(`signal.signal` 이 ValueError).
      Windows 에는 SIGHUP 이 없고, 강제 종료(TerminateProcess)는 잡을 수 없다.
    ⚠ 첫 신호 뒤 정리하는 동안 오는 신호는 무시한다 — 정리 도중 다시 끊기면
      임시 폴더가 남는다. 정리는 `main()` 의 중단 갈래에서 **즉시** 하고(atexit 를
      기다리지 않는다), 끝나면 **항상** 원래 처리기로 되돌린다.
    ⛔ [26.09.14 Gemini 교차리뷰 HIGH] 종전에는 신호가 한 번 오면 복원하지 않았다.
      `main()` 을 같은 프로세스에서 부르는 호스트(테스트 러너 등)는 그 뒤 SIGTERM 을
      **영구히 무시**하게 된다.
    """
    if threading.current_thread() is not threading.main_thread():
        return (lambda: None), (lambda: None)
    state = {"fired": False}
    saved = []

    def handler(signum, frame):
        if state["fired"]:
            return
        state["fired"] = True
        raise _Interrupted(signum)

    for name in _HANDLED_SIGNALS:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            saved.append((sig, signal.signal(sig, handler)))
        except (OSError, ValueError, RuntimeError):
            pass

    def restore():
        # ⚠ 테스트가 main() 을 같은 프로세스에서 여러 번 부르면 처리기가 쌓이고
        #   테스트 러너의 처리기를 덮는다 — 끝나면 원래대로 되돌린다.
        for sig, old in saved:
            # ⚠ [26.09.14 Gemini 교차리뷰 MEDIUM] 이전 처리기가 파이썬 밖(C 확장 등)에서
            #   설치됐으면 `signal.signal` 이 None 을 돌려준다. None 으로 복원하면
            #   TypeError 라 원래 종료 코드를 잃고 traceback 으로 죽는다 → 건너뛴다.
            if old is None:
                continue
            try:
                signal.signal(sig, old)
            except (OSError, ValueError, RuntimeError, TypeError):
                pass

    def suppress():
        # ⚠ [26.09.14 Gemini 교차리뷰 MEDIUM] SIGINT 는 이 처리기를 거치지 않고
        #   KeyboardInterrupt 로 오므로 `fired` 가 켜지지 않는다. Ctrl+C 로 정리하는
        #   도중 SIGTERM 이 오면 `_Interrupted` 가 except 블록 안에서 새로 터져
        #   정리 · 기록이 끊겼다 → 중단 갈래에 들어오면 먼저 이것을 부른다.
        state["fired"] = True

    return restore, suppress


def _record_interrupt(out: Optional[str], signum: int, model: Optional[str] = None) -> int:
    """중단을 화면과 `--out` 에 남기고 종료 코드(128+신호 번호)를 돌려준다.

    `model` 은 인자 해석을 마쳤을 때만 안다 — 알면 다른 최종 결과처럼 최상위 · `_meta` 에 남긴다.
    """
    try:
        # ⚠ `signal.Signals` 는 Python 3.5+ 다(공식 문서 "Added in version 3.5").
        #   [26.09.14 교차리뷰가 "3.8+ 라 3.7 에서 AttributeError" 라고 짚었으나 사실이 아니다]
        name = signal.Signals(signum).name
    except ValueError:
        name = "signal %d" % signum
    rc = 128 + int(signum)
    _safe_print("")
    _safe_print("⛔ 중단됐다(%s) — 리뷰가 수행되지 않았다. 임시 파일은 정리한다." % name)
    _safe_print("   이 결과를 '지적 없음'으로 읽지 말 것.")
    if out:
        body = {"signal": name,
                "note": "신호로 중단됐다 — 리뷰가 수행되지 않았다. 통과가 아니다."}
        if model:
            body["model"] = model
        _write_out(out, _result("interrupted", body, rc, signal=name, model=model), quiet=True)
    return rc


def main(argv=None) -> int:
    """진입점. 본문(`_main`)을 신호 처리 · 내부 오류 기록으로 감싼다(26.09.14 S1 · 1.4.0)."""
    restore, suppress = _install_signal_handlers()
    # `calls` · `diff_sha256` 은 `_main` 이 채운다 — `internal_error` 는 `finish` 를 거치지
    #   않으므로(아래 except), 여기에 없으면 **토큰을 태운 회차가 집계에서 통째로 빠진다**
    #   (Eng H4 · H1). `calls` 는 같은 리스트 객체를 공유해 추가가 그대로 보인다.
    ctx = {"out": None, "tmpdir": None, "model": None, "calls": [], "diff_sha256": None}
    try:
        return _main(argv, ctx)
    except (_Interrupted, KeyboardInterrupt) as exc:
        suppress()
        # 정리는 **지금** 한다 — 처리기를 되돌린 뒤 atexit 까지 기다리면 그 사이의
        #   두 번째 신호가 정리를 끊는다(atexit 등록은 남겨 두어도 두 번 지워 무해).
        if ctx["tmpdir"]:
            shutil.rmtree(ctx["tmpdir"], True)
        return _record_interrupt(ctx["out"],
                                 getattr(exc, "signum", signal.SIGINT), ctx["model"])
    except Exception as exc:
        # ⛔ [26.09.14 Eng 교차리뷰 → 1.4.0] 예기치 못한 예외는 traceback · exit 1 로 끝났고,
        #   `--out` 은 `in_progress` 로 남았다. exit 1 은 "파싱 실패" 와 겹쳐 원인을 가린다 →
        #   `internal_error` 로 **기록**하고 원 예외는 그대로 보여 준다.
        traceback.print_exc()
        if ctx["tmpdir"]:
            shutil.rmtree(ctx["tmpdir"], True)
        _safe_print("⛔ 예기치 못한 오류로 끝났다(%s) — 리뷰가 수행되지 않았다. 통과가 아니다."
                    % exc.__class__.__name__)
        _safe_print("   위 traceback 을 그대로 보고할 것(스크립트 결함이다).")
        # `--out` 이 없으면 다른 종료 경로처럼 기본 결과 폴더에 남긴다. 기록 자체가 또 실패해도
        #   원 예외의 종료 코드를 지킨다.
        try:
            body = {"error": "%s: %s" % (exc.__class__.__name__, str(exc)[:300]),
                    "note": "스크립트 내부 오류로 끝났다 — 리뷰가 수행되지 않았다. 통과가 아니다."}
            if ctx["model"]:
                body["model"] = ctx["model"]
            _write_out(ctx["out"], _result("internal_error", body, EXIT_PARSE_FAILED,
                                           model=ctx["model"], calls=ctx["calls"] or None,
                                           diff_sha256=ctx["diff_sha256"]),
                       quiet=bool(ctx["out"]))
        except Exception:
            pass
        return EXIT_PARSE_FAILED
    finally:
        restore()


def _main(argv, ctx: dict) -> int:
    started = time.time()
    # ⚠ argparse 보다 **먼저** 불러야 한다 [26.08.13 재점검]. `--help` 와
    # argparse 의 에러 메시지는 `parse_args` **안에서** 출력되므로, 그 뒤에
    # 두면 한글·em dash 가 cp949 로 나가 UnicodeEncodeError 로 죽는다
    # (실측: `--help` → exit 1). help 문자열에서 em dash 를 뺀 것과 함께
    # 이중 방어다.
    _ensure_utf8_stdout()

    # ⛔ **argparse 보다 먼저 `--out` 을 무효화한다** [26.09.10 2회차 리뷰 high →
    #   26.09.14 Eng 교차리뷰 P1 로 위치 이동]. 종전에는 이 무효화가 `parse_args`
    #   **뒤**에 있어, 인자 오류(exit 2) · `--help` 에서 파일이 손대지 않은 채
    #   남았다(재현: 어제의 approve 를 넣어 두고 `--bogus` → exit 2 · 파일 approve).
    #   ⛔ 무효화 자체가 실패하면(쓸 수 없는 경로) **리뷰를 시작하지 않는다.**
    #     종전에는 조용히 넘어가 최종 기록도 실패했고, 재현에서 **exit 5 인데 파일은
    #     어제 approve** 였다 — 종료 코드와 파일이 다른 이야기를 한다.
    #   ⚠ 갈래마다 `_write_out` 을 더하는 대신 여기서 한 번 무효화한다. 종료 경로가
    #     늘어도 자동으로 덮이고, 중간에 죽어도 낡은 값이 남지 않는다.
    out_early = _peek_out(argv)
    ctx["out"] = out_early
    if out_early and _write_out(out_early, _result("in_progress", {
            "note": "리뷰가 시작됐고 아직 끝나지 않았다. 이 파일이 이 상태로 남아 "
                    "있으면 리뷰가 중간에 죽은 것이다 — 통과가 아니다.",
            "started_at": datetime.now().isoformat(timespec="seconds"),
    }, None), quiet=True) is None:
        _safe_print("⛔ --out 경로에 쓸 수 없다: %s" % out_early)
        _safe_print("   리뷰를 시작하지 않는다(외부 전송 없음). 경로 · 권한을 확인할 것.")
        return EXIT_TOOL_ERROR

    ap = _build_parser()
    try:
        args = ap.parse_args(argv)
        # ⛔ [1.5.0 A5] 충돌 검사는 `parse_args` 를 감싼 try **안에서** `ap.error()` 로 낸다.
        #   밖에서 내면 `--out` 파일에 `in_progress`("중간에 죽었다") 가 남는다.
        #   ⚠ "사용자가 명시했는가" 는 파싱된 값이 아니라 **argv 토큰**으로 판정한다 —
        #     `--base` 는 기본값이 `HEAD~1` 이라 값 비교로는 가릴 수 없다. 약어도 잡는다
        #     (`--stats --stag` 가 새면 안 된다, Eng M6).
        if args.stats:
            clash = _named_in_argv(argv, _STATS_CONFLICTS)
            if clash:
                ap.error(_STATS_CONFLICT_MSG % ", ".join(clash))
        if args.paths:
            clash = _named_in_argv(argv, _PATHS_CONFLICTS)
            if clash:
                ap.error(_PATHS_CONFLICT_MSG % ", ".join(clash))
        if args.record_fallback:
            if not (args.staged or args.paths):
                ap.error(_FALLBACK_SCOPE_MSG)
            if not ((args.reviewer or "").strip() and (args.summary or "").strip()):
                ap.error(_FALLBACK_FIELDS_MSG)
            clash = _named_in_argv(argv, _FALLBACK_CONFLICTS)
            if clash:
                ap.error(_FALLBACK_CONFLICT_MSG % ", ".join(clash))
        elif args.reviewer is not None or args.summary is not None:
            ap.error(_FALLBACK_ONLY_MSG)
    except SystemExit as exc:
        # ⚠ [26.09.14 Gemini 교차리뷰 HIGH] `--help` · 인자 오류로 여기서 끝나면 방금
        #   쓴 `in_progress`("중간에 죽었다") 가 사실과 다르게 남는다. 리뷰를 시작하지도
        #   않았다는 것을 그대로 적는다 — 여전히 통과가 아니다.
        if out_early:
            _write_out(out_early, _result("not_run", {
                "note": "인자 해석 단계에서 끝났다(도움말 · 판 확인 · 인자 오류) — 리뷰가 "
                        "수행되지 않았다. 통과가 아니다.",
            }, exc.code), quiet=True)
        raise
    if args.stats:
        return _run_stats(args)
    _apply_deprecated_flags(args)
    # 중단 · 내부 오류 기록(`main`)도 판정 모델을 남기게 한다 — 인자 해석을 마친 뒤의 모든 결과에 있다.
    ctx["model"] = args.model

    calls: List[dict] = []                # agy 호출마다 원인 기록 → `_meta.calls`
    # ⛔ [1.5.0 Eng H1] 새 `_meta` 키는 **상태에 담아 `finish` 안에서 한 번** 대입한다.
    #   호출부마다 넘기면 언젠가 빠진다 — 최상위 `model` 이 같은 실수를 두 번 했고 직전
    #   커밋이 그 수정이었다.
    state = {"scope": None, "diff_sha256": None, "diff_bytes": None}
    ctx["calls"] = calls                  # 같은 객체 — internal_error 도 호출 기록을 잃지 않는다

    def finish(mode: str, body: dict, rc: Optional[int] = None, **meta) -> int:
        """최종 결과를 `--out` 에 남긴다. **명시한 `--out` 에 못 쓰면 exit 2.**

        종료 코드는 `rc` 를 주지 않으면 mode 표(`_MODE_EXIT`)에서 정한다 — 갈래마다 숫자를
        따로 적으면 표와 어긋난다.
        ⛔ [26.09.14] 종전에는 기록 실패를 무시해, 판정 코드와 파일 내용이 갈렸다.
          파일을 믿는 자동화에게는 종료 코드보다 파일이 먼저다.
        """
        if rc is None:
            rc = _MODE_EXIT[mode]
        meta.setdefault("model", args.model)
        # 호환(1.4.x): 1.3.x 결과 파일은 agy 단계의 성공 · 실패 경로 모두 최상위 `model` 을 담았다.
        #   ⛔ [1.4.0 --base main 교차리뷰 CRITICAL — 실측 확인] 처음엔 `reviewed` 경로에만 넣어,
        #   timeout · quota · text_fallback 등에서 빠졌다 → 여기서 대입한다. `finish` 를 거치지 않는
        #   중단 · 내부 오류는 `main` 이 `ctx["model"]` 로 같은 값을 남긴다(후속 교차리뷰 HIGH).
        body = dict(body)
        body["model"] = meta["model"]
        meta["scope"] = state["scope"]
        meta["calls"] = calls
        meta["elapsed_seconds"] = round(time.time() - started, 1)
        if state["diff_sha256"]:
            meta["diff_sha256"] = state["diff_sha256"]
            meta["diff_bytes"] = state["diff_bytes"]
        if _write_out(args.out, _result(mode, body, rc, **meta)) is None and args.out:
            _safe_print("⛔ --out 에 결과를 쓰지 못했다 — 종료코드 %d 대신 2 로 끝낸다."
                        % rc)
            return EXIT_TOOL_ERROR
        return rc

    if args.check:
        rc, body = _run_check(args, calls)
        return finish("check", body, rc)

    try:
        root = _git_root(os.getcwd())
        if args.paths:
            diff, files = _commit_diff(root, "only", args.paths, os.getcwd())
        else:
            diff, files = _collect_diff(root, args.base, args.head, args.staged,
                                        args.two_dot)
    except RuntimeError as exc:
        _safe_print(str(exc))
        if not args.staged and args.base == "HEAD~1":
            _safe_print("  첫 커밋만 있는 저장소라면 비교할 이전 커밋이 없다 — "
                        "`--staged` 또는 `--base <ref>` 로 범위를 지정할 것.")
        return finish("tool_error", {"note": str(exc)})
    state["scope"] = _resolve_scope(root, args)

    # ⚠ [26.09.14] 저장소 루트를 안 뒤에 찾는다 — PATH 에서 저장소 안 항목을 빼려면
    #   루트가 필요하다(`_which_agy`).
    # [1.7.0] 대체 리뷰 기록은 agy 를 부르지 않는다 — agy 가 망가진 날에도 기록할 수 있어야 한다.
    agy = None if args.record_fallback else _find_agy(root)
    if not agy and not args.record_fallback:
        _safe_print("agy(Antigravity CLI)를 찾지 못했다 — 리뷰가 수행되지 않았다.")
        _safe_print("  확인한 곳: 알려진 설치 경로 · PATH(현재 폴더와 저장소 안은 제외)")
        _safe_print("  설치: https://antigravity.google/cli "
                    "(Windows: irm https://antigravity.google/cli/install.ps1 | iex)")
        _safe_print("  설치 뒤 `agy` 를 한 번 실행해 Google 계정으로 로그인할 것.")
        return finish("tool_error",
                      {"note": "agy 를 찾지 못했다 — 리뷰가 수행되지 않았다."})
    # ⚠ [26.09.14 Gemini 교차리뷰 HIGH] 아래 조기 종료 갈래들도 **최종 상태**를 남긴다.
    #   종전에는 `in_progress`("중간에 죽었다") 가 그대로 남아 사실과 달랐다.
    if not diff.strip():
        # ⛔ [1.4.0 결정 8] `--staged` 인데 스테이징이 비면 **exit 8** 이다. 1.3.x 는 0 이라,
        #   `git add` 를 빠뜨린 커밋이 "리뷰 통과" 로 읽혔다(전역 규약이 "exit 0 도 통과가
        #   아니다" 는 **문서 경고**로 막고 있었다). 범위 리뷰(`--base`)의 빈 diff 는 0 그대로다.
        if args.paths and not args.allow_empty:
            _safe_print("⛔ 지정한 경로에 커밋할 변경이 없다 — 리뷰는 수행되지 않았다(exit %d)."
                        % EXIT_EMPTY_STAGED)
            _safe_print("   경로가 git 이 아는 파일인지(새 파일은 먼저 git add) 확인할 것.")
            return finish("no_changes", {
                "note": "지정한 경로에 커밋할 변경이 없었다 — 리뷰가 수행되지 않았다. 통과가 아니다."},
                EXIT_EMPTY_STAGED)
        if args.staged and not args.allow_empty:
            _safe_print("⛔ 스테이징된 변경이 없다 — 리뷰는 수행되지 않았다(exit %d)."
                        % EXIT_EMPTY_STAGED)
            _safe_print("   `git add` 로 리뷰할 변경을 스테이징한 뒤 다시 돌릴 것.")
            _safe_print("   변경이 없는 것이 의도라면 --allow-empty 로 0 을 받는다 — 그래도 통과는 아니다.")
            return finish("no_changes", {
                "note": "스테이징된 변경이 없었다 — 리뷰가 수행되지 않았다. 통과가 아니다."},
                EXIT_EMPTY_STAGED)
        _safe_print("변경분이 없다. 리뷰는 수행되지 않았다.")
        return finish("no_changes",
                      {"note": "리뷰할 변경분이 없었다 — 리뷰가 수행되지 않았다."}, EXIT_PASSED)

    # ⛔ [1.5.0 A2] 빈 diff 를 거른 **직후**, 민감 경로 판정보다 **먼저** 계산한다(Eng M3).
    #   `sensitive_blocked` 로 끝난 실행에도 "무엇을 보려 했는가" 가 남아야 집계가 구멍 나지
    #   않는다. 해시 대상은 아래에서 파일에 쓰는 바이트와 **같은** `_diff_bytes` 결과다.
    diff_bytes = _diff_bytes(diff)
    state["diff_sha256"] = ctx["diff_sha256"] = _sha256_hex(diff_bytes)
    # [1.5.0 A5] 크기도 남긴다. OQ6 분류에서 **회차 간 diff 크기 증가율**이 루프 종류를
    #   가르는 유일하게 확실한 손잡이였다 — 범위가 커지는 루프는 수정이 새 코드를 낳아
    #   스스로 연료를 만들고(H-B), 고정된 루프는 재현율이 제약이다(H-A, 실측 67%).
    state["diff_bytes"] = len(diff_bytes)
    if args.record_fallback:
        return _record_fallback(args, root, files, state, finish)

    classified = [(f, _classify_path(f)) for f in files]
    blocked = [f for f, c in classified if c == "block"]
    warned = [f for f, c in classified if c == "warn"]
    if blocked and not args.allow_sensitive:
        _safe_print("⛔ 민감 경로가 diff 에 포함돼 있다 — 외부 전송을 중단한다:")
        for f in blocked:
            _safe_print("   %s" % f)
        # ⛔ [26.09.14 DX 교차리뷰] 종전 문구 "의도한 것이면 --allow-sensitive 로 다시
        #   실행하라" 는 에이전트가 **지시로 읽고** 스스로 가드를 끌 수 있었다.
        _safe_print("   리뷰는 수행되지 않았다. 목록을 사용자에게 보여 주고, 사용자가")
        _safe_print("   전송을 명시적으로 승인한 경우에만 --allow-sensitive 로 다시 실행할 것.")
        return finish("sensitive_blocked", {
            "blocked": blocked,
            "note": "민감 경로가 있어 전송을 중단했다 — 리뷰가 수행되지 않았다."})

    project_ctx = _load_project_context(root)
    if args.staged:
        scope = "staged"
    elif args.paths:
        scope = "paths %s" % " ".join(args.paths)
    else:
        scope = "%s%s%s" % (args.base, ".." if args.two_dot else "...", args.head)
    if blocked:
        sensitive_label = "⚠ 강행 %d건 (--allow-sensitive)" % len(blocked)
    elif warned:
        sensitive_label = "경고 %d건" % len(warned)
    else:
        sensitive_label = "없음"
    _safe_print("=" * 74)
    _safe_print("Gemini 교차 리뷰 v%s (Antigravity CLI · %s)" % (__version__, args.model))
    _safe_print("스크립트: %s" % os.path.abspath(__file__))
    _safe_print("저장소: %s" % root)
    # ⚠ [26.09.10] `diff N자` 를 **프롬프트 길이로 읽지 말 것.** diff 는 임시
    #   파일 경로로 넘어가므로 프롬프트에는 실리지 않는다(`_build_prompt`).
    #   실측에서 이 수치를 프롬프트 크기로 읽고 빈 응답의 원인을 두 번 잘못
    #   짚었다("크기 때문" → 실제로는 다섯 단어 프롬프트도 무응답이었다).
    _safe_print("범위: %s / 변경 파일 %d개 / diff %d자 (파일로 전달 — 프롬프트에 싣지 않는다)"
                % (scope, len(files), len(diff)))
    _safe_print("맥락: %s" % (_CONTEXT_FILE if project_ctx else "범용 (프로젝트 파일 없음)"))
    _safe_print("민감: %s" % sensitive_label)
    _safe_print("모드: plan (read-only — Gemini 는 파일을 수정할 수 없다)")
    _safe_print("판정: %s 만 낸다 (진단 모델은 빈 응답일 때 생존 확인에만 쓴다)" % args.model)
    _safe_print("=" * 74)
    # ⚠ [26.08.13] 강행·경고는 **반드시 화면에 남긴다.**
    # 종전 `--allow-sensitive` 는 목록 출력 자체를 지워, 그 플래그를 상시로
    # 붙인 뒤에는 어느 리뷰가 무엇을 내보냈는지 사후 재구성이 불가능했다
    # (실측: `.env` 의 `SECRET=hunter2` 가 전송됐는데 콘솔에 `.env` 도 `민감`
    # 도 한 번 안 나왔다). 강행은 허용하되 **조용해서는 안 된다.**
    for f in blocked:
        _safe_print("   ⚠ 외부로 전송됨: %s" % f)
    for f in warned:
        _safe_print("   ℹ 예시/템플릿으로 보여 통과: %s (내용 확인 권장)" % f)
    if blocked or warned:
        _safe_print("")

    # ⛔ [1.5.0 A4] **쿼터 단락은 여기다** — 민감 경로 판정과 `_find_agy` 뒤, tmpdir 생성 앞.
    #   · 민감 경로가 먼저여야 캐시가 있어도 exit 3 이 난다.
    #   · agy 미설치는 여전히 exit 2 다(처방이 뒤집히면 안 된다).
    #   · tmpdir 앞이어야 **보내지 않을 diff 를 디스크에 쓰지 않는다**(잔류 사고 2회 이력).
    if not args.ignore_quota_cache:
        state_path = _quota_guard(_quota_state_path)
        models = _quota_guard(_load_quota_state, state_path) or {}
        hit = _quota_guard(_quota_short_circuit, agy, args.model, root,
                           state_path, models, calls)
        if hit:
            reset, entry, source, buckets = hit
            streak = int(entry.get("cached_hits") or 0)
            since = _parse_utc(entry.get("blocked_since"))
            _say_not_reviewed(
                "구독 사용량 한도에 걸려 있다(%s) — agy 를 부르지 않았다" % args.model,
                "[%s 기록 당시 원문] %s" % (entry.get("recorded_at") or "?",
                                        entry.get("detail") or "쿼터 한도"),
                "%s 이 지난 뒤 **같은 모델로** 다시 돌릴 것. 모델을 낮추지 않는다."
                % _fmt_until(reset))
            # ⛔ [DX critical] 단락은 609초를 0.1초로 만든다 — 사람이 **게이트가 없다는 사실
            #   자체를** 못 보게 된다. 누적을 화면에 적어 그 부재를 보이게 한다.
            if streak >= _QUOTA_STREAK_WARN:
                hours = int((_utc_now() - since).total_seconds() // 3600) if since else None
                _safe_print("   ⚠ 이 기기에서 게이트 없이 %d회째다%s."
                            % (streak, " · 멈춘 지 %d시간" % hours if hours else ""))
            return finish("quota_exhausted", {
                "error": entry.get("detail") or "쿼터 한도",
                "retry_after": _fmt_hms(int((reset - _utc_now()).total_seconds())) or None,
                "note": "agy 구독 사용량 한도로 리뷰가 수행되지 않았다. 통과가 아니다."},
                quota_cached=True, quota_source=source,
                quota_cached_streak=streak,
                quota_buckets=[b.get("id") for b in buckets] or None)

    # ⛔ **정리를 등록해 두고 만든다** — `mkdtemp` 만 하면 지워지지 않는다.
    #   이 결함은 **두 번째**다. 처음엔 `/tmp` 에 50개 1.5MB 가 쌓였고 그 안에
    #   diff 전문이 평문으로 남아 있어 고쳤는데(그 주석이 플러그인 v1.2.0 에
    #   그대로 있다), 그 뒤 Windows 쪽 편집에서 `atexit`·`shutil` import 와
    #   함께 **통째로 사라졌다.**
    #   ⚠ 재발 규모가 훨씬 컸다 [26.09.11 실측]: `%TEMP%` 에 **2,875개 ·
    #     24MB**(09-07~09-11). 테스트가 `main()` 을 반복 호출하므로 스위트를
    #     한 번 돌릴 때마다 수십 개씩 늘어난다 — 사람이 리뷰를 돌린 횟수와
    #     무관하게 증폭된다.
    #   ⚠ 담기는 것이 `changes.diff`(저장소 코드 전문)라 **용량보다 내용이
    #     문제**다. 비공개 저장소의 diff 가 평문으로 남는다.
    # → 가드: `tests/test_gemini_review.py::_check_tmpdir_removed_after_real_run`
    #   — 자식 프로세스로 `main()` 을 끝까지 돌리고, 종료 후 **그 diff 가 있던
    #   디렉터리**가 사라졌는지 본다(`atexit` 는 프로세스가 끝나야 돈다).
    #   ⚠ [26.09.14] 종전 주석은 이 저장소에 없는 `test_gemini_review_guards.py`
    #     를 가리켰고, 저장소 안의 가드는 `"atexit.register" in src` 문자열
    #     검사 하나뿐이었다.
    tmpdir = tempfile.mkdtemp(prefix="gemini_review_")
    atexit.register(shutil.rmtree, tmpdir, True)
    ctx["tmpdir"] = tmpdir
    diff_path = os.path.join(tmpdir, "changes.diff")
    schema_path = os.path.join(tmpdir, "schema.json")
    # ⛔ [1.5.0 A2] **바이너리로 쓴다.** 텍스트 모드는 Windows 에서 `\n` 을 `\r\n` 으로 바꿔
    #   같은 변경의 해시가 기기마다 달라진다. agy 는 이 파일을 경로로 읽으므로 줄바꿈이 LF 로
    #   통일돼도 리뷰 내용에는 영향이 없다.
    with open(diff_path, "wb") as f:
        f.write(diff_bytes)
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(_SCHEMA, f, ensure_ascii=False)

    prompt = _build_prompt(diff_path, files, project_ctx)
    model = args.model
    run = _invoke_schema(agy, model, args, root, schema_path, prompt)
    cause, kind, detail = _classify_run(run, structured=True)
    calls.append(_call_record("structured", model, run, cause, kind, detail))
    if run.rc is not None:
        _safe_print("응답: %s · %.0f초" % (model, run.elapsed))

    if cause in _FAILURE_CAUSES:
        mode, body, extra = _failure_result("구조화 호출", model, run, cause, kind, detail, args)
        if cause == "quota":
            # [1.5.0 A4] 차단을 기록한다. 이미 수백 초를 쓴 뒤라 `/quota` 6초는 무시할 만하고,
            #   그 대가로 **추정이 아닌** 해제 시각을 저장해 다음 실행이 0.1초로 끝난다.
            extra.update(_quota_guard(_record_quota_block, agy, model, root, detail, calls) or {})
        return finish(mode, body, **extra)
    if cause == "ok":
        # 성공은 해제 신호다. `timeout` · `tool_error` 로는 지우지 않는다 — 쿼터가 풀렸다는
        #   증거가 아니기 때문이다.
        _quota_guard(_clear_quota_block, model)
    if cause in ("empty", "tool_denied"):
        mode, body, extra = _recover_empty_response(
            agy, model, args, root, diff_path, files, project_ctx,
            run, cause, detail, calls)
        return finish(mode, body, **extra)

    payload = _extract_json(run.stdout)
    if payload is None:
        _safe_print("JSON 파싱 실패 — 원문을 그대로 출력한다:")
        _safe_print(run.stdout[:4000])
        return finish("parse_failed", {
            "note": "응답에서 리뷰 JSON 을 하나로 특정하지 못했다 — 통과가 아니다."})

    # ⛔ [26.09.14 CEO 스펙 리뷰 → 1.4.0 E18] **결과 계약 키는 코드만 쓴다.** 1.3.0 은
    #   `payload.setdefault("model", …)` 라 LLM 응답에 `model` 키가 있으면 그 값이 남았고,
    #   diff 속 프롬프트 주입으로도 "주 모델이 판정했다" · `_meta.passed: true` 를 심을 수
    #   있었다. 스키마에 있는 키만 옮기고 나머지는 버린다(버린 키 이름은 `_meta` 에 남긴다).
    review = dict((k, payload[k]) for k in _REVIEW_KEYS if k in payload)
    dropped = sorted(k for k in payload if k not in _REVIEW_KEYS)
    _render(review)
    rc = _exit_code_for(review)
    if rc:
        _safe_print("")
        _safe_print("   (종료코드 %d — 지적을 실측 검증한 뒤 반영하고 다시 돌릴 것)"
                    % rc)
    return finish("reviewed", review, rc, dropped_llm_keys=dropped or None)


# 이 원인이면 곧바로 끝낸다(재시도 없음). 빈 응답(`empty` · `tool_denied`)만 복구를 시도한다.
_FAILURE_CAUSES = ("tool_error", "model_unavailable", "quota", "timeout")


# [1.7.0] "Gemini 리뷰가 수행되지 않았다" 로 치는 mode — exit 4 의 원인들이다. 설치 · 인증 문제(2),
#   민감 경로(3), 텍스트 모드(6)는 고칠 것이 따로 있으므로 대체 리뷰의 근거가 되지 않는다.
_NOT_PERFORMED_MODES = frozenset((
    "timeout", "quota_exhausted", "primary_model_unavailable", "tool_unavailable",
    "review_unavailable"))
# 시도로 치지 않는 기록 — 가장 최근 **시도**를 찾을 때 건너뛴다.
_NOT_ATTEMPT_MODES = frozenset((
    "fallback_reviewed", "fallback_refused", "not_run", "in_progress", "check", "interrupted",
    "internal_error", "no_changes"))


def _latest_attempt(root: str, sha: str) -> Optional[dict]:
    """같은 저장소 · 같은 diff 해시로 **가장 최근에 Gemini 리뷰를 시도한** 결과의 `_meta`. 없으면 None.

    ⚠ 결과 폴더는 저장소들이 함께 쓰고, 같은 변경은 저장소가 달라도 해시가 같다 — 저장소를 함께 본다.
    """
    base, trust = _state_dir(create=False)
    if base is None or trust != "ok":
        return None
    try:
        names = [n for n in os.listdir(base)
                 if n.startswith("gemini_review_") and n.endswith(".json")]
    except OSError:
        return None
    ordered = []
    for n in names:
        path = os.path.join(base, n)
        try:
            ordered.append((os.path.getmtime(path), path))
        except OSError:
            continue
    ordered.sort(reverse=True)
    real_root = os.path.realpath(root)
    for _mtime, path in ordered:
        try:
            with io.open(path, encoding="utf-8") as fh:
                meta = json.load(fh).get("_meta")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(meta, dict) or meta.get("diff_sha256") != sha:
            continue
        scope = meta.get("scope") if isinstance(meta.get("scope"), dict) else {}
        if not (isinstance(scope.get("repo"), str)
                and os.path.realpath(scope["repo"]) == real_root):
            continue
        if meta.get("mode") in _NOT_ATTEMPT_MODES:
            continue
        return meta
    return None


def _record_fallback(args, root: str, files: List[str], state: dict, finish) -> int:
    """대체 리뷰를 **마쳤다는 기록**을 남긴다(`--record-fallback`, 1.7.0). 리뷰를 요청하지 않는다.

    ⛔ 같은 내용의 **가장 최근 Gemini 시도가 수행되지 않음(exit 4)** 일 때만 남긴다. Gemini 에게 묻지도
      않고 대체 리뷰로 건너뛰거나, Gemini 가 낸 지적(request_changes)을 대체 리뷰로 덮는 경로를 닫는다.
    ⚠ 기록은 주 모델 판정이 아니다 — `_meta.passed` 는 false 다. 커밋 게이트만 운영자가 막지 않았다면
      이 기록을 인정한다(`git config gemini-review.gateFallback false` 로 끈다).
    ⚠ 위협 모델은 망각이다. 대체 리뷰를 실제로 했는지는 코드가 확인할 수 없다 — 기록에 주체와 요약을
      남겨 사후에 추적할 수 있게 한다.
    """
    sha = state["diff_sha256"]
    attempt = _latest_attempt(root, sha)
    mode = attempt.get("mode") if attempt else None
    why = None
    if attempt is None:
        why = ("같은 내용으로 Gemini 리뷰를 시도한 기록이 없다",
               "먼저 같은 범위 인자로 리뷰를 돌린다. 수행되지 않으면(exit 4) 그때 기록한다.")
    elif mode == "reviewed" and attempt.get("passed") is True:
        why = ("같은 내용이 이미 Gemini 리뷰를 통과했다", "기록할 필요가 없다.")
    elif mode == "reviewed":
        why = ("같은 내용에 Gemini 가 판정을 냈다(exit %s) — 대체 리뷰로 덮을 수 없다"
               % attempt.get("exit_code"),
               "지적을 실측 검증해 반영하고 다시 리뷰한다.")
    elif mode not in _NOT_PERFORMED_MODES:
        why = ("가장 최근 시도가 '수행되지 않음(exit 4)' 이 아니다(%s · exit %s)"
               % (mode, attempt.get("exit_code")),
               "그 원인(설치 · 인증 · 민감 경로 · 텍스트 모드)을 먼저 해결한다.")
    if why is not None:
        _safe_print("⛔ 대체 리뷰를 기록하지 않았다(exit %d)." % EXIT_TOOL_ERROR)
        _safe_print("   원인: %s." % why[0])
        _safe_print("   해결: %s" % why[1])
        if args.out:
            _write_out(args.out, _result("fallback_refused", {
                "note": "대체 리뷰 기록을 거부했다: %s. 통과가 아니다." % why[0]},
                EXIT_TOOL_ERROR, scope=state["scope"], diff_sha256=sha,
                diff_bytes=state["diff_bytes"]))
        return EXIT_TOOL_ERROR

    _safe_print("=" * 74)
    _safe_print("대체 리뷰 기록 v%s (Gemini 리뷰가 수행되지 않았다)" % __version__)
    _safe_print("저장소: %s" % root)
    _safe_print("범위: %s / 변경 파일 %d개 / sha256 %s"
                % ("staged" if args.staged else "paths %s" % " ".join(args.paths),
                   len(files), sha[:12]))
    _safe_print("Gemini 시도: %s (exit %s · %s)"
                % (mode, attempt.get("exit_code"), attempt.get("written_at")))
    _safe_print("대체 리뷰: %s" % args.reviewer.strip())
    _safe_print("요약: %s" % args.summary.strip())
    _safe_print("⚠ 주 모델 판정이 아니다(_meta.passed false). 커밋 게이트는 운영자가 막지 않았다면 인정한다.")
    _safe_print("=" * 74)
    return finish("fallback_reviewed", {
        "reviewer": args.reviewer.strip(),
        "summary": args.summary.strip(),
        "gemini_attempt": {"mode": mode, "exit_code": attempt.get("exit_code"),
                           "written_at": attempt.get("written_at")},
        "note": "Gemini 리뷰가 수행되지 않아 운영 규칙의 대체 리뷰로 갈음한 기록이다. "
                "주 모델 판정이 아니다."}, EXIT_PASSED)


def _say_not_reviewed(problem: str, cause: str = "", fix: str = "") -> None:
    """비통과 경로 문구를 **문제 · 원인 · 해결 · 리뷰 수행 여부** 로 통일한다(1.4.0 DX-3)."""
    _safe_print("⛔ 리뷰가 수행되지 않았다 — %s" % problem)
    if cause:
        _safe_print("   원인: %s" % cause)
    if fix:
        _safe_print("   해결: %s" % fix)
    _safe_print("   이 결과를 '지적 없음'으로 읽지 말 것.")


def _say_note(problem: str, cause: str = "", fix: str = "") -> None:
    """리뷰는 계속되지만 **무언가를 잃었을 때** 쓰는 형제 헬퍼(1.5.0 DX).

    `_say_not_reviewed` 와 같은 세 부분(문제 · 원인 · 해결)을 쓰되, "리뷰가 수행되지 않았다"
    라고 말하지 않는다 — 판정은 정상적으로 난다.

    ⛔ [DX] `fix` 에는 **대가**를 적는다. "한 줄 알린다" 로만 규정하면 문구를 테스트로
      고정할 수 없고, 문구 없는 알림은 조용한 실패의 축소판이다. 예: "다음 실행이 같은
      609초 대기를 다시 쓴다", "이 실행은 --stats 집계에 잡히지 않는다".
    """
    _safe_print("⚠ %s" % problem)
    if cause:
        _safe_print("   원인: %s" % cause)
    if fix:
        _safe_print("   대가: %s" % fix)


def _failure_result(stage: str, model: str, run: "_AgyRun", cause: str, kind: str,
                    detail: str, args) -> Tuple[str, dict, dict]:
    """재시도하지 않고 끝내는 원인 → `(mode, 결과 본문, _meta 추가 필드)`. 화면 안내도 여기서 한다."""
    if cause == "quota":
        reset = _quota_reset_hint(detail)
        _say_not_reviewed(
            "%s: 구독 사용량 한도에 걸렸다(%s)" % (stage, model), detail,
            "재설정 시각%s이 지난 뒤 **같은 모델로** 다시 돌릴 것. 모델을 낮추지 않는다."
            % (" (%s 뒤)" % reset if reset else ""))
        return "quota_exhausted", {
            "error": detail, "retry_after": reset or None,
            "note": "agy 구독 사용량 한도로 리뷰가 수행되지 않았다. 통과가 아니다."}, {}
    if cause == "timeout":
        if kind == "hard":
            _say_not_reviewed(
                "%s: agy 가 바깥 상한 %d초 안에 끝나지 않아 강제 종료했다 (--timeout %s 요청 · "
                "래퍼는 %d초로 읽었다)" % (stage, run.hard_limit, args.timeout,
                                       run.hard_limit - _HARD_TIMEOUT_MARGIN),
                "agy 가 자기 상한을 지키지 못했다(재인증 프롬프트 · 멈춤 등).",
                "`agy` 를 직접 한 번 실행해 상태를 보고, 시간을 두고 다시 돌릴 것.")
        else:
            _say_not_reviewed(
                "%s: agy 출력 시간 초과 (--timeout %s · %.0f초)" % (stage, args.timeout, run.elapsed),
                "저장소를 넓게 읽었거나 모델이 느리다. 같은 모델로 곧바로 재시도하지 않는다 "
                "(대기만 두 배가 된다).",
                "몇 분 뒤 다시 돌리거나 --timeout 을 늘린다(예: --timeout 20m). 모델을 낮추지 말 것.")
        return "timeout", {
            "error": detail,
            "note": "agy 시간 초과로 리뷰가 수행되지 않았다. 통과가 아니다."}, {"timeout_kind": kind}
    if cause == "model_unavailable":
        _say_not_reviewed(
            "%s: agy 가 모델 %s 를 모른다" % (stage, model), detail,
            "`agy models` 로 이름을 확인하고 --model 로 지정할 것(flash 로 낮추지 말 것).")
        return "model_unavailable", {
            "error": detail,
            "note": "agy 가 모델을 알지 못해 리뷰가 수행되지 않았다. 통과가 아니다."}, {}
    _say_not_reviewed(
        "%s: agy 가 실패했다 (종료코드 %s · %.0f초)" % (
            stage, "없음" if run.rc is None else run.rc, run.elapsed),
        detail, "`agy` 를 직접 한 번 실행해 설치 · 로그인 상태를 확인할 것.")
    return "tool_error", {
        "error": detail,
        "note": "agy 실행이 실패했다 — 리뷰가 수행되지 않았다. 통과가 아니다."}, {}


def _recover_empty_response(agy: str, model: str, args, root: str, diff_path: str,
                            files: List[str], project_ctx: str, first: "_AgyRun",
                            first_cause: str, first_detail: str,
                            calls: List[dict]) -> Tuple[str, dict, dict]:
    """구조화 응답이 **비었을 때**의 복구. 반환 `(mode, 결과 본문, _meta 추가 필드)`.

    ⛔ [1.4.0 결정 1] **판정은 주 모델만 낸다.** 진단 모델(`--probe-model`)은 한 줄 생존
      확인에만 쓰고 리뷰를 요청하지 않는다. 1.3.x 는 flash 로 구조화를 다시 요청해 그
      approve 를 exit 0 으로 냈다 — 사용자 지시("flash 계열로 바꾸지 말 것 · 빈 응답이면
      재시도하지 모델을 낮추지 않는다", 26.08.20 · 21)와 정면으로 어긋났다.
    ⛔ [1.4.0 결정 4] 같은 모델로 구조화를 **다시 요청하지 않는다.** 최악 대기만 늘고,
      갈린 실측이 없다. 시간 초과 · 쿼터는 여기 오기 전에 끝난다(`_FAILURE_CAUSES`).

        구조화(주 모델) 응답이 비었다 — empty · tool_denied
              │
              ▼
        생존 확인(주 모델, 한 줄) ─ 응답 없음 ─┬─ 쿼터 안내 ─────────────────▶ quota_exhausted (4)
              │                                ├─ 진단 모델 생존 확인 ─ 응답 ──▶ primary_model_unavailable (4)
              │ 응답                            │                     └ 없음 ──▶ tool_unavailable (4)
              ▼                                └─ 진단 모델 없음 ──────────────▶ tool_unavailable (4)
        텍스트 재시도(주 모델) ─┬─ 원문 받음 ─────────────────────────────────▶ text_fallback (6)
                               ├─ 시간 초과 · 쿼터 ─────────────────────────────▶ timeout · quota_exhausted (4)
                               ├─ agy 실패(모델 · 인증) ────────────────────────▶ model_unavailable · tool_error (2)
                               └─ 빈 응답 · 권한 거부 ──────────────────────────▶ review_unavailable (4)

    최악 소요(`--timeout 10m`): 구조화 720 + 생존 확인 180 + 텍스트 720 ≈ 1,620초.
    """
    info = _empty_info(first.stdout)
    _safe_print("⚠ 구조화 응답이 비었다 (%s%s)." % (
        "도구 권한 거부" if first_cause == "tool_denied" else "빈 응답",
        "" if info is None else " · status=%s · output %s · thinking %s"
        % (info["status"], info["output_tokens"], info["thinking_tokens"])))
    if first_cause == "tool_denied":
        _safe_print("   리뷰어가 권한이 필요한 도구(명령 실행 등)를 시도해 agy 가 거부했다: %s"
                    % first_detail)

    # ① 도구 계층이 살아 있는가 [26.09.10] — 죽어 있으면 긴 재시도는 전부 낭비다.
    _safe_print("   → 계층 생존 확인 (주 모델 %s · 한 줄 프롬프트 · %s)" % (model, _PROBE_TIMEOUT))
    alive, prun = _probe_alive(agy, model, root)
    pcause, pkind, pdetail = _probe_cause(alive, prun)
    calls.append(_call_record("probe", model, prun, pcause, pkind, pdetail))
    if not alive:
        if pcause == "quota":
            return _failure_result("생존 확인", model, prun, pcause, pkind, pdetail, args)
        probe_model = _resolve_probe_model(args)
        if not probe_model:
            # ⛔ [26.09.10 2회차 리뷰] **확인한 만큼만 말한다.** 하나만 찔렀으면 계층을 단정하지 않는다.
            _say_not_reviewed(
                "주 모델이 한 줄 프롬프트에도 응답하지 않는다 (%.0f초)" % prun.elapsed,
                "⚠ 주 모델 하나만 확인했다(진단 모델 없음) — 계층 장애인지 주 모델만의 "
                "문제인지 가르지 못했다.",
                "`--no-probe-fallback` 을 빼고 다시 돌려 볼 것.")
            return "tool_unavailable", {
                "probed_models": [model],
                "note": "주 모델이 한 줄 프롬프트에도 응답하지 않았다(주 모델만 확인) — "
                        "리뷰가 수행되지 않았다. 통과가 아니다."}, {}
        _safe_print("   주 모델이 응답하지 않았다 (%.0f초) — 진단 모델로 계층을 확인한다: %s"
                    % (prun.elapsed, probe_model))
        alive2, prun2 = _probe_alive(agy, probe_model, root)
        c2, k2, d2 = _probe_cause(alive2, prun2)
        calls.append(_call_record("probe", probe_model, prun2, c2, k2, d2))
        if alive2:
            _say_not_reviewed(
                "주 모델 %s 만 응답하지 않는다 (진단 모델 %s 는 %.0f초에 응답)"
                % (model, probe_model, prun2.elapsed),
                "계층 장애가 아니다 — 주 모델의 일시적 불능(용량 · 한도)으로 보인다.",
                "시간을 두고 **같은 모델로** 다시 돌릴 것. 판정은 주 모델만 낸다 — "
                "모델을 낮추지 않는다.")
            return "primary_model_unavailable", {
                "probed_models": [model, probe_model],
                "note": "주 모델만 응답하지 않았다 — 진단 모델로는 리뷰하지 않는다. "
                        "리뷰가 수행되지 않았다. 통과가 아니다."}, {}
        _say_not_reviewed(
            "agy 가 한 줄 프롬프트에도 응답하지 않는다 (주 %.0f초 · 진단 %.0f초) — 도구 계층 장애다"
            % (prun.elapsed, prun2.elapsed),
            "두 모델을 확인했다 — diff 크기 · 프롬프트 내용의 문제가 아니다.",
            "`agy models` 로 인증을 확인하고, 시간을 두고 다시 돌릴 것.")
        return "tool_unavailable", {
            "probed_models": [model, probe_model],
            "note": "agy 계층이 한 줄 프롬프트에도 응답하지 않았다(주 · 진단 모델 둘 다) — "
                    "리뷰가 수행되지 않았다. 통과가 아니다."}, {}

    # ② 살아 있다 → 형식을 포기하고 **같은 주 모델**로 내용을 받는다 [26.08.12].
    #    `--json-schema` 강제가 빈 응답을 부르는 경우가 실측으로 있었다.
    _safe_print("   생존 확인 OK (%.0f초) — 계층은 살아 있다. 주 모델로 텍스트 재시도한다 "
                "(형식만 포기, 리뷰는 받는다)." % prun.elapsed)
    text, trun, tcause, tkind, tdetail = _retry_as_text(
        agy, model, args, root, diff_path, files, project_ctx)
    calls.append(_call_record("text", model, trun, tcause, tkind, tdetail))
    if text:
        _safe_print("")
        _safe_print("-" * 74)
        _safe_print("[텍스트 모드 리뷰 — 구조화 실패로 원문 그대로]")
        _safe_print("-" * 74)
        _safe_print(text)
        # ⚠ [26.08.13 Gemini 교차리뷰 지적 — 실측 확인] 구조화에 실패했으니 verdict 를
        #   지어내지 말고, 형식이 다르다는 사실 자체를 파일에 남긴다.
        # ⚠ [26.08.25] 종전엔 0 이었다. 그런데 note 가 스스로 *"원문을 사람이 읽어야
        #   한다"* 고 적는다 — 종료코드가 성공이면 그 당부는 자동화에 전달되지 않는다.
        return "text_fallback", {
            "raw_text": text,
            "note": "스키마 강제가 빈 응답을 반환해 텍스트로 재시도했다 "
                    "— verdict·findings 없음. 원문을 사람이 읽어야 한다."}, {}
    if tcause in _FAILURE_CAUSES:
        return _failure_result("텍스트 재시도", model, trun, tcause, tkind, tdetail, args)
    _say_not_reviewed(
        "구조화 · 텍스트 재시도가 모두 빈 응답이었다 (%s)" % model,
        "텍스트 재시도: 도구 권한 거부 — %s" % tdetail if tcause == "tool_denied" else
        "계층은 살아 있으나 이 리뷰에 응답을 내지 못했다(시점에 따라 갈린 실측이 있다).",
        "몇 분 뒤 2회까지 다시 돌리고, 그래도 안 되면 다른 계열(codex 등) 리뷰로 대체할 것.")
    return "review_unavailable", {
        "note": "구조화 · 텍스트 재시도가 모두 빈 응답이었다 — 리뷰가 수행되지 않았다. "
                "통과가 아니다."}, {}


def _run_check(args, calls: List[dict]) -> Tuple[int, dict]:
    """`--check` — **코드를 보내지 않고** 리뷰가 돌 준비가 됐는지 본다. 반환 `(종료 코드, 결과 본문)`.

    [1.4.0 결정 6 · E15] 첫 실제 리뷰에서야 agy 부재 · 로그인 · 모델 · 인터프리터 문제가 드러났다
    (DX 리뷰 TTHW 8~15분). 그리고 해석 계층은 **agy 의 출력 표면**(래퍼 필드 · 시간 초과
    안내문 · 모델 없음 문구)에 기댄다 — agy 가 판을 올려 문구를 바꾸면 시간 초과가 빈 응답으로,
    모델명 오류가 도구 오류로 조용히 오진된다. 그 표면을 한 줄 프롬프트로 직접 찔러 본다.

    종료 코드: 0 모두 정상 / 2 설치 · 인증 · 출력 형식 문제 / 4 모델 무응답 · 쿼터(기다리면 풀림).
    ⚠ 결과 JSON 의 `_meta.passed` 는 언제나 false 다 — 점검은 리뷰가 아니다.
    ⛔ agy 에는 **한 줄 프롬프트만** 보낸다. `--add-dir` · diff · 저장소 경로를 넘기지 않는다(테스트가 본다).
    """
    checks: List[dict] = []
    bad = set()

    def mark(name: str, ok, detail: str, rc_if_bad: int = EXIT_TOOL_ERROR) -> None:
        # ok: True 정상 · False 문제 · None 참고(종료 코드에 영향 없음)
        checks.append({"name": name, "ok": ok, "detail": detail})
        _safe_print("  %s %s: %s" % ({True: "✓", False: "✗", None: "·"}[ok], name, detail))
        if ok is False:
            bad.add(rc_if_bad)

    def done() -> Tuple[int, dict]:
        # 설치 · 인증 · 형식 문제(2)가 기다리면 풀리는 문제(4)보다 먼저다 — 고칠 것이 있다는 뜻이다.
        rc = (EXIT_TOOL_ERROR if EXIT_TOOL_ERROR in bad
              else EXIT_NOT_REVIEWED if bad else EXIT_PASSED)
        _safe_print("점검 %s (종료코드 %d) — 리뷰는 수행하지 않았다." % ("정상" if rc == 0 else "문제 있음", rc))
        return rc, {"checks": checks, "note": "점검 결과다 — 리뷰가 아니다. 통과가 아니다."}

    _safe_print("gemini-review v%s 점검 — 코드는 보내지 않는다 (한 줄 프롬프트만)" % __version__)
    _safe_print("스크립트: %s" % os.path.abspath(__file__))
    mark("python", sys.version_info >= (3, 7),
         "%s (%s)" % (sys.version.split()[0], sys.executable))
    cwd = os.getcwd()
    root = None
    try:
        mark("git", True, _git(["--version"], cwd).strip())
        try:
            root = _git_root(cwd)
            mark("저장소", True, root)
        except RuntimeError:
            mark("저장소", None, "현재 폴더는 git 저장소가 아니다 — 리뷰는 저장소 안에서 돌린다")
    except RuntimeError as exc:
        mark("git", False, str(exc))

    agy = _find_agy(root)
    if not agy:
        mark("agy", False, "찾지 못했다 — https://antigravity.google/cli 에서 설치하고 `agy` 로 로그인")
        return done()
    mark("agy", True, agy)
    # ⛔ agy 의 작업 폴더를 **빈 임시 폴더**로 둔다. 저장소에서 돌리면 agy 가 그 폴더를 작업
    #   공간으로 삼는다 — "코드를 보내지 않는다" 를 프롬프트 내용이 아니라 구조로 지킨다.
    where = tempfile.mkdtemp(prefix="gemini_review_check_")
    try:
        return _check_agy_surface(agy, args, where, mark, done, calls)
    finally:
        shutil.rmtree(where, True)


def _check_agy_surface(agy: str, args, where: str, mark, done, calls: List[dict]) -> Tuple[int, dict]:
    """`--check` 의 agy 부분 — 모델 응답 · 래퍼 필드 · 시간 초과 안내 · 모델 없음 안내."""
    # ① 주 모델이 한 줄 프롬프트에 구조화 래퍼로 답하는가 (로그인 · 모델 · 래퍼 필드)
    run = _run_agy(agy, args.model, ["--output-format", "json",
                                     "--print-timeout", _PROBE_TIMEOUT,
                                     "-p", _PROBE_PROMPT], where, _PROBE_TIMEOUT, 60)
    cause, kind, detail = _classify_run(run, structured=True)
    calls.append(_call_record("check_model", args.model, run, cause, kind, detail))
    if cause != "ok":
        mark("모델 응답", False, "%s · %s%s" % (args.model, cause, " · " + detail if detail else ""),
             EXIT_NOT_REVIEWED if cause in ("timeout", "quota", "empty") else EXIT_TOOL_ERROR)
        return done()
    # ⚠ 래퍼를 **먼저** 본다. 형식이 바뀌어 `response` 를 못 찾으면 OK 판별도 틀리는데, 그것을
    #   "모델 무응답(4)" 으로 적으면 기다리라는 뜻이 된다 — 고칠 것은 이 스크립트다(2).
    try:
        wrapper = json.loads(run.stdout)
    except ValueError:
        wrapper = None
    missing = [k for k in ("status", "response") if not (isinstance(wrapper, dict) and k in wrapper)]
    if missing:
        mark("모델 응답", None, "%s · %.0f초 · agy 는 응답했으나 래퍼를 읽지 못해 내용은 확인하지 못했다"
             % (args.model, run.elapsed))
        mark("출력 래퍼", False, "필드 %s 가 없다 — agy 출력 형식이 바뀌어 빈 응답 판별이 틀릴 수 "
             "있다" % ", ".join(missing))
    else:
        says_ok = _probe_says_ok(_response_text(run.stdout))
        mark("모델 응답", says_ok, "%s · %.0f초%s" % (
            args.model, run.elapsed, "" if says_ok else " · 응답이 OK 가 아니다: %s"
            % _first_line(_response_text(run.stdout))[:80]))
        mark("출력 래퍼", True, "status · response 필드 있음")

    # ①-b [1.5.0 X2] usage 모양이 바뀌면 토큰 집계가 **조용히** 값을 잃는다. 알던 키가
    #   사라진 것만 실패로 올린다 — 새 키가 생기는 것은 무해하므로 참고다.
    usage, _, _, _ = _wrapper_usage(run.stdout)
    if usage is None:
        mark("usage 필드", None,
             "래퍼에 usage 가 없다 — 이 판에서는 리뷰당 토큰을 집계할 수 없다")
    else:
        lost = sorted(_KNOWN_USAGE_KEYS - set(usage))
        extra = sorted(set(usage) - _KNOWN_USAGE_KEYS)
        if lost:
            mark("usage 필드", False,
                 "알던 키가 사라졌다: %s — 집계가 조용히 값을 잃는다(_KNOWN_USAGE_KEYS 갱신 필요)"
                 % ", ".join(lost))
        else:
            mark("usage 필드", True, "키 %d개%s" % (
                len(usage), " · 처음 보는 키: " + ", ".join(extra) if extra else ""))

    # ② 출력 시간 초과 안내문을 알아보는가 — 못 알아보면 시간 초과가 '빈 응답' 으로 오진된다
    run = _run_agy(agy, args.model, ["--output-format", "json", "--print-timeout", "1s",
                                     "-p", _CHECK_SLOW_PROMPT], where, "1s", 30)
    cause, kind, detail = _classify_run(run, structured=True)
    calls.append(_call_record("check_timeout", args.model, run, cause, kind, detail))
    if cause == "timeout" and kind == "agy":
        mark("시간 초과 안내", True, "1초 상한에서 안내문을 알아봤다 (agy 종료코드 %s)" % run.rc)
    elif cause == "ok":
        mark("시간 초과 안내", None, "1초 안에 끝나 확인하지 못했다")
    else:
        mark("시간 초과 안내", False,
             "1초 상한에서 원인을 %s 로 읽었다%s — agy 안내문이 바뀌었을 수 있다"
             % (cause, " (" + detail + ")" if detail else ""))

    # ③ 없는 모델명을 알아보는가 — 못 알아보면 모델명 오류가 도구 오류 · 빈 응답으로 읽힌다
    run = _run_agy(agy, _CHECK_NO_SUCH_MODEL, ["--output-format", "json", "--print-timeout", "30s",
                                               "-p", _PROBE_PROMPT], where, "30s", 30)
    cause, kind, detail = _classify_run(run, structured=True)
    calls.append(_call_record("check_no_model", _CHECK_NO_SUCH_MODEL, run, cause, kind, detail))
    mark("모델 없음 안내", cause == "model_unavailable",
         "없는 모델명을 알아봤다" if cause == "model_unavailable" else
         "없는 모델명을 %s 로 읽었다%s — agy 문구가 바뀌었을 수 있다"
         % (cause, " (" + detail + ")" if detail else ""))
    return done()


# 1초 안에 끝나지 않을 만큼 출력이 긴 요청 — 시간 초과 안내문을 일부러 부른다(코드는 보내지 않는다).
_CHECK_SLOW_PROMPT = "Write the integers from 1 to 400, one per line, with no other text."
# agy 가 모를 것이 분명한 모델명. 이름 자체가 점검용임을 드러낸다.
_CHECK_NO_SUCH_MODEL = "gemini-review-check-no-such-model"


def _resolve_probe_model(args) -> str:
    """진단 모델. 끄면 빈 문자열. 주 모델과 같으면 다른 모델로 바꾼다(같으면 확인이 무의미하다)."""
    probe = "" if args.no_probe_fallback else (args.probe_model or "").strip()
    if probe and probe == args.model:
        # ⚠ [26.09.10] 종전에는 이 충돌이 **침묵한 채** 확인을 건너뛰었다.
        alt = _DEFAULT_MODEL if args.model != _DEFAULT_MODEL else _DEFAULT_PROBE_MODEL
        _safe_print("   ⚠ 진단 모델이 주 모델과 같다(%s) — 대신 %s 로 확인한다." % (probe, alt))
        probe = alt
    return probe


def _probe_cause(alive: bool, run: "_AgyRun") -> Tuple[str, str, str]:
    """생존 확인 호출의 원인 기록. 살아 있으면 `ok`, 응답은 왔는데 OK 가 아니면 `no_ok`."""
    if alive:
        return "ok", "", ""
    cause, kind, detail = _classify_run(run, structured=False)
    if cause == "ok":
        return "no_ok", "", _first_line(run.stdout)
    return cause, kind, detail


# 텍스트 출력이라 래퍼가 없는 단계. 나머지는 `--output-format json` 이라 래퍼가 있다.
_TEXT_OUTPUT_STAGES = frozenset(("probe", "text"))

# agy 1.2.3 실측(2026-09-16). **알던 키가 사라지면** 집계가 조용히 값을 잃으므로 `--check` 가
#   실패로 올린다. 모르는 키가 생기는 것은 무해하므로 참고로만 적는다.
#   ⚠ 기준선을 상태 파일이 아니라 상수로 두는 이유: 상수는 git 에 남아 변경이 리뷰되고,
#     "첫 실행은 참고" 라는 예외(그 자체가 조용한 실패 경로)가 필요 없다.
_KNOWN_USAGE_KEYS = frozenset((
    "input_tokens", "output_tokens", "thinking_tokens",
    "cache_read_tokens", "total_tokens"))


def _wrapper_usage(raw: str):
    """agy 래퍼에서 `(수치 usage, 걸러낸 키, num_turns, duration_seconds)`.

    래퍼가 아니거나 `usage` 가 없으면 `(None, [], …)` — **필드를 빼고 진행한다.**
    기록 실패가 판정이나 종료 코드를 바꾸면 안 된다.

    ⛔ [1.5.0 Eng H5] `usage` 를 "키 그대로" 실으면 agy 가 문자열이나 중첩 dict 를 넣는 순간
      `--stats` 가 `TypeError` 로 죽는다. JSON 자체는 멀쩡하니 "깨진 파일" 규칙에도 걸리지
      않아 **조용히** 죽는다. 그래서 수치만 남기고 걸러낸 키 이름을 따로 적는다.
    ⚠ `bool` 은 `int` 의 하위형이라 따로 뺀다 — `True` 가 1 로 집계되면 안 된다.
    """
    try:
        wrapper = json.loads(raw)
    except ValueError:
        return None, [], None, None
    if not isinstance(wrapper, dict):
        return None, [], None, None

    def _num(value, ints_only=False):
        if isinstance(value, bool):
            return None
        if ints_only:
            return value if isinstance(value, int) else None
        return value if isinstance(value, (int, float)) else None

    turns = _num(wrapper.get("num_turns"), ints_only=True)
    secs = _num(wrapper.get("duration_seconds"))
    usage = wrapper.get("usage")
    if not isinstance(usage, dict):
        return None, [], turns, secs
    nums, dropped = {}, []
    for key, value in usage.items():
        number = _num(value)
        if number is None:
            dropped.append(str(key))
            continue
        nums[str(key)] = number
    return nums, sorted(dropped), turns, secs


def _call_record(stage: str, model: str, run: "_AgyRun", cause: str, kind: str,
                 detail: str) -> dict:
    """`_meta.calls` 한 줄 — agy 호출마다 **원인**을 남긴다(1.4.0).

    ⚠ 1.3.x 는 시간 초과 · 도구 권한 거부 · 진짜 빈 응답을 모두 "빈 응답" 으로 뭉쳐 적어,
      exit 4 가 네 번 이어진 날 원인을 짚는 데 stderr 를 따로 떠야 했다(26.09.14).
    """
    rec = {"stage": stage, "model": model, "cause": cause,
           "agy_exit_code": run.rc, "seconds": round(run.elapsed, 1)}
    if kind:
        rec["timeout_kind"] = kind
    if detail:
        rec["detail"] = detail[:300]
    # [1.5.0 A1] 토큰은 **여기 한 곳**에서 붙인다. 호출부가 일곱 군데라 거기서 넣으면
    #   언젠가 빠진다 — 최상위 `model` 이 같은 실수를 두 번 했다(Eng H1).
    if stage in _TEXT_OUTPUT_STAGES:
        # 텍스트 출력에는 래퍼가 없다. "값이 0" 이 아니라 **알 길이 없다** 는 사실을 남긴다.
        rec["usage_unavailable"] = "text_output"
    else:
        usage, dropped, turns, secs = _wrapper_usage(run.stdout)
        if usage is not None:
            rec["usage"] = usage
            if dropped:
                rec["usage_dropped_keys"] = dropped
        if turns is not None:
            rec["agy_turns"] = turns
        if secs is not None:
            # 기존 `seconds` 는 **스크립트가 잰 벽시계**다. 둘은 다른 값이고 둘 다 쓸모가 있다.
            rec["agy_duration_seconds"] = secs
    return rec


def _apply_deprecated_flags(args) -> None:
    """1.3.x 플래그 이름을 경고와 함께 받는다(1.4.x 동안). 2.0 에서 뺀다."""
    if args.old_fallback_model is not None:
        _safe_print("⚠ --fallback-model 은 폐기 예정이다 — --probe-model 을 쓸 것. 1.4.0 부터 이 "
                    "모델은 판정을 내지 않고 생존 확인에만 쓴다.")
        if args.probe_model == _DEFAULT_PROBE_MODEL:
            args.probe_model = args.old_fallback_model
    if args.old_no_fallback:
        _safe_print("⚠ --no-fallback 은 폐기 예정이다 — --no-probe-fallback 을 쓸 것.")
        args.no_probe_fallback = True


def _resolve_scope(root: str, args) -> dict:
    """`_meta.scope` — 범위 **이름**이 아니라 **해석된 SHA** 를 남긴다(1.4.0).

    `--base main` 은 시간이 지나면 다른 커밋을 가리킨다. 며칠 뒤 결과 파일로 "무엇을
    리뷰했나" 를 재구성하려면 SHA 가 있어야 한다. 해석에 실패해도 리뷰는 막지 않는다(None).
    ⚠ diff 해시(`diff_sha256`)는 미뤘다(결정 5) — 커밋 게이트 hook 이 생길 때 **리뷰에 쓴
      같은 바이트**로 계산한다.
    """
    def sha(ref):
        if not ref or ref.startswith("-"):
            return None
        try:
            return _git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"],
                        root).strip() or None
        except RuntimeError:
            return None

    def branch():
        """현재 브랜치 이름. detached HEAD 면 None.

        ⛔ [1.5.0 Eng H3] `_git` 은 rc≠0 에서 `RuntimeError` 를 던지고,
          `git symbolic-ref --short -q HEAD` 는 detached HEAD 에서 rc 1 이다. 이 함수의
          호출부는 `try` 밖이라 삼키지 않으면 `main` 의 포괄 핸들러가 잡아 **exit 1
          (`internal_error`)** 이 된다 — rebase · bisect 중에는 리뷰가 아예 안 돈다.
        """
        try:
            return _git(["symbolic-ref", "--short", "-q", "HEAD"], root).strip() or None
        except RuntimeError:
            return None

    # `repo` 는 저장소 **절대 경로**다. 결과 파일은 0600 이지만 `--out` 으로 저장소 안에 쓰면
    # 그 경로가 커밋될 수 있다 — README · SKILL.md 의 `--out` 주의 문구에 적는다(CEO A3-1).
    common = {"repo": root, "branch": branch()}
    if args.staged:
        scope = {"kind": "staged", "head_sha": sha("HEAD")}
        scope.update(common)
        return scope
    if getattr(args, "paths", None):
        scope = {"kind": "paths", "paths": list(args.paths), "head_sha": sha("HEAD")}
        scope.update(common)
        return scope
    scope = {"kind": "range", "base": args.base, "head": args.head,
             "two_dot": bool(args.two_dot),
             "base_sha": sha(args.base), "head_sha": sha(args.head)}
    scope.update(common)
    if not args.two_dot:
        scope["merge_base_sha"] = None
        if scope["base_sha"] and scope["head_sha"]:
            try:
                scope["merge_base_sha"] = _git(
                    ["merge-base", scope["base_sha"], scope["head_sha"]], root).strip() or None
            except RuntimeError:
                pass
    return scope


def _write_out(out_path: Optional[str], payload: dict,
               quiet: bool = False) -> Optional[str]:
    """결과 JSON 저장. 반환: 기록한 경로, **실패하면 None**.

    `quiet` 는 **시작 시점 무효화**용이다 — 저장 안내를 두 번 찍지 않는다.
    ⚠ 실패를 어떻게 다룰지는 호출부가 정한다(명시한 `--out` 이면 exit 2).
    """
    try:
        path = out_path or os.path.join(
            _default_result_dir(),
            "gemini_review_%s_%d.json"
            % (datetime.now().strftime("%Y%m%d_%H%M%S"), os.getpid()))
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        _write_json_atomic(path, payload)
    except OSError as exc:
        if not quiet:
            _safe_print("결과 저장 실패: %s" % exc)
        return None
    if not quiet:
        _safe_print("")
        _safe_print("결과 저장: %s" % path)
    return path


class _PrintVersion(argparse.Action):
    """`--version` — 판과 스크립트 경로를 **한 줄로** 찍고 끝낸다.

    ⚠ argparse 기본 `version` 동작은 터미널 폭에 맞춰 줄을 접어 경로가 `gemini-` /
      `review` 로 끊겼다(26.09.14 실측). 이 줄은 "어느 설치본이 돌았나" 를 확인하는
      용도라 끊긴 경로는 쓸 수 없다.
    """
    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS,
                 help="판 · 스크립트 경로를 찍고 끝낸다"):
        super().__init__(option_strings=option_strings, dest=dest, default=default,
                         nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        _safe_print("gemini-review %s (%s)" % (__version__, os.path.abspath(__file__)))
        parser.exit()


def _build_parser() -> argparse.ArgumentParser:
    # ⚠ help · epilog 문자열에 **cp949 에 없는 문자(— 등)를 쓰지 말 것.** `PYTHONIOENCODING=cp949`
    #   처럼 인코딩을 명시한 콘솔에서는 `_ensure_utf8_stdout` 가 손대지 않으므로, argparse 가
    #   찍는 순간 UnicodeEncodeError 로 죽는다(1.4.0 작업 중 실측 — `_check_help_survives_cp949`).
    ap = argparse.ArgumentParser(
        description="Gemini cross-review via Antigravity CLI (agy)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_exit_code_epilog())
    ap.add_argument("--version", action=_PrintVersion)
    ap.add_argument("--check", action="store_true",
                    help="코드를 보내지 않고 python · git · agy · 로그인 · 모델 응답과 agy 출력 "
                         "형식(시간 초과 · 모델 없음 안내)을 점검한다. 0 정상 · 2 설치 · 인증 · "
                         "형식 문제 · 4 무응답 · 쿼터")
    ap.add_argument("--base", default="HEAD~1",
                    help="비교 기준 (기본 HEAD~1). merge-base 기준 3-dot 이다")
    ap.add_argument("--head", default="HEAD", help="비교 끝 (기본 HEAD)")
    ap.add_argument("--staged", action="store_true",
                    help="스테이징된 변경 리뷰 (커밋 직전). 스테이징이 비면 exit 8")
    ap.add_argument("--allow-empty", action="store_true",
                    help="--staged 인데 스테이징이 비었을 때 exit 8 대신 0 (그래도 통과는 아니다)")
    # [1.7.0] 두 세션이 작업 트리를 공유하면 스테이징을 건드리지 않는 경로 지정 커밋을 쓴다.
    #   그 커밋이 담을 내용은 스테이징과 다르므로 `--staged` 로는 리뷰할 수 없었다.
    ap.add_argument("--paths", nargs="+", metavar="경로",
                    help="`git commit -- 경로` 가 커밋할 내용(HEAD 에 그 경로의 작업 트리 내용을 "
                         "얹은 것)을 리뷰한다. 스테이징을 건드리지 않는다. 변경이 없으면 exit 8")
    ap.add_argument("--record-fallback", action="store_true",
                    help="Gemini 리뷰가 수행되지 않았을 때(같은 내용의 최근 시도가 exit 4) 운영 "
                         "규칙이 정한 대체 리뷰를 **마쳤다는 기록**을 남긴다. --staged 또는 --paths "
                         "와 --reviewer · --summary 가 필요하다. 리뷰를 요청하지 않는다. "
                         "0 기록함 · 2 거부")
    ap.add_argument("--reviewer", metavar="이름",
                    help="--record-fallback: 대체 리뷰를 한 주체 (예: codex · 서브에이전트 다렌즈)")
    ap.add_argument("--summary", metavar="요약",
                    help="--record-fallback: 대체 리뷰 결과 요약 (반영한 지적 포함)")
    ap.add_argument("--two-dot", action="store_true",
                    help="base..head 2-dot diff (기본은 merge-base 기준 3-dot). "
                         "브랜치 리뷰에서는 쓰지 마라 (남의 커밋이 섞인다)")
    # ⛔ 자기 우회 계열이다(`--allow-sensitive` 와 같다). 구독을 올렸거나 다른 기기에서
    #   풀린 것을 확인했을 때 **사용자가** 쓴다. 화면 · 결과에서 이것을 권하지 않는다.
    ap.add_argument("--ignore-quota-cache", action="store_true",
                    help="기록된 쿼터 차단을 무시하고 실제로 호출한다 "
                         "(사용자 전용: Claude 가 스스로 붙이지 말 것)")
    ap.add_argument("--stats", action="store_true",
                    help="쌓인 결과 파일을 집계한다 (agy 를 부르지 않고 결과도 쓰지 않는다)")
    ap.add_argument("--stats-dir", metavar="경로",
                    help="--stats 가 읽을 폴더 (기본: 결과 폴더. 폴더를 만들지 않는다)")
    ap.add_argument("--since", default="14d", metavar="기간",
                    help="--stats 의 기간. d(일) · h(시간) 단위 (기본 14d)")
    # ⚠ `--effort` 는 두지 않는다. agy 는 **모델명에 effort 가 내장**돼 있고
    # (`gemini-3.1-pro-high`/`-low`, `gemini-3.6-flash-medium` …), 모델 접미사와
    # 다른 --effort 를 주면 즉시 status=ERROR 로 죽는다 (실측: 0초, tokens 0).
    ap.add_argument("--model", default=_DEFAULT_MODEL,
                    help="판정을 내는 모델 (기본 %s: 느리다고 낮추지 말 것). "
                         "`agy models` 로 목록 확인" % _DEFAULT_MODEL)
    # ⛔ [1.4.0 결정 1] 1.3.x 의 `--fallback-model` 은 주 모델이 빈 응답이면 **그 모델로
    #   리뷰를 다시 요청**했고 그 판정이 exit 0 이 됐다. 이제 이 모델은 판정하지 않는다 —
    #   뜻이 바뀌었으므로 이름도 바꾼다(옛 이름은 경고와 함께 1.4.x 동안 받는다).
    ap.add_argument("--probe-model", default=_DEFAULT_PROBE_MODEL,
                    help="진단 모델 (기본 %s). 빈 응답일 때 주 모델이 한 줄 프롬프트에 "
                         "응답하지 않으면, 계층 장애인지 주 모델만의 문제인지 이 모델로 "
                         "가른다. **판정은 내지 않는다**" % _DEFAULT_PROBE_MODEL)
    ap.add_argument("--no-probe-fallback", action="store_true",
                    help="진단 모델로 확인하지 않는다 (주 모델만 확인)")
    ap.add_argument("--fallback-model", dest="old_fallback_model", default=None,
                    help="폐기 예정: --probe-model 로 바뀌었다 (1.4.0 부터 판정하지 않는다)")
    ap.add_argument("--no-fallback", dest="old_no_fallback", action="store_true",
                    help="폐기 예정: --no-probe-fallback 로 바뀌었다")
    ap.add_argument("--timeout", default="10m",
                    help="agy 응답 상한. 예: 10m · 90s · 1h (기본 10m). 래퍼는 +%d초에서 "
                         "강제 종료한다. 리뷰 1회 최악 소요 = 2 x (상한 + %d초) + %d초 "
                         "(기본값이면 약 27분)"
                         % (_HARD_TIMEOUT_MARGIN, _HARD_TIMEOUT_MARGIN,
                            _duration_seconds(_PROBE_TIMEOUT, 60) + _HARD_TIMEOUT_MARGIN))
    ap.add_argument("--out", default=None,
                    help="결과 JSON 경로. 생략하면 $XDG_STATE_HOME/gemini-review/ "
                         "(보통 ~/.local/state/gemini-review/, Windows 는 "
                         "%%LOCALAPPDATA%%\\gemini-review\\) 에 0600 으로 쌓인다")
    ap.add_argument("--allow-sensitive", action="store_true",
                    help="민감 경로가 diff 에 있어도 강행 (기본: 중단). 사용자가 목록을 보고 "
                         "명시적으로 승인한 경우에만 쓴다")
    return ap


def _exit_code_epilog() -> str:
    """`--help` 끝의 종료 코드 표. 정본은 `_EXIT_TABLE` 이다(README · SKILL.md 표가 같아야 한다)."""
    lines = ["종료 코드 (이 표에 없는 코드는 통과가 아니다):"]
    for codes, meaning in _EXIT_TABLE:
        lines.append("  %-10s %s" % (codes, meaning))
    lines.append("")
    lines.append("결과 JSON 의 _meta.passed 는 주 모델이 리뷰해 통과 판정을 냈을 때만 true 다.")
    return "\n".join(lines)


# `--stats` 와 함께 쓸 수 없는 인자. 집계는 리뷰를 하지 않으므로 범위 · 출력이 무의미하다.
_STATS_CONFLICTS = ("--staged", "--base", "--two-dot", "--out", "--allow-sensitive",
                    "--allow-empty", "--check", "--ignore-quota-cache", "--paths",
                    "--record-fallback", "--reviewer", "--summary")
# [1.7.0] `--paths` 는 범위를 스스로 정한다 — 다른 범위 인자와 섞으면 어느 쪽을 리뷰했는지 모호하다.
_PATHS_CONFLICTS = ("--staged", "--base", "--head", "--two-dot")
_PATHS_CONFLICT_MSG = "--paths 는 범위를 스스로 정한다: 함께 쓸 수 없다: %s"
# [1.7.0] 대체 리뷰 기록은 리뷰를 요청하지 않으므로 전송 · 쿼터 · 빈 변경 인자가 무의미하다.
_FALLBACK_CONFLICTS = ("--check", "--allow-sensitive", "--allow-empty", "--ignore-quota-cache",
                       "--base", "--head", "--two-dot")
_FALLBACK_CONFLICT_MSG = "--record-fallback 과 함께 쓸 수 없다: %s"
_FALLBACK_SCOPE_MSG = "--record-fallback 은 --staged 또는 --paths 로 범위를 정해야 한다"
_FALLBACK_FIELDS_MSG = "--record-fallback 은 --reviewer 와 --summary 가 필요하다(빈 값 불가)"
_FALLBACK_ONLY_MSG = "--reviewer · --summary 는 --record-fallback 과 함께만 쓴다"
# cp949 검사가 훑도록 문자열 상수로 뺀다(Eng M5). em dash 를 쓰지 않는다.
_STATS_CONFLICT_MSG = ("--stats 는 집계만 한다 — 리뷰 인자와 함께 쓸 수 없다: %s")


def _named_in_argv(argv, names) -> List[str]:
    """argv 에 그 인자가 **사용자가 적은 형태로** 있는가. 약어까지 잡는다.

    ⛔ [Eng M6] 문자열 완전 일치로 보면 argparse 의 약어(`--stag` · `--ou=x`)가 샌다.
      `_peek_out` 과 같은 규칙을 쓴다 — `--` 로 시작하고 `=` 앞부분이 어떤 이름의
      **접두사**이면 그 이름을 적은 것으로 본다.
    ⚠ `--` 뒤는 인자가 아니라 값이다.
    """
    hit = []
    for token in list(sys.argv[1:] if argv is None else argv):
        if token == "--":
            break
        if not token.startswith("--"):
            continue
        head = token.split("=", 1)[0]
        for name in names:
            if name.startswith(head) and name not in hit:
                hit.append(name)
    return hit


def _peek_out(argv) -> Optional[str]:
    """argparse 로 본 해석을 하기 **전에** `--out` 값만 알아낸다.

    ⚠ [26.09.14 Gemini 교차리뷰 MEDIUM] 종전에는 `--out` 만 아는 임시 파서를 따로
      썼다. 그 파서는 `--model` 이 값을 받는다는 것을 몰라, `--model --out --staged`
      를 `--out=--staged` 로 읽고 **`--staged` 라는 파일을 만들었다.** 본 파서와
      같은 정의(`_build_parser`)로 해석해 두 해석이 갈리지 않게 한다.
    ⚠ 인자 오류로 본 파서가 해석을 못 하면(값 빠짐 등) 보수적으로 직접 훑는다:
      `--out=값` 이거나, `--out` 바로 뒤 토큰이 `-` 로 시작하지 않을 때만 경로로 본다.
      이 단계의 도움말 · 오류 출력은 삼킨다 — 사용자에게는 본 해석이 한 번만 알린다.
    """
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            known, _ = _build_parser().parse_known_args(argv)
        return known.out
    except SystemExit:
        pass
    tokens = list(sys.argv[1:] if argv is None else argv)

    def is_out(flag):
        # 본 파서처럼 약어(`--o` · `--ou`)도 인정한다 — `--o` 로 시작하는 옵션은 `--out` 뿐이다
        #   [26.09.14 Gemini 교차리뷰 LOW].
        return len(flag) >= 3 and "--out".startswith(flag)

    found = None                          # argparse 처럼 **마지막** 값이 이긴다 [26.09.14 LOW]
    for i, tok in enumerate(tokens):
        if tok.startswith("--") and "=" in tok and is_out(tok.split("=", 1)[0]):
            found = tok.split("=", 1)[1] or None
        elif is_out(tok) and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
            found = tokens[i + 1]
    return found


def _state_dir(create: bool = True) -> Tuple[Optional[str], str]:
    """결과 · 상태 파일을 둘 폴더와 그 **신뢰 상태**. 반환 `(경로, 상태)`.

    상태는 셋이다.
      `ok`      — 사용자 전용이 확실하다
      `shared`  — Windows 에서 `%LOCALAPPDATA%` 가 없어 공유 `%TEMP%` 로 떨어졌다
      `unsafe`  — 링크 · 남의 소유 · 만들 수 없음 (이때 경로는 None)

    ⛔ [1.5.0 Eng M2] 세 호출부의 실패 동작이 **서로 다르다** — 결과 폴더는 `mkdtemp` 로
      폴백하고, 쿼터 상태는 아예 쓰지 않으며, `--stats` 는 exit 2 다. 그래서 여기서
      폴백을 정하지 않고 상태만 돌려준다.
    ⚠ `create=False` 는 폴더를 **만들지 않는다.** `--stats` 가 빈 폴더를 새로 만들면
      "0건 · exit 0" 으로 조용히 끝나 사용자가 결과가 없다고 오해한다.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        # ⛔ [Eng M7] `%LOCALAPPDATA%` 가 없으면 공유 `%TEMP%` 다. 남이 심은 `reset_at` 으로
        #   게이트를 막을 수 있으므로(통과 위조는 불가 — DoS) 호출부가 구분할 수 있게 알린다.
        status = "ok" if base else "shared"
        d = os.path.join(base or tempfile.gettempdir(), "gemini-review")
        if create:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                return None, "unsafe"
        return d, status
    base = os.environ.get("XDG_STATE_HOME") or ""
    if not os.path.isabs(base):          # XDG 규약: 상대 경로는 무시한다
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    d = os.path.join(base, "gemini-review")
    try:
        if create:
            os.makedirs(d, mode=0o700, exist_ok=True)
        elif not os.path.isdir(d):
            return d, "ok"               # 없는 폴더는 "안전하지 않음" 이 아니라 **0건**이다
        st = os.lstat(d)
        if (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode)
                or st.st_uid != os.getuid()):
            return None, "unsafe"
        # ⚠ [26.09.14 Gemini 교차리뷰 HIGH] "느슨하면 좁힌다"만으로는 부족하다. 내 소유
        #   폴더가 쓰기 권한 없이(0500, 엄격한 umask) 있으면 `& 0o077` 이 0 이라 그대로
        #   반환되고 결과 기록이 실패한다 → 정확히 0700 으로 맞춘다.
        if create and stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(d, 0o700)
        return d, "ok"
    except OSError:
        return None, "unsafe"


def _default_result_dir() -> str:
    """`--out` 을 주지 않았을 때 결과 JSON 을 둘 **사용자 전용** 폴더.

    ⛔ [26.09.14] 종전 기본값은 `tempfile.gettempdir()` 바로 아래
      `gemini_review_<시각>.json` 이었다. 이 PC 실측으로 `-rw-rw-r--` 파일
      137개가 공유 `/tmp` 에 쌓여 있었고, 담긴 것은 코드가 인용된 지적이다.
      같은 초에 끝난 두 실행은 서로 덮어썼다(→ 파일명에 PID).
    ⚠ 폴더가 **링크이거나 남의 소유**면 쓰지 않고 새 임시 폴더로 대체한다.
      [1.5.0 Eng M8] 대체하면 그 실행은 `--stats` 가 **영원히 못 본다** — 한 줄 알린다.
    """
    d, status = _state_dir(create=True)
    if d is None or status == "unsafe":
        fallback = tempfile.mkdtemp(prefix="gemini_review_out_")
        _say_note("결과 폴더가 안전하지 않아 임시 폴더에 남긴다",
                  "기본 폴더가 링크이거나 남의 소유이거나 만들 수 없다.",
                  "이 실행은 --stats 집계에 잡히지 않는다: %s" % fallback)
        return fallback
    return d


_QUOTA_STATE_NAME = "quota-state.json"
_QUOTA_QUERY_TIMEOUT = 20        # 실측 6초. `_run_agy` 는 여기에 120초를 더해 버려 쓸 수 없다
_QUOTA_MAX_HOURS = 72            # **추정 기록에만** 적용한다. 실측 최악이 63h45m 이었다
_QUOTA_STREAK_WARN = 2           # 이 횟수부터 화면에 게이트 부재 누적을 적는다


def _quota_state_path() -> Optional[str]:
    """쿼터 상태 파일. 폴더를 믿을 수 없으면 None — **대체 폴더를 만들지 않는다.**

    `mkdtemp` 로 대체하면 실행마다 새 폴더가 생겨 캐시가 조용히 무력해진다. 그럴 바에는
    캐시가 없는 편이 낫다 — 다음 실행이 609초를 쓰지만 그 사실이 화면에 보인다.
    ⚠ Windows 에서 `%LOCALAPPDATA%` 가 없어 공유 `%TEMP%` 로 떨어진 경우(`shared`)도
      쓰지 않는다. 남이 심은 `reset_at` 으로 게이트를 최대 72시간 막을 수 있다(Eng M7).
    """
    d, status = _state_dir(create=True)
    return os.path.join(d, _QUOTA_STATE_NAME) if d and status == "ok" else None


def _load_quota_state(path: Optional[str]) -> dict:
    """모델 이름 → 항목. 읽지 못하거나 모양이 깨졌으면 빈 dict — **막지 않는다.**

    ⛔ `main` 은 예기치 못한 예외를 전부 `internal_error`(exit 1)로 바꾼다. JSON 은 맞지만
      모양이 깨진 파일(`{"models": []}` · `reset_at: 123`)은 `TypeError` ·
      `AttributeError` 를 내므로 여기서 **전부** 잡는다.
    ⚠ 모르는 키는 무시한다(거부하지 않는다) — 1.5.1 이 필드를 더해도 1.5.0 이 남의 상태를
      버리지 않게.
    """
    if not path:
        return {}
    try:
        with io.open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        models = data.get("models")
        if not isinstance(models, dict):
            return {}
        return dict((name, item) for name, item in models.items()
                    if isinstance(name, str) and isinstance(item, dict))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {}


def _save_quota_state(path: Optional[str], models: dict) -> bool:
    """원자적으로 기록. 실패해도 **종료 코드를 바꾸지 않는다** — 성패만 돌려준다."""
    if not path:
        return False
    try:
        _write_json_atomic(path, {"models": models})
        return True
    except (OSError, ValueError, TypeError):
        return False


def _quota_entry_reset(entry: dict, now=None):
    """항목이 **아직 유효한 차단**이면 해제 시각, 아니면 None.

    ⚠ 72시간 상한과 `recorded_at` 미래 검사는 **추정 기록에만** 적용한다. `/quota` 가 준
      값은 권위 있으므로 자르지 않는다 — 자르면 실제보다 일찍 풀린 것으로 보여 609초를
      다시 태운다.
    """
    now = now or _utc_now()
    reset = _parse_utc(entry.get("reset_at"))
    if reset is None or reset <= now:
        return None
    if entry.get("source") != "quota_query":
        recorded = _parse_utc(entry.get("recorded_at"))
        if recorded is None or recorded > now:
            return None                  # 시계가 뒤로 갔다 — 믿지 않는다
        if (reset - recorded).total_seconds() > _QUOTA_MAX_HOURS * 3600:
            return None
    return reset


def _quota_query(agy: str, model: str, cwd: str, calls: Optional[List[dict]] = None,
                 timeout: int = _QUOTA_QUERY_TIMEOUT) -> Optional[List[dict]]:
    """`/quota` 의 그룹 목록. 읽지 못하면 None — **없다고 막지 않는다.**

    ⭐ [1.5.0] agy 의 print 모드는 읽기 전용 슬래시 명령을 **에이전트 턴 없이** 답한다.
      실측(agy 1.2.3): `usage` 전부 0 · `num_turns` 0 · `conversation_id` 빈 문자열 —
      즉 **토큰을 쓰지 않는다.** 그래서 "커밋당 총 토큰을 늘리지 않는다" 는 제약을 지킨다.
      소요는 실측 6초라 공짜는 아니다 → 차단이 기록된 뒤에만 부른다(호출부 참조).
    ⚠ agy 호출은 `_run_agy` **한 곳**을 쓴다 — `--mode plan` · stdin 차단 · 바깥 상한이 거기
      한 자리에 모여 있고, 흩어 두면 그 중 하나가 빠진다(26.09.15 S5, 실제로 세 자리에서
      한 번씩 빠졌다). 짧은 상한이 필요해 `margin` 만 줄인다 — 모델이 도는 호출이 아니라
      여유를 깎아도 정상 응답을 자를 위험이 없다.
    """
    run = _run_agy(agy, model, ["-p", "/quota", "--output-format", "json"],
                   cwd, "%ds" % timeout, timeout, margin=5)
    if calls is not None:
        # 조회도 `_meta.calls` 에 남긴다. 단락 경로의 불변식은 "구조화(`structured`) 호출이
        #   0회" 이지 "agy 호출이 0회" 가 아니다 — 무엇을 했는지 감추면 안 된다.
        calls.append(_call_record("quota_query", model, run,
                                  "ok" if run.rc == 0 else "tool_error", "", ""))
    if run.rc != 0:
        return None
    try:
        payload = json.loads(run.stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    command = payload.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        return None                      # agy 판이 바뀌어 모양이 다르다 — 믿지 않는다
    data = command.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        return None
    return data["groups"]


def _quota_group_for_model(groups: List[dict], model: str) -> Optional[dict]:
    """모델이 속한 쿼터 그룹. **정확히 하나일 때만** 인정한다.

    `/quota` 는 모델을 계열로 묶어 답한다(실측: `Gemini Models` · `Claude and GPT models`).
    모델 id 의 첫 토큰(`gemini-3.1-pro-high` → `gemini`)을 그룹 이름에서 찾는다.
    ⚠ 0개거나 2개 이상이면 None 을 돌려 **막지 않는다.** 비대칭이 분명하다 — 잘못 막으면
      그 기기의 커밋 게이트가 사라지고, 못 막으면 609초를 한 번 더 태울 뿐이다.
    """
    token = (model or "").strip().lower().split("-")[0]
    if not token:
        return None
    hits = [g for g in groups
            if isinstance(g, dict) and token in str(g.get("name", "")).lower()]
    return hits[0] if len(hits) == 1 else None


def _quota_blocked(group: dict, now=None):
    """차단 중이면 `(해제 시각, 차단 버킷들)`, 아니면 None.

    ⚠ 여유를 두지 않고 **정확히 0** 과 비교한다. 실측 차단값이 `remaining_fraction: 0`
      이었고, 여유를 두면 쿼터가 남았는데도 막는다.
    ⚠ 해제 시각을 읽지 못했거나 이미 지난 버킷은 세지 않는다 — 언제 풀리는지 말할 수 없는
      차단으로 게이트를 막으면 사용자가 기다릴 근거를 잃는다.
    """
    now = now or _utc_now()
    hit = []
    for bucket in group.get("buckets") or []:
        if not isinstance(bucket, dict):
            continue
        frac = bucket.get("remaining_fraction")
        if isinstance(frac, bool) or not isinstance(frac, (int, float)) or frac > 0:
            continue
        when = _parse_utc(bucket.get("reset_time"))
        if when is None or when <= now:
            continue
        hit.append((when, bucket))
    if not hit:
        return None
    hit.sort(key=lambda pair: pair[0])
    return hit[-1][0], [b for _, b in hit]   # 여럿이면 가장 늦게 풀리는 쪽까지 막힌다


def _fmt_until(reset) -> str:
    """`(49h20m 뒤 · 09-18 09:49 로컬)`. 남은 시간과 **절대 시각**을 함께 보여 준다.

    남은 시간만 적으면 기록 당시 값인지 지금 값인지 구분되지 않는다(캐시 단락의 본문에는
    기록 당시 원문이 그대로 실린다).
    """
    secs = max(0, int((reset - _utc_now()).total_seconds()))
    local = datetime.fromtimestamp(
        time.time() + secs).strftime("%m-%d %H:%M")
    return "%s 뒤 · %s 로컬" % (_fmt_hms(secs) or "0s", local)


def _quota_guard(fn, *args, **kwargs):
    """쿼터 상태 부작용은 **판정 · 종료 코드를 바꿀 수 없다**(1.5.0 상태 불변식).

    ⛔ `main` 은 예기치 못한 예외를 전부 `internal_error`(exit 1)로 바꾼다. 상태 기록 하나가
      깨끗한 exit 4("기다리면 풀린다")를 exit 1("스크립트 결함이다")로 뒤집으면 처방이
      정반대가 된다. 실측으로 확인했다 — 이 가드가 없을 때 가짜 agy 의 서명이 하나 달라진
      것만으로 결과 계약 매트릭스의 쿼터 행이 exit 1 이 됐다.
    ⚠ `except Exception` 이 넓은 것은 **의도**다. 이 경로에서 잃을 수 있는 것은 캐시뿐이고,
      잃지 말아야 할 것은 판정이다. 무엇이 터졌는지는 화면에 남긴다.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:                 # noqa: BLE001 — 위 docstring 참조
        _say_note("쿼터 상태를 다루지 못했다",
                  "%s: %s" % (exc.__class__.__name__, str(exc)[:200]),
                  "다음 실행이 같은 대기를 다시 쓴다. 판정과 종료 코드는 그대로다.")
        return None


def _quota_short_circuit(agy: str, model: str, cwd: str, state_path: Optional[str],
                         models: dict, calls: Optional[List[dict]] = None):
    """차단 기록이 있을 때만 `/quota` 로 확인한다. 아직 차단이면 `(해제 시각, 항목)`.

    ⛔ **차단 기록이 없으면 조회하지 않는다.** 조회는 토큰 0 이지만 실측 6초다. 차단되지
      않은 날이 대부분이므로 매 리뷰에 얹을 값이 아니다 — 첫 차단은 429 가 알려 준다
      (C1 수정으로 하드 상한에 걸린 경우까지 잡는다).
    반환이 None 이면 그대로 진행한다. 풀린 것이 확인되면 항목을 지운다 —
    **조회 성공이 곧 해제 신호다**(그래서 별도의 재탐색이 필요 없다).
    """
    entry = models.get(model)
    if not isinstance(entry, dict):
        return None
    cached_reset = _quota_entry_reset(entry)

    groups = _quota_query(agy, model, cwd, calls)
    if groups is not None:
        group = _quota_group_for_model(groups, model)
        if group is not None:
            blocked = _quota_blocked(group)
            if blocked is None:
                models.pop(model, None)          # 풀렸다 — 기록을 지우고 정상 진행
                _save_quota_state(state_path, models)
                return None
            reset, buckets = blocked
            entry["reset_at"] = reset.strftime(_UTC_FMT)
            entry["source"] = "quota_query"
            entry["cached_hits"] = int(entry.get("cached_hits") or 0) + 1
            entry.setdefault("blocked_since", entry.get("recorded_at") or _utc_now_str())
            models[model] = entry
            _save_quota_state(state_path, models)
            return reset, entry, "quota_query", buckets
        # 모델을 그룹에 대응시키지 못했다 — 막지 않고 저장된 값으로만 판단한다.
    if cached_reset is None:
        return None
    entry["cached_hits"] = int(entry.get("cached_hits") or 0) + 1
    models[model] = entry
    _save_quota_state(state_path, models)
    return cached_reset, entry, "cached", []


def _record_quota_block(agy: str, model: str, cwd: str, detail: str,
                        calls: Optional[List[dict]] = None) -> dict:
    """429 를 만났을 때 차단을 기록한다. 반환은 `_meta` 에 실을 필드.

    권위 있는 값을 먼저 노린다 — 이미 609초를 쓴 뒤라 6초짜리 `/quota` 는 무시할 만하고,
    그 대가로 **추정이 아닌** `reset_time` 을 저장한다. 조회가 답하지 못하면 오류 문자열의
    `Resets in …` 을 읽어 추정으로 기록하고(72시간 상한), 그것도 못 읽으면 보수적으로
    15분만 막는다 — 손익이 비대칭이다(최대 15분 대 매 실행 609초).
    """
    now = _utc_now()
    meta: dict = {}
    reset = None
    source = "error_hint"
    groups = _quota_query(agy, model, cwd, calls)
    if groups is not None:
        group = _quota_group_for_model(groups, model)
        if group is not None:
            blocked = _quota_blocked(group, now)
            if blocked is not None:
                reset, _ = blocked
                source = "quota_query"
            meta["quota_probe"] = "blocked" if blocked else "clear"
    if reset is None:
        secs = _quota_reset_seconds(detail)
        if secs is None:
            meta["quota_reset_raw"] = _first_line(detail)[:300]
            _say_note("쿼터 재설정 시각을 읽지 못했다",
                      "오류 문구에서 `Resets in …` 을 찾지 못했다.",
                      "보수적으로 15분만 막는다 — 그 뒤 실행은 다시 609초를 쓸 수 있다.")
            secs = 15 * 60
        reset = now + timedelta(seconds=min(secs, _QUOTA_MAX_HOURS * 3600))

    path = _quota_state_path()
    if path is None:
        _say_note("쿼터 차단을 기록하지 못했다", "상태 폴더를 믿을 수 없다.",
                  "다음 실행이 같은 대기를 다시 쓴다.")
        return meta
    models = _load_quota_state(path)
    previous = models.get(model) if isinstance(models.get(model), dict) else {}
    models[model] = {
        "reset_at": reset.strftime(_UTC_FMT), "recorded_at": _utc_now_str(),
        "source": source, "detail": _first_line(detail)[:300],
        "blocked_since": previous.get("blocked_since") or _utc_now_str(),
        "cached_hits": int(previous.get("cached_hits") or 0)}
    if not _save_quota_state(path, models):
        _say_note("쿼터 차단을 기록하지 못했다", "상태 파일에 쓰지 못했다.",
                  "다음 실행이 같은 대기를 다시 쓴다.")
    return meta


def _clear_quota_block(model: str) -> None:
    """그 모델의 호출이 `ok` 로 끝났을 때만 항목을 지운다.

    ⚠ 지우기 직전에 다시 읽어, 그 사이 다른 세션이 새로 기록한 차단을 덮지 않는다.
    """
    path = _quota_state_path()
    if path is None:
        return
    models = _load_quota_state(path)
    if models.pop(model, None) is not None:
        _save_quota_state(path, models)


_STATS_FILE_CAP = 5000           # mtime 최신순. 넘으면 잘렸다는 사실을 알린다
# 회차로 세는 mode — 리뷰를 위해 agy 에 **실제로 요청을 보낸** 실행이다.
#   `check` 는 진단이고, `quota_cached` 단락과 `internal_error` 는 요청을 보내지 않았다.
_STATS_ROUND_MODES = frozenset((
    "reviewed", "text_fallback", "parse_failed", "timeout", "quota_exhausted",
    "tool_unavailable", "review_unavailable", "primary_model_unavailable",
    "model_unavailable", "tool_error"))
_STATS_VERDICT_MODES = frozenset(("reviewed",))   # 통과율의 분모


def _stats_read(path: str) -> Optional[dict]:
    """결과 파일 하나에서 **집계에 필요한 필드만** 뽑는다. 읽지 못하면 None.

    ⛔ [Eng M5] 파일을 통째로 메모리에 올리면 `text_fallback` 의 `raw_text` 나 대량
      findings 가 섞였을 때 터진다. 개별 파일이 깨졌다고 전체 집계를 잃어서도 안 된다 —
      건너뛰고 "읽지 못한 N건" 으로 보고한다.
    """
    try:
        with io.open(path, encoding="utf-8") as fh:
            body = json.load(fh)
        meta = body.get("_meta")
        if not isinstance(meta, dict):
            return None
        scope = meta.get("scope") if isinstance(meta.get("scope"), dict) else {}
        calls = meta.get("calls") if isinstance(meta.get("calls"), list) else []
        usage: dict = {}
        incomplete = False
        for call in calls:
            if not isinstance(call, dict):
                continue
            if call.get("stage") in ("quota_query", "probe"):
                continue                 # 토큰이 없거나 무시할 만하다
            values = call.get("usage")
            if not isinstance(values, dict):
                incomplete = True
                continue
            for key, value in values.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                usage[str(key)] = usage.get(str(key), 0) + value
        return {
            "mode": meta.get("mode"), "exit_code": meta.get("exit_code"),
            "passed": bool(meta.get("passed")),
            "written_at": _parse_utc(meta.get("written_at")) or _file_time(path),
            "repo": scope.get("repo"), "branch": scope.get("branch"),
            "key_sha": scope.get("merge_base_sha") or scope.get("base_sha")
            or scope.get("head_sha"),
            "diff_sha256": meta.get("diff_sha256"),
            "diff_bytes": meta.get("diff_bytes"),
            "quota_cached": bool(meta.get("quota_cached")),
            "findings": len(body.get("findings") or []) if isinstance(
                body.get("findings"), list) else 0,
            "seconds": meta.get("elapsed_seconds"),
            "usage": usage, "usage_incomplete": incomplete}
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return None


def _file_time(path: str) -> Optional[datetime]:
    """1.4.x 파일에는 `written_at` 이 없다 — 파일명의 `YYYYMMDD_HHMMSS` 를 **로컬** 시각으로
    읽어 UTC 로 정규화한다(그 시절 코드가 `datetime.now()` 로 지었다).

    ⛔ [Eng H4] 읽는 즉시 UTC 로 맞추지 않으면 `written_at`(UTC)과 한 정렬·한 필터에서
      섞인다. CI 는 UTC 라 그 회귀가 **영원히 빨개지지 않는다.**
    """
    name = os.path.basename(path)
    m = re.search(r"(\d{8})_(\d{6})", name)
    if not m:
        return None
    try:
        local = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    # ⛔ [26.09.16 교차리뷰 HIGH] `time.timezone` 을 손으로 빼지 말 것. 그 값은 **UTC 기준
    #   서쪽으로의 초**라 KST 에서 -32400 이고, DST 분기까지 얹으면 부호를 뒤집기 쉽다 —
    #   실제로 그렇게 짰다가 KST 오전 10시가 UTC 19:00(18시간 미래)으로 나왔고, 그러면
    #   과거 결과가 미래로 둔갑해 `--since` 필터가 통째로 어긋난다.
    #   `mktime` → `utcfromtimestamp` 는 OS 에 맡기므로 DST 도 자동으로 맞는다.
    try:
        stamp = time.mktime(local.timetuple())
    except (OverflowError, ValueError):
        return None
    return datetime(1970, 1, 1) + timedelta(seconds=stamp)


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def _run_stats(args) -> int:
    """쌓인 결과 파일을 집계한다. **agy 를 부르지 않고 결과 파일도 쓰지 않는다.**

    종료 코드는 0(집계함 · 0건 포함) 또는 2(폴더를 믿을 수 없음)다. 개별 파일이 깨진 것은
    건너뛰고 "읽지 못한 N건" 으로 보고하며 0 을 유지한다 — 파일 하나로 전체를 잃지 않는다.
    """
    if args.stats_dir:
        base = os.path.abspath(os.path.expanduser(args.stats_dir))
        if not os.path.isdir(base):
            _safe_print("⛔ --stats-dir 경로가 폴더가 아니다: %s" % base)
            return EXIT_TOOL_ERROR
    else:
        base, status = _state_dir(create=False)
        if base is None or status == "unsafe":
            _safe_print("⛔ 결과 폴더를 믿을 수 없다(링크 · 남의 소유 · 읽기 실패).")
            _safe_print("   --stats-dir 로 폴더를 직접 지정하거나 폴더 권한을 확인할 것.")
            return EXIT_TOOL_ERROR

    since = _utc_now() - timedelta(seconds=_duration_seconds(args.since, 14 * 86400))
    try:
        names = [n for n in os.listdir(base)
                 if n.startswith("gemini_review_") and n.endswith(".json")]
    except OSError:
        names = []
    if not names:
        _safe_print("결과 0건 — 집계할 것이 없다 (%s)" % base)
        _safe_print("   `--out` 으로 다른 경로에 쓴 결과는 집계에 들어가지 않는다.")
        return EXIT_PASSED

    paths = [os.path.join(base, n) for n in names]
    truncated = 0
    if len(paths) > _STATS_FILE_CAP:
        paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        truncated = len(paths) - _STATS_FILE_CAP
        paths = paths[:_STATS_FILE_CAP]

    rows, unreadable = [], 0
    for path in paths:
        row = _stats_read(path)
        if row is None:
            unreadable += 1
            continue
        if row["written_at"] is None or row["written_at"] >= since:
            rows.append(row)

    rounds = [r for r in rows if r["mode"] in _STATS_ROUND_MODES and not r["quota_cached"]]
    judged = [r for r in rounds if r["mode"] in _STATS_VERDICT_MODES]
    cached = [r for r in rows if r["quota_cached"]]
    blocks = [r for r in rounds if r["mode"] == "quota_exhausted"]
    fallbacks = [r for r in rows if r["mode"] == "fallback_reviewed"]

    _safe_print("gemini-review --stats · 최근 %s · %s" % (args.since, base))
    _safe_print("")
    _safe_print("회차 %d · 판정이 난 회차 %d · 통과 %d (%s)"
                % (len(rounds), len(judged), len([r for r in judged if r["passed"]]),
                   _pct(len([r for r in judged if r["passed"]]), len(judged))))
    _safe_print("쿼터 차단 %d회 · 캐시 단락 %d회 · 대체 리뷰 기록 %d건 · 읽지 못한 파일 %d건%s"
                % (len(blocks), len(cached), len(fallbacks), unreadable,
                   " · 상한으로 잘린 %d건" % truncated if truncated else ""))

    # 루프 키별 회차 — 커밋 단위로 묶는다. 1.4.x 파일은 키가 없어 따로 센다.
    loops: dict = {}
    keyless = 0
    for row in rounds:
        if not row["repo"] or not row["key_sha"]:
            keyless += 1
            continue
        loops.setdefault((row["repo"], row["branch"], row["key_sha"]), []).append(row)
    if loops:
        sizes = [len(v) for v in loops.values()]
        singles = len([1 for n in sizes if n == 1])
        _safe_print("")
        _safe_print("루프 %d개 · 회차 중앙값 %s · p90 %s · 최대 %d"
                    % (len(loops), _median(sizes), _percentile(sizes, 90), max(sizes)))
        # ⚠ 쪼개짐 지표를 **p90 보다 먼저** 본다. 기본 실행(`--base HEAD~1`)에서 수정 커밋을
        #   쌓으면 키가 회차마다 갈려 회차를 과소 계상한다(Eng M6).
        _safe_print("  키 1회짜리 루프 %d개 (%s) — 이 값이 크면 위 회차 수를 믿지 말 것"
                    % (singles, _pct(singles, len(loops))))
        grew, fixed = _loop_shapes(loops)
        _safe_print("  범위 증가형 %d · 범위 고정형 %d — 증가형은 수정이 새 결함을 낳는 쪽,"
                    % (grew, fixed))
        _safe_print("  고정형은 한 번에 다 못 찾는 쪽이다(처방이 다르다)")
    if keyless:
        _safe_print("  키 없음 %d회 (1.4.x 결과 — repo · branch 가 없다)" % keyless)

    # ⚠ 해시가 있는 회차끼리만 센다. 1.4.x 결과에는 `diff_sha256` 이 없어, 전체 회차 수에서
    #   빼면 "전부 반복" 이라는 거짓이 나온다(실측으로 확인했다).
    hashed = [r["diff_sha256"] for r in rounds if r["diff_sha256"]]
    repeats = len(hashed) - len(set(hashed))
    if repeats > 0:
        _safe_print("  같은 diff 를 다시 리뷰한 회차 %d (코드를 안 고치고 돌렸다)" % repeats)

    secs = [r["seconds"] for r in rounds if isinstance(r["seconds"], (int, float))]
    if secs:
        _safe_print("")
        _safe_print("소요 중앙값 %.0f초 · 최대 %.0f초" % (_median(secs), max(secs)))

    keys = sorted(set(k for r in rounds for k in r["usage"]))
    incomplete = len([1 for r in rounds if r["usage_incomplete"]])
    _safe_print("")
    if not keys:
        _safe_print("리뷰당 토큰: 기록 없음 (1.4.x 결과이거나 래퍼에 usage 가 없었다)")
    else:
        for key in keys:
            values = [r["usage"][key] for r in rounds if key in r["usage"]]
            _safe_print("  %-18s 중앙값 %s · 최대 %s (%d회)"
                        % (key, _median(values), max(values), len(values)))
    if incomplete:
        _safe_print("  ⚠ 토큰 불완전 %d회 (텍스트 재시도 · 하드 상한 등으로 래퍼가 없었다)"
                    % incomplete)
    return EXIT_PASSED


def _loop_shapes(loops: dict):
    """루프를 **범위 증가형 · 고정형**으로 가른다. 반환 `(증가, 고정)`.

    OQ6 분류(2026-09-16)에서 이것이 원인을 가르는 유일하게 확실한 손잡이였다 —
    범위가 커지는 루프는 (ii) 수정이 만든 결함이, 고정된 루프는 (i) 처음부터 있던 결함이
    지배한다(실측 27% 대 67%). 처방이 다르므로 섞어 세면 안 된다.
    """
    grew = fixed = 0
    for rows in loops.values():
        sizes = [r["diff_bytes"] for r in sorted(
            rows, key=lambda r: r["written_at"] or datetime.min)
            if isinstance(r["diff_bytes"], int)]
        if len(sizes) < 2:
            continue
        # 마지막이 처음보다 1.5배 넘게 커졌으면 "범위가 자란 루프" 로 본다.
        (grew, fixed) = (grew + 1, fixed) if sizes[-1] > sizes[0] * 1.5 else (grew, fixed + 1)
    return grew, fixed


def _pct(part: int, whole: int) -> str:
    return "%.0f%%" % (100.0 * part / whole) if whole else "-"


def _write_json_atomic(path: str, payload: dict) -> None:
    """같은 폴더의 임시 파일에 쓰고 `os.replace` 로 바꿔 끼운다.

    ⛔ [26.09.14 실측] `open(path, "w")` 로 곧장 쓰면, 기록 도중 종료(신호 ·
      예외)되었을 때 **잘린 JSON** 이 남는다(`JSONDecodeError`).
    ⚠ 임시 파일은 `mkstemp` 라 0600 이다. 바꿔 끼운 결과 파일도 0600 이 된다
      (코드가 인용된 결과이므로 의도다).
    """
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".gemini_review_", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _AgyRun(object):
    """agy 호출 한 번의 결과. **`_run_agy` 만 만든다.**

    `rc` 는 agy 의 종료 코드, 프로세스를 끝까지 기다리지 못했으면 None 이다
    (`hard_timeout` · `error` 중 하나가 원인을 말한다).
    """

    def __init__(self, rc, stdout="", stderr="", elapsed=0.0, hard_limit=0,
                 hard_timeout=False, error=""):
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.elapsed = elapsed
        self.hard_limit = hard_limit
        self.hard_timeout = hard_timeout
        self.error = error


_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _utc_now() -> datetime:
    """시각 비교의 **유일한** 기준(1.5.0).

    ⛔ 이 파일의 다른 시각은 전부 로컬이다(`started_at` · 결과 파일명). `_meta.written_at` 과
      쿼터 상태는 UTC 로 적으므로, 한 곳에서만 만들지 않으면 KST 에서 9시간 어긋난다.
    ⚠ `datetime.utcnow()` 는 3.12 가 경고를 낸다 — aware 로 만들고 tzinfo 를 떼어 쓴다.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_now_str() -> str:
    return _utc_now().strftime(_UTC_FMT)


def _parse_utc(text) -> Optional[datetime]:
    """`2026-09-23T02:30:41Z` → naive UTC. 읽지 못하면 None.

    ⛔ `datetime.fromisoformat` 을 쓰지 말 것 — 3.7~3.10 이 `Z` 접미사를 거부해 **조용히**
      실패한다. CI 와 개발 기기가 3.12 라 그 회귀는 테스트로 잡히지 않는다.
    """
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text.strip(), _UTC_FMT)
    except ValueError:
        return None


def _diff_bytes(diff: str) -> bytes:
    """리뷰에 **실제로 보낸 바이트**. 해시와 파일 쓰기가 같은 것을 쓰게 한 곳에 둔다.

    ⚠ 이것은 `git diff` 의 원본 바이트가 아니라 **스크립트가 디코딩·재인코딩한 UTF-8** 이다
      (`_git` 이 `utf-8/replace` 로 읽는다). 나중에 커밋 게이트 hook 을
      `git diff | sha256sum` 으로 짜면 값이 맞지 않는다 — 같은 헬퍼를 쓸 것.
    """
    return diff.encode("utf-8", "surrogateescape")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_stream(data) -> str:
    """자식 프로세스의 stdout · stderr 를 문자열로. 하드 상한에 걸린 호출은 None 일 수 있다.

    `subprocess.TimeoutExpired` 의 `stdout` · `stderr` 는 아무것도 못 읽었으면 None 이고,
    `subprocess.run` 의 정상 반환은 항상 bytes 다. 두 자리가 같은 규칙을 쓰게 모아 둔다.
    """
    if not data:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace").strip()
    return str(data).strip()


def _run_agy(agy: str, model: str, extra: List[str], cwd: str,
             timeout_spec: str, default_secs: int, margin: int = None) -> _AgyRun:
    """agy 를 부르는 **유일한** 자리. `--mode plan` · stdin 차단 · 바깥 하드 상한을 여기서만 건다.

    ⛔ [26.09.14 Eng 교차리뷰 S5] 종전에는 세 함수(구조화 · 생존 확인 · 텍스트 재시도)가
      각자 `subprocess.run` · 상한 · `--mode plan` 을 적었다. "종료 코드를 안 본다" 결함이
      그 세 자리에서 **한 번씩 따로** 났다 — 모아 두면 빠질 자리가 하나다.
    ⚠ `extra` 에 `--mode` 를 넣지 말 것. 두 번 주면 어느 값이 이기는지 agy 에 달린다
      (테스트가 명령마다 `--mode plan` 이 정확히 한 번인지 본다).
    ⚠ 바깥 상한은 agy 자신의 `--print-timeout` 보다 넉넉하다(`_HARD_TIMEOUT_MARGIN`).
      응답 불능이 의심되는 도구에게 상한을 맡기지 않기 위한 것이다(26.09.10).
    """
    cmd = [agy, "--mode", "plan",         # ★ read-only 고정 (협상 대상 아님)
           "--model", model] + list(extra)
    # ⚠ [1.5.0] `margin` 은 **agy 에 턴을 맡기지 않는 호출**만 줄인다(`/quota` 같은 읽기 전용
    #   슬래시 명령). 모델이 도는 호출에서 여유를 줄이면 정상 응답을 잘라 버린다.
    hard = _duration_seconds(timeout_spec, default_secs) + (
        _HARD_TIMEOUT_MARGIN if margin is None else margin)
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, check=False,
                              stdin=subprocess.DEVNULL, timeout=hard)
    except subprocess.TimeoutExpired as exc:
        # ⛔ [1.5.0 C1] 부분 출력을 버리지 않는다. 쿼터에 걸린 agy 는 내부에서 8회까지
        #   재시도하므로(609초 실측, 기본 하드 상한 720초 — 여유 111초) 조금만 더 끌면
        #   쿼터가 이 갈래로 떨어진다. 버리면 `_classify_run` 이 `timeout` 으로 뭉쳐
        #   쿼터 기록이 남지 않고 **다음 실행이 같은 720초를 또 태운다.**
        return _AgyRun(None, stdout=_decode_stream(exc.stdout),
                       stderr=_decode_stream(exc.stderr),
                       elapsed=time.time() - started, hard_limit=hard,
                       hard_timeout=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return _AgyRun(None, elapsed=time.time() - started, hard_limit=hard,
                       error=str(exc) or exc.__class__.__name__)
    return _AgyRun(proc.returncode,
                   stdout=_decode_stream(proc.stdout),
                   stderr=_decode_stream(proc.stderr),
                   elapsed=time.time() - started, hard_limit=hard)


def _invoke_schema(agy: str, model: str, args, root: str, schema_path: str,
                   prompt: str) -> _AgyRun:
    """스키마 강제 호출 1회. **판정은 호출부가 `_classify_run` 으로 한다.**

    ⚠ **소요를 재는 것이 이 호출의 절반**이다 [26.09.10]. 종전에는 호출 시간이
      화면에 남지 않아, 25~43초가 400~550초로 열화된 사실을 사람이 알아채지
      못했다. 소요는 `_AgyRun.elapsed` 로 돌아가 화면 · `_meta.calls` 에 남는다.
    """
    return _run_agy(agy, model, [
        "--output-format", "json",
        "--json-schema", schema_path,
        "--print-timeout", args.timeout,
        "--add-dir", root,
        "-p", prompt,
    ], root, args.timeout, 600)


def _classify_run(run: _AgyRun, structured: bool) -> Tuple[str, str, str]:
    """agy 호출 한 번의 **원인**. 반환 `(원인, 시간 초과 종류, 설명 한 줄)`.

    원인은 `ok` · `empty` · `tool_denied` · `timeout` · `quota` · `model_unavailable` ·
    `tool_error` 중 하나다. 시간 초과 종류는 `agy`(agy 가 알림) · `hard`(래퍼가 끊음).

    ⛔ [1.4.0] 1.3.x 는 시간 초과 · 도구 권한 거부 · 진짜 빈 응답을 모두 **"빈 응답"** 으로,
      쿼터 소진을 **"도구 오류"** 로 뭉쳤다. 원인마다 할 일이 다르다 — 시간 초과면 같은
      모델로 곧바로 재시도하지 않고, 쿼터면 재설정 시각까지 기다리고, 모델명 오류면
      이름을 고친다. 판정 순서가 곧 우선순위다:
      1. 끝까지 못 기다렸다 → `timeout`(hard) · `tool_error`(실행 실패)
      2. agy 가 실패를 알렸다(rc≠0 · 래퍼 status=ERROR) → `quota` · `model_unavailable` ·
         `tool_error`. ⛔ stdout 이 무엇이든 실패다 [26.09.14 실측: 없는 모델명이 빈 응답으로
         읽혀 폴백 판정 exit 0 까지 갔다 · 인증 오류 평문이 파싱 실패 exit 1 이 됐다].
      3. 출력 시간 초과 안내 → `timeout`(agy). ⚠ agy 는 이때 **exit 0** 이고 안내는 stderr
         로 나간다(26.09.14 실측).
      4. 응답 본문이 비었다 → `tool_denied`(stderr 에 권한 거부 안내) · `empty`.
    """
    if run.hard_timeout:
        # ⛔ [1.5.0 C1] 하드 상한에 걸려도 **쿼터를 먼저 본다.** 쿼터에 걸린 agy 는 내부에서
        #   재시도를 쌓으므로 가장 비싼 경우가 바로 이 갈래로 온다(609초 실측 대 720초 상한).
        #   여기서 `timeout` 으로 뭉치면 쿼터 기록이 남지 않아 다음 실행이 또 태운다.
        detail = _agy_error_detail(run.stdout, run.stderr)
        if _looks_like_quota_error(detail):
            return "quota", "", _first_line(detail)
        return "timeout", "hard", "바깥 상한 %d초 초과로 강제 종료" % run.hard_limit
    if run.rc is None:
        return "tool_error", "", "agy 실행 실패: %s" % (run.error or "원인 불명")
    raw, err = run.stdout, run.stderr
    if run.rc != 0 or (structured and _wrapper_status(raw) == "ERROR"):
        detail = _agy_error_detail(raw, err)
        if _looks_like_quota_error(detail):
            return "quota", "", _first_line(detail)
        if _looks_like_model_error(detail):
            return "model_unavailable", "", _first_line(detail)
        return "tool_error", "", _first_line(detail) or "agy 종료코드 %d" % run.rc
    if _is_print_timeout(err):
        return "timeout", "agy", _first_line(
            [ln for ln in err.splitlines() if _is_print_timeout(ln)][0])
    if not structured and _has_agy_timeout_line(raw):
        return "timeout", "agy", _first_line(raw.splitlines()[-1])
    body = _response_text(raw) if structured else raw
    if not body.strip():
        denied = _denied_tool_notice(err)
        return ("tool_denied", "", denied) if denied else ("empty", "", "")
    return "ok", "", ""


def _first_line(text: str) -> str:
    for ln in (text or "").splitlines():
        if ln.strip():
            return ln.strip()[:300]
    return ""


def _response_text(raw: str) -> str:
    """구조화 호출의 응답 본문. agy 래퍼면 `response` 필드, 래퍼가 아니면 원문 그대로."""
    try:
        wrapper = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(wrapper, dict) and "response" in wrapper:
        return str(wrapper.get("response") or "")
    return raw


def _looks_like_quota_error(detail: str) -> bool:
    """구독 사용량 한도 안내인가.

    실측 문구(26.09.14, agy exit 1 · 래퍼 status=ERROR): `Individual quota reached. Please
    upgrade your subscription to increase your limits. Resets in 23m10s.` — 1.3.x 는 이것을
    "도구 오류"(exit 2)로 적어, 설정을 고치러 가게 만들었다. 기다리면 풀린다.
    """
    lowered = (detail or "").lower()
    return "quota" in lowered or "resource_exhausted" in lowered


_QUOTA_RESET = re.compile(r"resets in\s+([0-9][0-9hms.]*[hms])", re.I)


_HMS = re.compile(r"^(?:([0-9]+)h)?(?:([0-9]+)m)?(?:([0-9]+)s)?$")


def _parse_hms(text: str) -> Optional[int]:
    """`63h45m21s` → 229521. 문법에 맞지 않으면 None.

    ⛔ 최소 한 단위를 **강제**한다 — `^(\\d+h)?(\\d+m)?(\\d+s)?$` 는 빈 문자열에도 매치해서
      거부해야 할 입력에 0 을 주는 죽은 가드가 된다(Eng M8).
    ⚠ `\\d` 가 아니라 `[0-9]` 를 쓴다 — `\\d` 는 유니코드 숫자(아라비아-인도 숫자 등)도 잡는다.
    ⚠ 기존 `_duration_seconds` 와 합치지 않는다. 그쪽은 `--timeout` 의 단일 단위 표기를 읽고
      **읽지 못하면 기본값을 돌려준다.** 여기서는 읽지 못한 것을 반드시 알아야 한다 —
      기본값을 받으면 쿼터 차단을 엉뚱한 시각까지로 기록한다.
    """
    m = _HMS.match((text or "").strip())
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(g or 0) for g in m.groups())
    return h * 3600 + mi * 60 + s


def _fmt_hms(secs: Optional[int]) -> str:
    """`229521` → `63h45m21s`. 앞자리 0 단위는 뺀다(`23m10s` · `49h20m10s`)."""
    if secs is None or secs < 0:
        return ""
    h, rest = divmod(int(secs), 3600)
    mi, s = divmod(rest, 60)
    parts = []
    if h:
        parts.append("%dh" % h)
    if mi or h:
        parts.append("%dm" % mi)
    parts.append("%ds" % s)
    return "".join(parts)


def _quota_reset_seconds(detail: str) -> Optional[int]:
    """쿼터 안내에서 재설정까지 남은 **초**. 읽지 못하면 None.

    화면의 `retry_after` 와 상태 파일의 추정 기록이 **같은 함수**를 쓰게 한 곳에 둔다 —
    두 경로가 서로 다른 값을 내면 사용자가 어느 쪽을 믿을지 알 수 없다.
    """
    m = _QUOTA_RESET.search(detail or "")
    return _parse_hms(m.group(1)) if m else None


def _quota_reset_hint(detail: str) -> str:
    """쿼터 안내의 재설정까지 남은 시간(`23m10s`). 없거나 읽지 못하면 빈 문자열."""
    return _fmt_hms(_quota_reset_seconds(detail))


def _wrapper_status(raw: str) -> str:
    """agy JSON 래퍼의 `status`(대문자). 래퍼가 아니면 빈 문자열."""
    try:
        wrapper = json.loads(raw)
    except ValueError:
        return ""
    if isinstance(wrapper, dict):
        return str(wrapper.get("status") or "").upper()
    return ""


def _agy_error_detail(raw: str, err: str) -> str:
    """agy 가 실패를 알린 문구. 래퍼 JSON 의 `error` 를 우선한다."""
    try:
        wrapper = json.loads(raw)
    except ValueError:
        wrapper = None
    if isinstance(wrapper, dict) and wrapper.get("error"):
        return str(wrapper["error"]).strip()
    return (err or raw or "").strip()


def _looks_like_model_error(detail: str) -> bool:
    lowered = (detail or "").lower()
    return ("invalid model selection" in lowered
            or "not recognized as a known model" in lowered)


def _empty_info(raw: str) -> Optional[dict]:
    """agy 래퍼가 **빈 응답**을 담고 있으면 진단용 dict, 아니면 None.

    래퍼를 먼저 보는 이유는 원인을 오해하지 않기 위해서다 — 실측은
    `status=SUCCESS` 인데 `response=""` 이고 output 14077 · thinking 13849
    이었다(생각은 했으나 전달 실패). 이것을 '파싱 실패' 로 뭉뚱그리면 진단이
    통째로 빗나간다.
    """
    try:
        wrapper = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(wrapper, dict) or "response" not in wrapper:
        return None
    if str(wrapper.get("response") or "").strip():
        return None
    usage = wrapper.get("usage") or {}
    return {
        "status": wrapper.get("status"),
        "duration_seconds": wrapper.get("duration_seconds") or 0,
        "output_tokens": usage.get("output_tokens"),
        "thinking_tokens": usage.get("thinking_tokens"),
    }


def _probe_alive(agy: str, model: str, root: str) -> Tuple[bool, _AgyRun]:
    """agy 계층이 **한 줄 프롬프트**에 응답하는가. 반환 `(살아있음, 호출 결과)`.

    이 저장소도 diff 도 읽지 않는 최소 호출이다 — 그래야 결과가 '계층 생존'
    하나만 뜻한다. 실패하면 재시도(폴백 모델 · 텍스트 모드)는 전부 낭비다.

    ⚠ 살아 있다고 해서 긴 프롬프트가 된다는 뜻은 아니다. 이 확인은 **한쪽
      방향으로만** 결정적이다 — 죽어 있으면 재시도가 무의미하다는 것.
    """
    run = _run_agy(agy, model, [
        "--output-format", "text",
        "--print-timeout", _PROBE_TIMEOUT,
        "-p", _PROBE_PROMPT,
    ], root, _PROBE_TIMEOUT, 60)
    # 상한을 넘겼거나 실행 자체가 안 됐으면(rc None) '응답하지 않는다' 는 답이다.
    # ⛔ [26.09.14 Eng 교차리뷰] 종전에는 stdout 만 봤다 — `_retry_as_text` ·
    #   `_invoke_schema` 에서 고친 "종료 코드를 안 본다" 계열의 세 번째 자리다.
    #   에러 문구에 OK 라는 낱말이 섞이면 살아 있다고 판정해 긴 재시도로 넘어간다.
    alive = (run.rc == 0
             and not _is_print_timeout(run.stderr)
             and _probe_says_ok(run.stdout))
    return alive, run


def _probe_says_ok(text: str) -> bool:
    """생존 확인 응답이 **실제 답변**인가.

    ⛔ '출력이 비지 않았다' 를 생존으로 읽으면 안 된다. agy 는 응답을 못 받아도
      안내문을 뱉는다 — 실측 문구가
      `[agy] print timeout after 2m0s with turn in progress; returning partial
      output` 이다. 그것을 생존으로 세면 계층 장애일 때 재시도로 넘어가고,
      이 확인이 막으려던 낭비가 그대로 재발한다(그날 400~550초 × 4회).
    ⚠ [26.09.14 실측 · 리눅스] 그 안내문은 **stderr** 로 나가고 exit 0 이었다.
      이 함수는 stdout 만 받으므로 그때는 위의 빈 출력 갈래에서 걸린다. 안내문
      검사는 부분 출력과 안내문이 stdout 에 섞이는 환경을 위해 남긴다.
    """
    body = (text or "").strip()
    if not body:
        return False
    if _is_print_timeout(body):
        return False
    return any(w.strip(".,!?:;\"'`*").upper() == "OK" for w in body.split())


def _denied_tool_notice(err: str) -> str:
    """agy stderr 의 **도구 권한 자동 거부** 안내 첫 줄. 없으면 빈 문자열.

    실측 문구(26.09.14): `jetski: no output produced — a tool required the "command"
    permission that headless mode cannot prompt for, so it was auto-denied. …`
    ⚠ 이 경우 exit 0 · `status=SUCCESS` · `response=""` 라 종료 코드로는 구분되지 않는다.
      화면에 원인이 없으면 "계층은 살아 있는데 빈 응답" 으로만 보여 원인을 못 짚는다.
    """
    for line in (err or "").splitlines():
        lowered = line.lower()
        if "permission" in lowered and ("auto-denied" in lowered or "cannot prompt" in lowered):
            return line.strip()[:300]
    return ""


def _is_print_timeout(text: str) -> bool:
    """agy 의 **출력 시간 초과 안내문**인가.

    실측 문구: `[agy] print timeout after 1s with turn in progress; returning
    partial output`.
    ⚠ [26.09.14 실측] 이때 agy 의 **종료 코드는 0** 이고, 안내문은 **stderr**
      로 나간다(stdout 에는 그때까지의 부분 출력만 남는다). 종료 코드만 보는
      판정은 이 경우를 성공으로 읽는다.
    """
    lowered = (text or "").lower()
    return "print timeout" in lowered or "turn in progress" in lowered


# agy 안내문은 `[agy] print timeout after <시간> with turn in progress` 꼴의 **한 줄**이다.
_AGY_TIMEOUT_LINE = re.compile(
    r"^\[agy\] print timeout after \S+ with turn in progress\b", re.I)


def _has_agy_timeout_line(text: str) -> bool:
    """본문(stdout) 안에 agy 의 시간 초과 **안내 줄**이 섞였는가.

    ⚠ [26.09.14 Gemini 교차리뷰 지적 — 절반만 반영] stderr 가 stdout 에 섞이는
      환경이면 안내문이 본문으로 들어온다. 그런데 본문에 `_is_print_timeout` 을
      그대로 쓰면 **이 저장소 코드를 리뷰한 정상 결과**가 "print timeout" 이라는
      말을 인용하는 순간 리뷰를 버린다(오탐 → exit 4). 그래서 본문에서는
      `[agy] print timeout` 으로 **시작하는 줄**만 안내문으로 본다.
    ⚠ [26.09.14 Gemini 교차리뷰 2회차 MEDIUM] 여러 줄 매칭이면 리뷰 **중간**의
      어떤 줄이 그 문구로 시작하기만 해도 버린다. 안내문은 시간 초과 순간
      그때까지의 출력 **뒤에** 붙으므로, 비어 있지 않은 **마지막 줄**만 보고
      실측 문구 꼴 전체(`after <시간> with turn in progress`)를 요구한다.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return bool(lines) and bool(_AGY_TIMEOUT_LINE.match(lines[-1]))


def _retry_as_text(agy: str, model: str, args, root: str, diff_path: str,
                   files: List[str], project_ctx: str
                   ) -> Tuple[str, _AgyRun, str, str, str]:
    """스키마 없이 텍스트로 재요청 — 형식을 포기하고 내용을 건진다.

    `--json-schema` 강제가 빈 응답을 유발하는 경우가 실제로 있다(진단 확인).
    구조화 파싱은 포기하되 지적 자체는 받아야 하므로, 텍스트 형식을 명시적으로
    지시해 사람이 읽을 수 있게 받는다.

    반환: `(리뷰 원문, 호출 결과, 원인, 시간 초과 종류, 설명)`. **리뷰를 받지 못했으면
    원문은 빈 문자열**이다 — 호출부가 원인을 보고 종료 코드를 정한다. agy 가 무언가
    출력했다는 것만으로는 리뷰를 받은 것이 아니다(아래 두 갈래).
    """
    prompt = _build_prompt(diff_path, files, project_ctx).replace(
        "지정된 JSON 스키마로만 출력하라.",
        "다음 형식의 **일반 텍스트**로 답하라 (JSON 금지):\n"
        "판정: approve | approve_with_comments | request_changes\n"
        "요약: <한두 문장>\n"
        "그리고 지적마다:\n"
        "  [심각도] 제목\n"
        "  위치: 파일:줄\n"
        "  내용: <설명>\n"
        "  실패 시나리오: <구체적 입력/상태 → 잘못된 결과>\n"
        "지적이 없으면 '지적 사항 없음' 한 줄만 쓰라.",
    )
    run = _run_agy(agy, model, [
        "--output-format", "text",
        "--print-timeout", args.timeout,
        "--add-dir", root,
        "-p", prompt,
    ], root, args.timeout, 600)
    # ⚠ [26.08.27 교차리뷰 지적] 종료 코드를 보지 않으면, agy 가 타임아웃·인증
    #   오류로 죽으며 남긴 **에러 문구를 리뷰 원문으로 저장**한다. 그러면 exit 6
    #   (구조화 실패)이 되어 "리뷰는 받았다" 로 읽힌다. 실패는 실패로 돌린다.
    # ⛔ [26.09.14] **이 검사는 v1.3.0 병합에서 한 번 사라졌다.** 테스트가 파일
    #   전체에서 `proc.returncode != 0` 문자열을 찾았는데 `_invoke_schema` 에 같은
    #   문자열이 있어 두 판 동안 초록이었다. 실측: exit 1 + stdout `Error:
    #   authentication required…` 가 그대로 exit 6 의 리뷰 원문이 됐다.
    #   → 가드: `tests/test_gemini_review.py::_check_retry_as_text_rejects_failed_agy`
    #     (문자열이 아니라 이 함수를 가짜 agy 로 직접 돌린다).
    # ⛔ [26.09.14 실측] **출력 시간 초과는 exit 0 이다** (`_is_print_timeout`).
    #   stdout 에는 그때까지의 부분 출력이 남으므로 종료 코드만 보면 **잘린
    #   리뷰**가 온전한 리뷰로 저장된다 — 판정 줄까지만 오고 지적이 잘리면
    #   사람이 읽어도 통과로 읽힌다. 부분이라도 건질지 고민했으나, 어디서
    #   잘렸는지 알 수 없는 원문은 '지적 없음' 과 구분되지 않아 버린다.
    # [1.4.0] 두 판정은 이제 `_classify_run` 한 곳에 있다(구조화 호출과 같은 규칙).
    cause, kind, detail = _classify_run(run, structured=False)
    text = run.stdout if cause == "ok" else ""
    if cause == "ok":
        note = "응답 있음"
    elif cause == "timeout" and kind == "hard":
        note = "%d초 초과로 강제 종료" % run.hard_limit
    elif cause == "timeout":
        note = "agy 출력 시간 초과 (부분 출력 %d자) — 잘린 리뷰라 쓰지 않는다" % len(run.stdout)
    elif cause == "tool_denied":
        note = "빈 응답 — 도구 권한 거부: %s" % detail
    elif cause == "empty":
        note = "빈 응답"
    else:
        note = "agy 종료코드 %s — 리뷰로 쓰지 않는다 (%s)" % (
            "없음" if run.rc is None else run.rc, detail)
    _safe_print("   텍스트 재시도: %s · %.0f초 · %s" % (model, run.elapsed, note))
    return text, run, cause, kind, detail


def _looks_like_review(d) -> bool:
    """리뷰 결과인지 판별.

    ⚠ **JSON 스키마 조각과 반드시 구분해야 한다.** 스키마의 `properties` 내부
    객체에도 `findings` 키가 있어서, `"findings" in d` 만으로 판정하면 그것을
    결과로 오인한다(실측: 판정란에 `{'type':'string','enum':[...]}` 출력 후
    렌더링 중 AttributeError).

    ⚠ `findings` 배열만 보는 것도 부족하다 — LLM 은 본문에서 `{"findings": []}`
    같은 **예시용 더미 JSON** 을 흔히 만든다. 그것을 집으면 뒤에 오는 진짜
    리뷰가 조용히 버려진다.
    → 스키마 `required` 3종(`verdict`·`summary`·`findings`)을 **모두** 요구한다.
    """
    if not isinstance(d, dict):
        return False
    if not isinstance(d.get("verdict"), str) or not d["verdict"].strip():
        return False
    if not isinstance(d.get("summary"), str):
        return False
    findings = d.get("findings")
    if not isinstance(findings, list):
        return False
    return all(isinstance(x, dict) for x in findings)


_WRAPPER_KEYS = ("response", "result", "output", "content")


def _extract_json(raw: str):
    """agy 출력에서 리뷰 JSON 을 **하나만** 뽑는다. 애매하면 None(→ exit 1).

    ⛔ [26.09.14 Eng 교차리뷰] 종전에는 래퍼 **전체**를 훑어 **마지막** 리뷰 모양
      후보를 채택했다. diff 에 `{"verdict": "approve", …}` 를 심어 두면 그 문자열이
      래퍼 뒤쪽 필드나 응답 꼬리에 실릴 때 진짜 판정을 제칠 수 있다. 판정은 이
      도구의 게이트라, 애매하면 **판단하지 않는다.**
    → 래퍼가 있으면 응답 필드 **하나만** 본다. 필드 전체가 리뷰 JSON 이면 그것,
      아니면 본문에서 리뷰 모양 후보를 모아 **서로 다른 것이 정확히 하나**일 때만.
    ⚠ 종전 docstring 의 "LLM 이 사고 과정에서 만든 예시 JSON 이 앞에 오면 마지막을
      쓴다" 는 규칙은 버렸다. 스키마 강제 출력(`--json-schema`)에서는 응답 필드 전체가
      리뷰 JSON 이라 첫 갈래에서 끝나고, 예시와 진짜가 섞인 애매한 경우는 판정을
      지어내기보다 exit 1 이 옳다.
    """
    try:
        wrapper = json.loads(raw)
    except ValueError:
        wrapper = None
    if _looks_like_review(wrapper):
        return wrapper
    if isinstance(wrapper, dict):
        # ⚠ 처음으로 **존재하는** 응답 키 하나만 본다 — 거기서 못 찾아도 다음 키로 넘어가지
        #   않는다. 넘어가면 다른 필드에 심은 리뷰 JSON 을 고르는 경로가 다시 열린다.
        #   [26.09.14 교차리뷰 "첫 키에서 못 찾으면 다음 키도 보라"(HIGH) 는 이 이유로 기각.
        #    실측 agy 래퍼 키는 conversation_id · status · response · duration_seconds ·
        #    num_turns · usage · error 뿐이다. 나머지 키는 래퍼 모양이 바뀔 때를 위한 것.]
        for key in _WRAPPER_KEYS:
            if key in wrapper:
                inner = wrapper[key]
                if _looks_like_review(inner):
                    return inner
                return _single_review(inner) if isinstance(inner, str) else None
        return None
    return _single_review(raw)


def _single_review(text: str):
    """본문에 리뷰 JSON 이 **정확히 하나**면 그것, 아니면 None."""
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    if _looks_like_review(whole):
        return whole
    found = []
    for cand in _json_candidates(text):
        if _looks_like_review(cand) and cand not in found:
            found.append(cand)
    return found[0] if len(found) == 1 else None


def _json_candidates(raw: str):
    try:
        yield json.loads(raw)
    except ValueError:
        pass
    start = raw.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        yield json.loads(raw[start:i + 1])
                    except ValueError:
                        pass
                    break
        start = raw.find("{", start + 1)


_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_SEV_MARK = {"critical": "[CRITICAL]", "high": "[HIGH]",
             "medium": "[MEDIUM]", "low": "[LOW]"}

# 종료 코드. [26.08.25] 5 · 6 을 뒤에 붙였고, [1.4.0] 8 을 붙였다(0~6 의 뜻은 그대로 —
#   4 의 원인만 여럿으로 갈렸다). 중단은 128 + 신호 번호(130 · 143).
EXIT_PASSED = 0
EXIT_PARSE_FAILED = 1
EXIT_TOOL_ERROR = 2
EXIT_SENSITIVE = 3
EXIT_NOT_REVIEWED = 4
EXIT_REQUEST_CHANGES = 5
EXIT_UNSTRUCTURED = 6
EXIT_EMPTY_STAGED = 8

# ⚠ **정본**이다. `--help` epilog 가 이 표를 찍고, README · SKILL.md · 모듈 docstring 의
#   사본이 같은 코드 집합을 말하는지 테스트가 본다(`_check_exit_code_tables_agree`).
_EXIT_TABLE = (
    ("0", "통과: 주 모델의 approve · approve_with_comments (--record-fallback 은 기록함)"),
    ("1", "파싱 실패 · 내부 오류: 판정하지 않았다"),
    ("2", "실행 실패: git · agy 없음, agy 가 실패를 알림(모델명 · 인증), --out 에 못 씀, 대체 리뷰 기록 거부"),
    ("3", "민감 경로: 전송 중단"),
    ("4", "리뷰 안 됨: 시간 초과 · 쿼터 · 모델 무응답 · 빈 응답"),
    ("5", "request_changes (모르는 판정값 포함)"),
    ("6", "텍스트로만 받음: 사람이 원문을 읽어야 한다"),
    ("8", "--staged · --paths 인데 커밋할 변경이 없다 (--allow-empty 면 0)"),
    ("130 · 143", "중단 (Ctrl+C · 종료 신호)"),
)

# mode → 종료 코드. `reviewed` · `no_changes` · `interrupted` · `not_run` 은 상황에 따라
#   달라 호출부가 준다. ⚠ 1.4.x 동안 결과 JSON 최상위 `mode` 도 같은 값이다(호환).
_MODE_EXIT = {
    "parse_failed": EXIT_PARSE_FAILED,
    "internal_error": EXIT_PARSE_FAILED,
    "tool_error": EXIT_TOOL_ERROR,
    "model_unavailable": EXIT_TOOL_ERROR,
    "sensitive_blocked": EXIT_SENSITIVE,
    "timeout": EXIT_NOT_REVIEWED,
    "quota_exhausted": EXIT_NOT_REVIEWED,
    "tool_unavailable": EXIT_NOT_REVIEWED,
    "primary_model_unavailable": EXIT_NOT_REVIEWED,
    "review_unavailable": EXIT_NOT_REVIEWED,
    "text_fallback": EXIT_UNSTRUCTURED,
    # [1.7.0] 대체 리뷰 기록 — 기록 성공은 0 이지만 `_meta.passed` 는 false 다(판정이 아니다).
    "fallback_reviewed": EXIT_PASSED,
    "fallback_refused": EXIT_TOOL_ERROR,
}

# LLM 응답에서 결과 파일로 옮기는 키 — 스키마에 있는 것만. 나머지(`_meta` · `model` ·
#   `passed` …)는 코드가 쓰는 계약 키이므로 버린다(E18).
_REVIEW_KEYS = tuple(_SCHEMA["properties"])


def _passed(mode: str, exit_code) -> bool:
    """`_meta.passed` — **주 모델이 리뷰해 통과 판정을 냈을 때만** 참이다. 여기서만 계산한다.

    ⚠ [26.09.14 Eng 교차리뷰] mode 만 보면 `reviewed` 가 exit 0 과 5 둘 다라 request_changes
      를 통과시키고, 종료 코드만 보면 변경분 없음(`--allow-empty`)의 0 을 통과로 읽는다.
      커밋 게이트 hook 은 `passed` 가 true 이고 `exit_code` 가 0 인지 **둘 다** 본다.
    """
    return mode == "reviewed" and exit_code == EXIT_PASSED


def _result(mode: str, body: dict, exit_code, **meta) -> dict:
    """결과 JSON 한 벌. **모든 종료 경로**(시작 · 중단 · 인자 오류 · 내부 오류 포함)가 이것을 쓴다.

    `_meta` 의 `mode` · `exit_code` · `passed` · `plugin_version` 은 코드가 마지막에 대입한다 —
    호출부의 추가 필드가 덮어쓰지 못한다. 값이 None 인 추가 필드는 싣지 않는다.
    """
    m = dict((k, v) for k, v in meta.items() if v is not None)
    # [1.5.0 A5] 집계가 회차 순서를 세려면 시각이 있어야 한다. `in_progress` 기록에도 붙으므로
    #   "finished" 가 아니라 **written** 이다. 1.4.x 파일은 이 키가 없어 파일명(로컬 시각)으로
    #   떨어지므로, 읽는 쪽이 둘을 같은 UTC 로 정규화한다.
    m.update({"mode": mode, "exit_code": exit_code, "passed": _passed(mode, exit_code),
              "plugin_version": __version__, "written_at": _utc_now_str()})
    out = dict(body)
    out["mode"] = mode
    out["_meta"] = m
    return out

# 통과로 볼 판정. `approve_with_comments` 는 지적이 있어도 **통과**다 —
# 그것까지 비-0 으로 만들면 사실상 모든 리뷰가 실패가 되어 종료코드가 다시
# 무의미해진다(실측 236건 중 approve 51 · approve_with_comments 16).
_PASSING_VERDICTS = ("approve", "approve_with_comments")


def _exit_code_for(payload: dict) -> int:
    """리뷰 결과 → 종료코드. **함수로 분리한 것이 의도다.**

    `main()` 안에 인라인으로 두면 이 판정을 단위 테스트할 수 없고, 그러면
    "고쳤다" 는 주장을 검증할 방법이 사라진다(이 저장소의 동어반복 가드 계열).

    ⚠ **모르는 판정값은 통과로 보지 않는다.** 모델이 스키마 밖 문자열을 내면
      그것은 '통과의 증거' 가 아니라 '판정 불가' 다 — 이 저장소가 반복해서
      경계하는 *"판정 불가를 정상으로 읽는"* 계열이라 fail-closed 로 둔다.
    """
    verdict = (payload.get("verdict") or "").strip().lower()
    return 0 if verdict in _PASSING_VERDICTS else EXIT_REQUEST_CHANGES


def _render(p: dict) -> None:
    _safe_print("")
    _safe_print("판정: %s" % p.get("verdict", "?"))
    if p.get("summary"):
        _safe_print("요약: %s" % p["summary"])
    # `p` 는 `_looks_like_review` 를 통과한 객체뿐이라 원소가 전부 dict 임이
    # 이미 보장된다 — 여기서 다시 isinstance 필터를 걸면 dead code 다.
    findings = p.get("findings") or []
    _safe_print("")
    if not findings:
        _safe_print("지적 사항 없음.")
    else:
        _safe_print("지적 %d건 (심각도 순)" % len(findings))
        for f in sorted(findings,
                        key=lambda x: _SEV_ORDER.get(x.get("severity"), 9)):
            loc = f.get("file") or ""
            if f.get("line"):
                loc += ":%s" % f["line"]
            _safe_print("")
            _safe_print("%s %s" % (_SEV_MARK.get(f.get("severity"), "[?]"),
                                   f.get("title", "")))
            if loc:
                _safe_print("  위치: %s" % loc)
            _safe_print("  내용: %s" % f.get("detail", ""))
            if f.get("failure_scenario"):
                _safe_print("  실패 시나리오: %s" % f["failure_scenario"])
    for q in (p.get("questions") or []):
        _safe_print("")
        _safe_print("질문: %s" % q)


if __name__ == "__main__":
    raise SystemExit(main())
