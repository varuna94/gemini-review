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

⚠ 모델은 `gemini-3.1-pro-high` 고정이다 — 느리다고 flash 로 낮추지 않는다(SKILL.md).

종료 코드: 0 통과(approve·approve_with_comments) / 1 파싱 실패 / 2 실행 실패 /
          3 민감 경로 / 4 빈 응답 / **5 request_changes** / **6 구조화 실패** /
          130 · 143 중단(신호). 이 목록에 없는 코드는 통과가 아니다.

⚠ [26.08.25] 5·6 은 **신설**이다. 종전엔 판정과 무관하게 0 이었다 — 실측
  리뷰 236건 중 `request_changes` 가 **141건(68%)** 이고 critical 지적이 111건인데
  전부 exit 0 이었다. 그래서 규약이 *"exit 0 도 통과를 뜻하지 않는다"* 는
  **문서 경고**로 막고 있었고, 이제 그것을 코드로 옮긴다.
  기존 0~4 의 의미는 그대로라 그 값을 읽던 곳은 깨지지 않는다.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
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
from datetime import datetime
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
__version__ = "1.3.2"

_DEFAULT_MODEL = "gemini-3.1-pro-high"
_DEFAULT_FALLBACK_MODEL = "gemini-3.6-flash-high"
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


def _git(args: List[str], cwd: str, config: Optional[List[str]] = None) -> str:
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
                             cwd=cwd, capture_output=True,
                             timeout=60, check=False)
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
        "필요하면 저장소의 다른 파일도 읽어 맥락을 확인하라 (읽기 전용 모드다).",
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


def _record_interrupt(out: Optional[str], signum: int) -> int:
    """중단을 화면과 `--out` 에 남기고 종료 코드(128+신호 번호)를 돌려준다."""
    try:
        # ⚠ `signal.Signals` 는 Python 3.5+ 다(공식 문서 "Added in version 3.5").
        #   [26.09.14 교차리뷰가 "3.8+ 라 3.7 에서 AttributeError" 라고 짚었으나 사실이 아니다]
        name = signal.Signals(signum).name
    except ValueError:
        name = "signal %d" % signum
    _safe_print("")
    _safe_print("⛔ 중단됐다(%s) — 리뷰가 수행되지 않았다. 임시 파일은 정리한다." % name)
    _safe_print("   이 결과를 '지적 없음'으로 읽지 말 것.")
    if out:
        _write_out(out, {
            "mode": "interrupted",
            "signal": name,
            "note": "신호로 중단됐다 — 리뷰가 수행되지 않았다. 통과가 아니다.",
        }, quiet=True)
    return 128 + int(signum)


def main(argv=None) -> int:
    """진입점. 본문(`_main`)을 신호 처리로 감싼다(26.09.14 S1)."""
    restore, suppress = _install_signal_handlers()
    ctx = {"out": None, "tmpdir": None}
    try:
        return _main(argv, ctx)
    except (_Interrupted, KeyboardInterrupt) as exc:
        suppress()
        # 정리는 **지금** 한다 — 처리기를 되돌린 뒤 atexit 까지 기다리면 그 사이의
        #   두 번째 신호가 정리를 끊는다(atexit 등록은 남겨 두어도 두 번 지워 무해).
        if ctx["tmpdir"]:
            shutil.rmtree(ctx["tmpdir"], True)
        return _record_interrupt(ctx["out"],
                                 getattr(exc, "signum", signal.SIGINT))
    finally:
        restore()


def _main(argv, ctx: dict) -> int:
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
    if out_early and _write_out(out_early, {
            "mode": "in_progress",
            "note": "리뷰가 시작됐고 아직 끝나지 않았다. 이 파일이 이 상태로 남아 "
                    "있으면 리뷰가 중간에 죽은 것이다 — 통과가 아니다.",
            "started_at": datetime.now().isoformat(timespec="seconds"),
    }, quiet=True) is None:
        _safe_print("⛔ --out 경로에 쓸 수 없다: %s" % out_early)
        _safe_print("   리뷰를 시작하지 않는다(외부 전송 없음). 경로 · 권한을 확인할 것.")
        return 2

    ap = _build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        # ⚠ [26.09.14 Gemini 교차리뷰 HIGH] `--help` · 인자 오류로 여기서 끝나면 방금
        #   쓴 `in_progress`("중간에 죽었다") 가 사실과 다르게 남는다. 리뷰를 시작하지도
        #   않았다는 것을 그대로 적는다 — 여전히 통과가 아니다.
        if out_early:
            _write_out(out_early, {
                "mode": "not_run",
                "note": "인자 해석 단계에서 끝났다(도움말 · 판 확인 · 인자 오류) — 리뷰가 "
                        "수행되지 않았다. 통과가 아니다.",
                "exit_code": exc.code,
            }, quiet=True)
        raise

    def finish(payload: dict, rc: int) -> int:
        """최종 결과를 `--out` 에 남긴다. **명시한 `--out` 에 못 쓰면 exit 2.**

        ⛔ [26.09.14] 종전에는 기록 실패를 무시해, 판정 코드와 파일 내용이 갈렸다.
          파일을 믿는 자동화에게는 종료 코드보다 파일이 먼저다.
        """
        if _write_out(args.out, payload) is None and args.out:
            _safe_print("⛔ --out 에 결과를 쓰지 못했다 — 종료코드 %d 대신 2 로 끝낸다."
                        % rc)
            return 2
        return rc

    try:
        root = _git_root(os.getcwd())
        diff, files = _collect_diff(root, args.base, args.head, args.staged,
                                    args.two_dot)
    except RuntimeError as exc:
        _safe_print(str(exc))
        if not args.staged and args.base == "HEAD~1":
            _safe_print("  첫 커밋만 있는 저장소라면 비교할 이전 커밋이 없다 — "
                        "`--staged` 또는 `--base <ref>` 로 범위를 지정할 것.")
        return finish({"mode": "tool_error", "note": str(exc)}, 2)

    # ⚠ [26.09.14] 저장소 루트를 안 뒤에 찾는다 — PATH 에서 저장소 안 항목을 빼려면
    #   루트가 필요하다(`_which_agy`).
    agy = _find_agy(root)
    if not agy:
        _safe_print("agy(Antigravity CLI)를 찾지 못했다 — 리뷰가 수행되지 않았다.")
        _safe_print("  확인한 곳: 알려진 설치 경로 · PATH(현재 폴더와 저장소 안은 제외)")
        _safe_print("  설치: https://antigravity.google/cli "
                    "(Windows: irm https://antigravity.google/cli/install.ps1 | iex)")
        _safe_print("  설치 뒤 `agy` 를 한 번 실행해 Google 계정으로 로그인할 것.")
        return finish({"mode": "tool_error",
                       "note": "agy 를 찾지 못했다 — 리뷰가 수행되지 않았다."}, 2)
    # ⚠ [26.09.14 Gemini 교차리뷰 HIGH] 아래 조기 종료 갈래들도 **최종 상태**를 남긴다.
    #   종전에는 `in_progress`("중간에 죽었다") 가 그대로 남아 사실과 달랐다.
    #   종료 코드는 바꾸지 않는다(1.3.2 는 동작 계약 불변).
    if not diff.strip():
        _safe_print("변경분이 없다. 리뷰는 수행되지 않았다"
                    "%s." % (" — 스테이징한 변경이 없다면 `git add` 후 다시" if args.staged else ""))
        return finish({"mode": "no_changes",
                       "note": "리뷰할 변경분이 없었다 — 리뷰가 수행되지 않았다."}, 0)

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
        return finish({"mode": "sensitive_blocked", "blocked": blocked,
                       "note": "민감 경로가 있어 전송을 중단했다 — 리뷰가 수행되지 않았다."}, 3)

    project_ctx = _load_project_context(root)
    if args.staged:
        scope = "staged"
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
    with open(diff_path, "w", encoding="utf-8") as f:
        f.write(diff)
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(_SCHEMA, f, ensure_ascii=False)

    prompt = _build_prompt(diff_path, files, project_ctx)
    used_model = args.model
    raw, elapsed, fatal = _invoke_schema(agy, used_model, args, root,
                                         schema_path, prompt)
    if fatal is not None:
        return finish({"mode": "tool_error", "model": used_model,
                       "note": "agy 실행이 실패했다 — 리뷰가 수행되지 않았다."}, fatal)
    _safe_print("응답: %s · %.0f초" % (used_model, elapsed))

    # agy 래퍼를 먼저 본다 — 모델이 **빈 응답**을 낸 경우를 '파싱 실패'로
    # 뭉뚱그리면 원인을 오해한다 (실측: status=SUCCESS 인데 response="" 이고
    # output_tokens 14077·thinking_tokens 13849 — 생각은 했으나 전달 실패).
    info = _empty_info(raw)
    if info is not None:
        _safe_print("⚠ 스키마 강제 출력이 빈 응답을 반환했다 "
                    "(status=%s · %.0fs · output %s · thinking %s)."
                    % (info["status"], info["duration_seconds"],
                       info["output_tokens"], info["thinking_tokens"]))

        # ① **도구 계층이 살아 있는가**를 먼저 가른다 [26.09.10].
        #    이 확인이 없으면 계층 장애일 때 400~550초짜리 재시도를 두세 번 더
        #    돌게 된다(실측: 그날 4회가 그렇게 헛돌았고, 폴백 모델도 439초
        #    output 0 이었다). 재시도가 의미 있는 상황인지부터 확인한다.
        #
        # ⛔ **[26.09.10 리뷰] 한 모델만 찔러 보고 '계층' 을 단정하면 안 된다.**
        #    종전 구현은 `used_model` 로만 프로브해 놓고 실패 시 *"도구 계층
        #    장애 · 모델 변경으로는 넘어가지 않는다"* 고 적었다. 그것은 프로브가
        #    확인한 적 없는 명제다 — 주 모델 하나만 쿼터·용량 문제로 죽은 상황과
        #    구분되지 않고, 바로 그 상황을 위해 있는 폴백이 영영 안 불린다.
        #    → **주 모델이 죽으면 폴백 모델로도 찔러 본다.** 둘 다 죽어야 계층이다.
        fb = "" if args.no_fallback else (args.fallback_model or "").strip()
        if fb and fb == used_model:
            # ⚠ [26.09.10] 종전에는 **문서가 급행 경로로 권하는 값**이 폴백
            #   기본값과 같아서, 그 값을 주면 폴백이 **침묵한 채** 건너뛰어졌다
            #   (`--no-fallback` 을 준 실행과 화면이 구분되지 않았다).
            #   같은 날 문서 넷의 권장값을 `gemini-3.8-flash-high` 로 옮겨 그
            #   충돌 자체는 없앴지만, 운영자가 손으로 같은 값을 줄 수는 있으므로
            #   이 갈래는 남긴다.
            alt = (_DEFAULT_MODEL if used_model != _DEFAULT_MODEL
                   else _DEFAULT_FALLBACK_MODEL)
            _safe_print("   ⚠ 폴백 모델이 주 모델과 같다(%s) — 대신 %s 를 쓴다."
                        % (fb, alt))
            fb = alt

        _safe_print("   → 계층 생존 확인 (한 줄 프롬프트, %s)" % _PROBE_TIMEOUT)
        alive, probe_secs = _probe_alive(agy, used_model, root)
        probe_model, fb_alive, fb_probe_secs = used_model, None, 0.0
        if not alive and fb:
            _safe_print("   주 모델 프로브 실패 (%.0f초) — 폴백 모델로 확인한다: %s"
                        % (probe_secs, fb))
            fb_alive, fb_probe_secs = _probe_alive(agy, fb, root)
            if fb_alive:
                alive, probe_model = True, fb
        if not alive:
            _safe_print("   ⛔ agy 가 한 줄 프롬프트에도 응답하지 않는다 "
                        "(%s). **도구 계층 장애**다."
                        % ("주 %.0f초 · 폴백 %.0f초" % (probe_secs, fb_probe_secs)
                           if fb else "%.0f초" % probe_secs))
            # ⛔ [26.09.10 2회차 리뷰] **확인한 만큼만 말한다.** 종전에는 이 문장이
            #   조건 없이 찍혀, `--no-fallback` 으로 **주 모델 하나만** 찌른 경우에도
            #   "모델 변경으로는 안 된다" 고 단정했다. 그러면 주 모델만 죽은 날
            #   운영자가 다른 모델을 시도하지 않는다 — 1회차가 지적한 오단정이
            #   기본 경로에서만 고쳐지고 이 갈래에 남아 있었다.
            if fb:
                _safe_print("     재시도·모델 변경으로는 넘어가지 않는다 — "
                            "diff 크기나 프롬프트 내용의 문제가 아니다 "
                            "(두 모델을 확인했다).")
                _safe_print("     `agy models` 로 인증을 확인하고, 시간을 두고 "
                            "다시 돌릴 것. 이 결과를 '지적 없음'으로 읽지 말 것.")
            else:
                _safe_print("     ⚠ **주 모델 하나만 확인했다**(폴백 없음) — 다른 "
                            "모델은 살아 있을 수 있다.")
                _safe_print("     `--no-fallback` 을 빼고 다시 돌려 볼 것. "
                            "이 결과를 '지적 없음'으로 읽지 말 것.")
            return finish({
                "mode": "tool_unavailable",
                "note": "agy 계층이 한 줄 프롬프트에도 응답하지 않았다"
                        "%s — 리뷰가 수행되지 않았다. 통과가 아니다."
                        % (" (주 모델·폴백 모델 둘 다)" if fb else " (주 모델)"),
                "model": used_model,
                "probed_models": [used_model] + ([fb] if fb else []),
                "elapsed_seconds": round(elapsed, 1),
                "probe_seconds": round(probe_secs, 1),
                "fallback_probe_seconds": round(fb_probe_secs, 1),
            }, 4)
        if probe_model != used_model:
            _safe_print("   생존 확인 OK — **주 모델만 죽었다**(폴백 %s 는 %.0f초에 "
                        "응답). 계층 장애가 아니다." % (probe_model, fb_probe_secs))
        else:
            _safe_print("   생존 확인 OK (%.0f초) — 계층은 살아 있다. "
                        "프롬프트·스키마 쪽 문제로 좁혀진다." % probe_secs)

        # ② 폴백 모델로 **구조화**를 한 번 더 시도한다. 텍스트보다 먼저 두는
        #    이유는 판정이 종료코드에 실리기 때문이다(26.08.25) — 텍스트로
        #    떨어지면 exit 6 이 되어 사람이 원문을 읽어야 한다.
        text_model = used_model
        if fb:
            _safe_print("   → 폴백 모델로 구조화 재시도: %s" % fb)
            raw2, elapsed2, fatal2 = _invoke_schema(agy, fb, args, root,
                                                    schema_path, prompt)
            info2 = None if fatal2 is not None else _empty_info(raw2)
            if fatal2 is None and info2 is None and _extract_json(raw2) is not None:
                _safe_print("   폴백 성공: %s · %.0f초" % (fb, elapsed2))
                raw, used_model, elapsed, info = raw2, fb, elapsed2, None
            else:
                _safe_print("   폴백도 실패 (%.0f초) — 텍스트 모드로 내려간다."
                            % elapsed2)
                # ⚠ 텍스트 재시도는 **살아 있다고 확인된 모델**로 한다.
                #   주 모델이 죽은 것이 확인됐는데 그 모델로 텍스트를 요청하면
                #   같은 시간을 또 태운다(26.09.10 리뷰: 종전에는 이 인자가
                #   언제나 `args.model` 이라 값이 갈릴 여지가 구조적으로 없었다).
                text_model = probe_model
        else:
            _safe_print("   → 폴백 재시도 없음 (%s)"
                        % ("--no-fallback" if args.no_fallback
                           else "폴백 모델 미지정"))

    if info is not None:
        # [26.08.12] **텍스트 모드로 재시도한다.**
        # 진단 결과 `--json-schema` 강제가 원인이다 — 같은 diff·같은 모델로
        # 스키마 없이 요청하면 정상 응답한다(thinking 은 3만 토큰까지 도는데
        # 스키마에 맞춘 최종 출력만 비어서 온다). 여기서 그냥 포기하면
        # **리뷰가 통째로 유실**되므로, 형식을 포기하고 내용을 건진다.
        _safe_print("   → 텍스트 모드로 재시도한다 (형식만 포기, 리뷰는 받는다)")
        text = _retry_as_text(agy, text_model, args, root, diff_path,
                              files, project_ctx)
        if text:
            _safe_print("")
            _safe_print("-" * 74)
            _safe_print("[텍스트 모드 리뷰 — 구조화 실패로 원문 그대로]")
            _safe_print("-" * 74)
            _safe_print(text)
            # ⚠ [26.08.13 Gemini 교차리뷰 지적 — 실측 확인] 여기서 `--out`
            # 을 건너뛰면 호출자가 exit 0 을 받고도 파일이 없어 터지거나,
            # 더 나쁘게는 **직전 실행의 낡은 JSON** 을 읽어 어제 리뷰로
            # 오늘 변경을 승인한다. 구조화에 실패했으니 verdict 를 지어내지
            # 말고, 형식이 다르다는 사실 자체를 파일에 남긴다.
            return finish({
                "mode": "text_fallback",
                "note": "스키마 강제가 빈 응답을 반환해 텍스트로 재시도했다 "
                        "— verdict·findings 없음. 원문을 사람이 읽어야 한다.",
                "model": text_model,
                "raw_text": text,
            # ⚠ [26.08.25] 종전엔 0 이었다. 그런데 바로 위 note 가 스스로
            #   *"원문을 사람이 읽어야 한다"* 고 적는다 — 종료코드가 성공이면
            #   그 당부는 자동화에 전달되지 않는다. 실측 236건 중 28건(12%)이
            #   이 경로다.
            }, EXIT_UNSTRUCTURED)
        _safe_print("   ⛔ 텍스트 재시도도 실패했다 — 리뷰가 안 된 것이다.")
        _safe_print("     이 결과를 '지적 없음'으로 읽지 말 것.")
        # ⚠ [26.09.10 리뷰] 이 갈래에도 `--out` 을 남긴다. 종전에는 계층 장애
        #   경로만 파일을 썼고 여기서는 그냥 `return 4` 였다 — 그러면 호출자가
        #   **직전 실행의 낡은 JSON** 을 읽어 어제 리뷰로 오늘 변경을 승인한다
        #   (26.08.13 에 텍스트 성공 경로에서 실제로 확인된 계열인데, 실패
        #   경로에 같은 구멍이 남아 있었다).
        return finish({
            "mode": "review_unavailable",
            "note": "스키마·폴백·텍스트가 모두 빈 응답이었다 — 리뷰가 수행되지 "
                    "않았다. 통과가 아니다.",
            "model": used_model,
            "text_model": text_model,
            "elapsed_seconds": round(elapsed, 1),
        }, 4)

    payload = _extract_json(raw)
    if payload is None:
        _safe_print("JSON 파싱 실패 — 원문을 그대로 출력한다:")
        _safe_print(raw[:4000])
        return finish({"mode": "parse_failed", "model": used_model,
                       "note": "응답에서 리뷰 JSON 을 하나로 특정하지 못했다 — 통과가 아니다."}, 1)

    # ⚠ [26.09.10 리뷰] **어느 모델이 이 판정을 냈는지** 기록한다. 폴백으로
    #   되찾은 경우 화면의 "폴백 성공" 한 줄은 스크롤백에만 남고, 며칠 뒤
    #   `--out` JSON 으로 "커밋 전 리뷰 통과" 를 재구성하면 pro 가 냈는지
    #   flash 가 냈는지 알 방법이 없다(종전에는 `used_model` 이 재대입된 뒤
    #   한 번도 읽히지 않는 dead store 였다).
    # ⛔ [26.09.14 CEO 스펙 리뷰 — 확인] `setdefault` 는 LLM 응답에 `model` 키가
    #   **이미 있으면** 그 값을 남긴다. 폴백 모델이 판정했는데 응답이
    #   `"model": "gemini-3.1-pro-high"` 를 담고 있으면 주 모델이 판정한 것으로
    #   기록되고, diff 속 프롬프트 주입으로도 위조된다 → **코드가 대입한다.**
    if isinstance(payload, dict):
        payload["model"] = used_model
        payload["elapsed_seconds"] = round(elapsed, 1)
    _render(payload)
    rc = _exit_code_for(payload)
    if rc:
        _safe_print("")
        _safe_print("   (종료코드 %d — 지적을 실측 검증한 뒤 반영하고 다시 돌릴 것)"
                    % rc)
    return finish(payload, rc)


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
    ap = argparse.ArgumentParser(
        description="Gemini cross-review via Antigravity CLI (agy)")
    ap.add_argument("--version", action=_PrintVersion)
    ap.add_argument("--base", default="HEAD~1")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--staged", action="store_true", help="스테이징된 변경 리뷰")
    ap.add_argument("--two-dot", action="store_true",
                    help="base..head 2-dot diff (기본은 merge-base 기준 3-dot). "
                         "브랜치 리뷰에서는 쓰지 마라 (남의 커밋이 섞인다)")
    # ⚠ `--effort` 는 두지 않는다. agy 는 **모델명에 effort 가 내장**돼 있고
    # (`gemini-3.1-pro-high`/`-low`, `gemini-3.6-flash-medium` …), 모델 접미사와
    # 다른 --effort 를 주면 즉시 status=ERROR 로 죽는다 (실측: 0초, tokens 0).
    # ⚠ [26.09.10 리뷰] 종전 help 는 "빠르게: gemini-3.6-flash-high" 를 **리터럴**
    #   로 권했는데, 그 값이 곧 `_DEFAULT_FALLBACK_MODEL` 이라 그대로 주면 폴백이
    #   침묵한 채 죽었다. 두 상수의 **관계**가 어디에도 표현되지 않은 탓이다.
    ap.add_argument("--model", default=_DEFAULT_MODEL,
                    help="기본 %s. `agy models` 로 목록 확인. 폴백 기본값은 %s 라, "
                         "그 값을 --model 로 주면 폴백이 자동으로 다른 모델로 "
                         "바뀐다" % (_DEFAULT_MODEL, _DEFAULT_FALLBACK_MODEL))
    ap.add_argument("--fallback-model", default=_DEFAULT_FALLBACK_MODEL,
                    help="주 모델이 빈 응답을 내면 이 모델로 한 번 더 "
                         "구조화 시도 (기본 %s)" % _DEFAULT_FALLBACK_MODEL)
    ap.add_argument("--no-fallback", action="store_true",
                    help="폴백 모델 재시도를 끈다")
    ap.add_argument("--timeout", default="10m", help="agy --print-timeout")
    ap.add_argument("--out", default=None, help="결과 JSON 저장 경로")
    ap.add_argument("--allow-sensitive", action="store_true",
                    help="민감 경로가 diff 에 있어도 강행 (기본: 중단)")
    return ap


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


def _default_result_dir() -> str:
    """`--out` 을 주지 않았을 때 결과 JSON 을 둘 **사용자 전용** 폴더.

    ⛔ [26.09.14] 종전 기본값은 `tempfile.gettempdir()` 바로 아래
      `gemini_review_<시각>.json` 이었다. 이 PC 실측으로 `-rw-rw-r--` 파일
      137개가 공유 `/tmp` 에 쌓여 있었고, 담긴 것은 코드가 인용된 지적이다.
      같은 초에 끝난 두 실행은 서로 덮어썼다(→ 파일명에 PID).
    → POSIX 는 `$XDG_STATE_HOME/gemini-review`(기본 `~/.local/state`), Windows 는
      `%LOCALAPPDATA%\\gemini-review`(이미 사용자별).
    ⚠ 폴더가 **링크이거나 남의 소유**면 쓰지 않고 새 임시 폴더로 대체한다.
      이름을 짐작할 수 있는 경로는 다른 사용자가 먼저 만들어 둘 수 있다
      (26.09.14 Eng 리뷰). 내 소유인데 권한만 느슨하면 0700 으로 좁힌다.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        d = os.path.join(base, "gemini-review")
        os.makedirs(d, exist_ok=True)
        return d
    base = os.environ.get("XDG_STATE_HOME") or ""
    if not os.path.isabs(base):          # XDG 규약: 상대 경로는 무시한다
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    d = os.path.join(base, "gemini-review")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.lstat(d)
        if (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode)
                or st.st_uid != os.getuid()):
            raise OSError("안전하지 않은 결과 폴더: %s" % d)
        # ⚠ [26.09.14 Gemini 교차리뷰 HIGH] "느슨하면 좁힌다"만으로는 부족하다.
        #   내 소유 폴더가 쓰기 권한 없이(0500, 엄격한 umask) 있으면 `& 0o077` 이
        #   0 이라 그대로 반환되고 결과 기록이 실패한다 → 정확히 0700 으로 맞춘다.
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(d, 0o700)
        return d
    except OSError:
        return tempfile.mkdtemp(prefix="gemini_review_out_")


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


def _invoke_schema(agy: str, model: str, args, root: str, schema_path: str,
                   prompt: str) -> Tuple[str, float, Optional[int]]:
    """스키마 강제 호출 1회. 반환 `(raw, 소요초, 치명적 종료코드 or None)`.

    ⚠ **소요를 재는 것이 이 함수의 절반**이다 [26.09.10]. 종전에는 호출 시간이
      화면에 남지 않아, 25~43초가 400~550초로 열화된 사실을 사람이 알아채지
      못했다. 그날 원인을 두 번 잘못 짚었는데(프롬프트 크기 → 모델 계층),
      두 오진 모두 "얼마나 걸렸는가" 가 보였으면 첫 회에 갈렸을 것이다.
    """
    cmd = [
        agy,
        "--mode", "plan",                 # ★ read-only 고정 (협상 대상 아님)
        "--model", model,
        "--output-format", "json",
        "--json-schema", schema_path,
        "--print-timeout", args.timeout,
        "--add-dir", root,
        "-p", prompt,
    ]
    started = time.time()
    hard = _duration_seconds(args.timeout, 600) + _HARD_TIMEOUT_MARGIN
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, check=False,
                              stdin=subprocess.DEVNULL, timeout=hard)
    except subprocess.TimeoutExpired:
        # ⚠ 원인을 정확히 지목한다. 종전 문구는 "`--print-timeout <원문>` 초과" 라
        #   적어, 래퍼가 그 표기를 못 읽어 상한이 짧아진 경우에도 agy 탓으로
        #   보이게 했다(26.09.10 2회차 리뷰).
        _safe_print("agy 를 강제 종료했다 — 바깥 상한 %d초 초과 "
                    "(--print-timeout %s 요청 · 래퍼는 %d초로 읽었다)."
                    % (hard, args.timeout, hard - _HARD_TIMEOUT_MARGIN))
        return "", time.time() - started, 2
    except (OSError, subprocess.SubprocessError) as exc:
        _safe_print("agy 실행 실패: %s" % exc)
        return "", time.time() - started, 2
    elapsed = time.time() - started
    raw = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    # ⛔ [26.09.14 실측] 종전 조건은 `returncode != 0 and not raw` 였다.
    #   · 없는 모델명 → agy exit 1 + stdout 에 `{"status":"ERROR","response":"",
    #     "error":"invalid model selection …"}` → 빈 응답으로 읽혀 생존 확인 ·
    #     **폴백 모델 판정**으로 이어졌다(주 모델이 없어도 flash 의 approve 가 exit 0).
    #   · 인증 오류처럼 stdout 에 평문이 오면 "JSON 파싱 실패" exit 1 로 끝났다.
    #   → agy 가 실패를 알리면(종료 코드 · 래퍼 status) stdout 과 무관하게 실패다.
    if proc.returncode != 0 or _wrapper_status(raw) == "ERROR":
        detail = _agy_error_detail(raw, err)
        _safe_print("⛔ agy 가 실패했다(종료코드 %d · %.0f초) — 리뷰가 수행되지 않았다."
                    % (proc.returncode, elapsed))
        if detail:
            _safe_print("   %s" % detail.splitlines()[0][:300])
        if _looks_like_model_error(detail):
            _safe_print("   모델명을 확인할 것: `agy models` 로 목록을 보고 --model 로 지정.")
        return raw, elapsed, 2
    if _is_print_timeout(err):
        # 동작은 그대로(1.4.0 에서 원인별 종료 코드). 원인만 화면에 남긴다.
        _safe_print("⚠ agy 가 출력 시간 초과를 알렸다(--timeout %s) — 응답이 비었거나 "
                    "잘렸을 수 있다." % args.timeout)
    return raw, elapsed, None


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


def _probe_alive(agy: str, model: str, root: str) -> Tuple[bool, float]:
    """agy 계층이 **한 줄 프롬프트**에 응답하는가. 반환 `(살아있음, 초)`.

    이 저장소도 diff 도 읽지 않는 최소 호출이다 — 그래야 결과가 '계층 생존'
    하나만 뜻한다. 실패하면 재시도(폴백 모델 · 텍스트 모드)는 전부 낭비다.

    ⚠ 살아 있다고 해서 긴 프롬프트가 된다는 뜻은 아니다. 이 확인은 **한쪽
      방향으로만** 결정적이다 — 죽어 있으면 재시도가 무의미하다는 것.
    """
    cmd = [agy, "--mode", "plan", "--model", model,
           "--output-format", "text",
           "--print-timeout", _PROBE_TIMEOUT,
           "-p", _PROBE_PROMPT]
    started = time.time()
    hard = _duration_seconds(_PROBE_TIMEOUT, 60) + _HARD_TIMEOUT_MARGIN
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, check=False,
                              stdin=subprocess.DEVNULL, timeout=hard)
    except subprocess.TimeoutExpired:
        # 상한을 넘겼다는 것 자체가 '응답하지 않는다' 는 답이다.
        return False, time.time() - started
    except (OSError, subprocess.SubprocessError):
        return False, time.time() - started
    elapsed = time.time() - started
    # ⛔ [26.09.14 Eng 교차리뷰] 종전에는 stdout 만 봤다 — `_retry_as_text` ·
    #   `_invoke_schema` 에서 고친 "종료 코드를 안 본다" 계열의 세 번째 자리다.
    #   에러 문구에 OK 라는 낱말이 섞이면 살아 있다고 판정해 긴 재시도로 넘어간다.
    alive = (proc.returncode == 0
             and not _is_print_timeout(proc.stderr.decode("utf-8", "replace"))
             and _probe_says_ok(proc.stdout.decode("utf-8", "replace")))
    return alive, elapsed


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
                   files: List[str], project_ctx: str) -> str:
    """스키마 없이 텍스트로 재요청 — 형식을 포기하고 내용을 건진다.

    `--json-schema` 강제가 빈 응답을 유발하는 경우가 실제로 있다(진단 확인).
    구조화 파싱은 포기하되 지적 자체는 받아야 하므로, 텍스트 형식을 명시적으로
    지시해 사람이 읽을 수 있게 받는다.

    반환: 리뷰 원문. **리뷰를 받지 못했으면 빈 문자열**이다 — 호출부가 그것을
    보고 exit 4(리뷰 안 됨)로 끝낸다. agy 가 무언가 출력했다는 것만으로는
    리뷰를 받은 것이 아니다(아래 두 갈래).
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
    cmd = [
        agy, "--mode", "plan", "--model", model,
        "--output-format", "text",
        "--print-timeout", args.timeout,
        "--add-dir", root,
        "-p", prompt,
    ]
    started = time.time()
    hard = _duration_seconds(args.timeout, 600) + _HARD_TIMEOUT_MARGIN
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, check=False,
                              stdin=subprocess.DEVNULL, timeout=hard)
    except subprocess.TimeoutExpired:
        _safe_print("   텍스트 재시도: %s · %d초 초과로 강제 종료" % (model, hard))
        return ""
    except (OSError, subprocess.SubprocessError):
        return ""
    elapsed = time.time() - started
    text = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    # ⚠ [26.08.27 교차리뷰 지적] 종료 코드를 보지 않으면, agy 가 타임아웃·인증
    #   오류로 죽으며 남긴 **에러 문구를 리뷰 원문으로 저장**한다. 그러면 exit 6
    #   (구조화 실패)이 되어 "리뷰는 받았다" 로 읽힌다. 실패는 실패로 돌린다.
    # ⛔ [26.09.14] **이 검사는 v1.3.0 병합에서 한 번 사라졌다.** 테스트가 파일
    #   전체에서 `proc.returncode != 0` 문자열을 찾았는데 `_invoke_schema` 에 같은
    #   문자열이 있어 두 판 동안 초록이었다. 실측: exit 1 + stdout `Error:
    #   authentication required…` 가 그대로 exit 6 의 리뷰 원문이 됐다.
    #   → 가드: `tests/test_gemini_review.py::_check_retry_as_text_rejects_failed_agy`
    #     (문자열이 아니라 이 함수를 가짜 agy 로 직접 돌린다).
    if proc.returncode != 0:
        _safe_print("   텍스트 재시도: %s · %.0f초 · agy 종료코드 %d — 리뷰로 쓰지 않는다"
                    % (model, elapsed, proc.returncode))
        if err or text:
            _safe_print("     %s" % (err or text)[:800])
        return ""
    # ⛔ [26.09.14 실측] **출력 시간 초과는 exit 0 이다** (`_is_print_timeout`).
    #   stdout 에는 그때까지의 부분 출력이 남으므로 종료 코드만 보면 **잘린
    #   리뷰**가 온전한 리뷰로 저장된다 — 판정 줄까지만 오고 지적이 잘리면
    #   사람이 읽어도 통과로 읽힌다. 부분이라도 건질지 고민했으나, 어디서
    #   잘렸는지 알 수 없는 원문은 '지적 없음' 과 구분되지 않아 버린다.
    if _is_print_timeout(err) or _has_agy_timeout_line(text):
        _safe_print("   텍스트 재시도: %s · %.0f초 · agy 출력 시간 초과 "
                    "(부분 출력 %d자) — 잘린 리뷰라 쓰지 않는다"
                    % (model, elapsed, len(text)))
        return ""
    _safe_print("   텍스트 재시도: %s · %.0f초 · %s"
                % (model, elapsed, "응답 있음" if text else "빈 응답"))
    return text


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

# [26.08.25] 판정 → 종료코드. 기존 0~4 는 건드리지 않고 뒤에 붙인다.
EXIT_REQUEST_CHANGES = 5
EXIT_UNSTRUCTURED = 6

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
