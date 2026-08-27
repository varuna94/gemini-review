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

## 프로젝트별 리뷰 관점 주입

저장소 루트에 **`.gemini-review.md`** 가 있으면 그 내용을 리뷰 지시에 덧붙인다.
그 저장소에서 실제로 반복된 실패 계열을 적어두면 리뷰 품질이 크게 올라간다.
없으면 범용 지시만 쓴다.

사용법:
    python gemini_review.py                    # 마지막 커밋
    python gemini_review.py --base HEAD~3      # 최근 3커밋
    python gemini_review.py --staged           # 스테이징된 변경 (커밋 직전)
    python gemini_review.py --base main        # 브랜치 전체 (PR 전)

종료 코드: 0 통과(approve·approve_with_comments) / 1 파싱 실패 / 2 실행 실패 /
          3 민감 경로 / 4 빈 응답 / **5 request_changes** / **6 구조화 실패**

⚠ [26.08.25] 5·6 은 **신설**이다. 종전엔 판정과 무관하게 0 이었다 — 실측
  리뷰 236건 중 `request_changes` 가 **141건(68%)** 이고 critical 지적이 111건인데
  전부 exit 0 이었다. 그래서 규약이 *"exit 0 도 통과를 뜻하지 않는다"* 는
  **문서 경고**로 막고 있었고, 이제 그것을 코드로 옮긴다.
  기존 0~4 의 의미는 그대로라 그 값을 읽던 곳은 깨지지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import atexit
import shutil
from datetime import datetime
from typing import List, Optional, Tuple

# agy 는 PATH 에 없을 수 있어 알려진 설치 경로를 fallback 으로 둔다.
_AGY_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe"),
    os.path.expanduser("~/.local/bin/agy"),
    "agy",
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
# 스크립트의 흔한 관례다. 실측한 어느 저장소에서는 `.build/devel.env` ·
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

_DEFAULT_MODEL = "gemini-3.1-pro-high"
_CONTEXT_FILE = ".gemini-review.md"


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


def _find_agy() -> Optional[str]:
    for cand in _AGY_CANDIDATES:
        if not cand:
            continue
        # ⚠ 상대 경로는 건너뛴다. 리눅스·맥에는 LOCALAPPDATA 가 없어 첫 후보가
        #   `agy/bin/agy.exe` 라는 **작업 디렉토리 기준 상대 경로**로 평가된다
        #   (실측). 리뷰는 대상 저장소 안에서 도므로, 그 경로를 품은 저장소를
        #   clone 해 리뷰하면 저장소가 심은 파일이 agy 대신 실행된다.
        if cand != "agy" and not os.path.isabs(cand):
            continue
        if os.path.isfile(cand):
            return cand
        if cand == "agy":
            try:
                subprocess.run([cand, "--help"], capture_output=True,
                               timeout=30, check=False)
                return cand
            except (OSError, subprocess.SubprocessError):
                continue
    return None


def _git(args: List[str], cwd: str) -> str:
    """⚠ `core.quotePath=false` 는 **보안 옵션**이다 [26.08.13].

    기본값(true)이면 git 이 비ASCII 경로를 `"\\354\\232\\264\\354\\230\\201.env"`
    처럼 큰따옴표 + 8진 이스케이프로 내보낸다. 그러면 민감 판정의 basename·
    확장자 앵커가 **전부** 빗나가 `운영.env` 가 통과한다 — 실측에서 AWS 키가
    담긴 diff 가 `민감: 없음` 배너와 함께 전송 직전까지 갔다.
    오탐과 달리 fail-open 이라 사후에도 드러나지 않는다. 한국어 파일명만의
    문제가 아니다 — 경로 어디든 비ASCII 바이트 하나면 발동한다.
    """
    out = subprocess.run(["git", "-c", "core.quotePath=false"] + args,
                         cwd=cwd, capture_output=True,
                         timeout=60, check=False)
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
        diff = _git(["diff", "--cached"], root)
        files = [f for f in _git(["diff", "--cached", "--name-only"], root).splitlines() if f]
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
        diff = _git(["diff", rng], root)
        files = [f for f in _git(["diff", rng, "--name-only"], root).splitlines() if f]
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


def main(argv=None) -> int:
    # ⚠ argparse 보다 **먼저** 불러야 한다 [26.08.13 재점검]. `--help` 와
    # argparse 의 에러 메시지는 `parse_args` **안에서** 출력되므로, 그 뒤에
    # 두면 한글·em dash 가 cp949 로 나가 UnicodeEncodeError 로 죽는다
    # (실측: `--help` → exit 1). help 문자열에서 em dash 를 뺀 것과 함께
    # 이중 방어다.
    _ensure_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="Gemini cross-review via Antigravity CLI (agy)")
    ap.add_argument("--base", default="HEAD~1")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--staged", action="store_true", help="스테이징된 변경 리뷰")
    ap.add_argument("--two-dot", action="store_true",
                    help="base..head 2-dot diff (기본은 merge-base 기준 3-dot). "
                         "브랜치 리뷰에서는 쓰지 마라 (남의 커밋이 섞인다)")
    # ⚠ `--effort` 는 두지 않는다. agy 는 **모델명에 effort 가 내장**돼 있고
    # (`gemini-3.1-pro-high`/`-low`, `gemini-3.6-flash-medium` …), 모델 접미사와
    # 다른 --effort 를 주면 즉시 status=ERROR 로 죽는다 (실측: 0초, tokens 0).
    ap.add_argument("--model", default=_DEFAULT_MODEL,
                    help="기본 %s — 바꾸지 말 것. flash 계열은 빈 응답이 잦아 "
                         "게이트로 쓸 수 없다(실측). 다른 계열이 필요하면 "
                         "사람에게 먼저 묻는다. `agy models` 로 목록 확인" % _DEFAULT_MODEL)
    ap.add_argument("--timeout", default="10m", help="agy --print-timeout")
    ap.add_argument("--out", default=None, help="결과 JSON 저장 경로")
    ap.add_argument("--allow-sensitive", action="store_true",
                    help="민감 경로가 diff 에 있어도 강행 (기본: 중단)")
    args = ap.parse_args(argv)

    agy = _find_agy()
    if not agy:
        _safe_print("agy(Antigravity CLI)를 찾지 못했다.")
        _safe_print("  설치: irm https://antigravity.google/cli/install.ps1 | iex")
        return 2

    try:
        root = _git_root(os.getcwd())
        diff, files = _collect_diff(root, args.base, args.head, args.staged,
                                    args.two_dot)
    except RuntimeError as exc:
        _safe_print(str(exc))
        return 2
    if not diff.strip():
        _safe_print("변경분이 없다.")
        return 0

    classified = [(f, _classify_path(f)) for f in files]
    blocked = [f for f, c in classified if c == "block"]
    warned = [f for f, c in classified if c == "warn"]
    if blocked and not args.allow_sensitive:
        _safe_print("⛔ 민감 경로가 diff 에 포함돼 있다 — 외부 전송을 중단한다:")
        for f in blocked:
            _safe_print("   %s" % f)
        _safe_print("   의도한 것이면 --allow-sensitive 로 다시 실행하라.")
        return 3

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
    _safe_print("Gemini 교차 리뷰 (Antigravity CLI · %s)" % args.model)
    _safe_print("저장소: %s" % root)
    _safe_print("범위: %s / 변경 파일 %d개 / diff %d자" % (scope, len(files), len(diff)))
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

    # ⚠ 정리를 등록해 두고 만든다. 예전에는 mkdtemp 만 하고 지우지 않아
    #   /tmp 에 50개 1.5MB 가 쌓였고, 그 안에 diff 전문이 평문으로 남아 있었다.
    tmpdir = tempfile.mkdtemp(prefix="gemini_review_")
    atexit.register(shutil.rmtree, tmpdir, True)
    diff_path = os.path.join(tmpdir, "changes.diff")
    schema_path = os.path.join(tmpdir, "schema.json")
    with open(diff_path, "w", encoding="utf-8") as f:
        f.write(diff)
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(_SCHEMA, f, ensure_ascii=False)

    cmd = [
        agy,
        "--mode", "plan",                 # ★ read-only 고정 (협상 대상 아님)
        "--model", args.model,
        "--output-format", "json",
        "--json-schema", schema_path,
        "--print-timeout", args.timeout,
        "--add-dir", root,
        "-p", _build_prompt(diff_path, files, project_ctx),
    ]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        _safe_print("agy 실행 실패: %s" % exc)
        return 2

    raw = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 and not raw:
        _safe_print("agy 종료코드 %d" % proc.returncode)
        _safe_print(proc.stderr.decode("utf-8", "replace")[:800])
        return 2

    # agy 래퍼를 먼저 본다 — 모델이 **빈 응답**을 낸 경우를 '파싱 실패'로
    # 뭉뚱그리면 원인을 오해한다 (실측: status=SUCCESS 인데 response="" 이고
    # output_tokens 14077·thinking_tokens 13849 — 생각은 했으나 전달 실패).
    wrapper = None
    try:
        wrapper = json.loads(raw)
    except ValueError:
        pass
    if isinstance(wrapper, dict) and "response" in wrapper:
        usage = wrapper.get("usage") or {}
        if not str(wrapper.get("response") or "").strip():
            _safe_print("⚠ 스키마 강제 출력이 빈 응답을 반환했다 "
                        "(status=%s · %.0fs · output %s · thinking %s)."
                        % (wrapper.get("status"), wrapper.get("duration_seconds") or 0,
                           usage.get("output_tokens"), usage.get("thinking_tokens")))
            # [26.08.12] **텍스트 모드로 재시도한다.**
            # 진단 결과 `--json-schema` 강제가 원인이다 — 같은 diff·같은 모델로
            # 스키마 없이 요청하면 정상 응답한다(thinking 은 3만 토큰까지 도는데
            # 스키마에 맞춘 최종 출력만 비어서 온다). 여기서 그냥 포기하면
            # **리뷰가 통째로 유실**되므로, 형식을 포기하고 내용을 건진다.
            _safe_print("   → 텍스트 모드로 재시도한다 (형식만 포기, 리뷰는 받는다)")
            text = _retry_as_text(agy, args, root, diff_path, files, project_ctx)
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
                _write_out(args.out, {
                    "mode": "text_fallback",
                    "note": "스키마 강제가 빈 응답을 반환해 텍스트로 재시도했다 "
                            "— verdict·findings 없음. 원문을 사람이 읽어야 한다.",
                    "raw_text": text,
                })
                # ⚠ [26.08.25] 종전엔 0 이었다. 그런데 바로 위 note 가 스스로
                #   *"원문을 사람이 읽어야 한다"* 고 적는다 — 종료코드가 성공이면
                #   그 당부는 자동화에 전달되지 않는다. 실측 236건 중 28건(12%)이
                #   이 경로다.
                return EXIT_UNSTRUCTURED
            _safe_print("   ⛔ 텍스트 재시도도 실패했다 — 리뷰가 안 된 것이다.")
            _safe_print("     이 결과를 '지적 없음'으로 읽지 말 것.")
            return 4

    payload = _extract_json(raw)
    if payload is None:
        _safe_print("JSON 파싱 실패 — 원문을 그대로 출력한다:")
        _safe_print(raw[:4000])
        return 1

    _render(payload)
    _write_out(args.out, payload)
    rc = _exit_code_for(payload)
    if rc:
        _safe_print("")
        _safe_print("   (종료코드 %d — 지적을 실측 검증한 뒤 반영하고 다시 돌릴 것)"
                    % rc)
    return rc


def _write_out(out_path: Optional[str], payload: dict) -> None:
    """결과 JSON 저장. 실패해도 리뷰 자체는 이미 화면에 나갔으므로 무시한다."""
    path = out_path or os.path.join(
        tempfile.gettempdir(),
        "gemini_review_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        _safe_print("")
        _safe_print("결과 저장: %s" % path)
    except OSError as exc:
        _safe_print("결과 저장 실패(무시): %s" % exc)


def _retry_as_text(agy: str, args, root: str, diff_path: str,
                   files: List[str], project_ctx: str) -> str:
    """스키마 없이 텍스트로 재요청 — 형식을 포기하고 내용을 건진다.

    `--json-schema` 강제가 빈 응답을 유발하는 경우가 실제로 있다(진단 확인).
    구조화 파싱은 포기하되 지적 자체는 받아야 하므로, 텍스트 형식을 명시적으로
    지시해 사람이 읽을 수 있게 받는다.
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
        agy, "--mode", "plan", "--model", args.model,
        "--output-format", "text",
        "--print-timeout", args.timeout,
        "--add-dir", root,
        "-p", prompt,
    ]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    # ⚠ [26.08.27 교차리뷰 지적] 종료 코드를 보지 않으면, agy 가 타임아웃·인증
    #   오류로 죽으며 남긴 **에러 문구를 리뷰 원문으로 저장**한다. 그러면 exit 6
    #   (구조화 실패)이 되어 "리뷰는 받았다"로 읽힌다. 실패는 실패로 돌린다 —
    #   호출부가 이 빈 문자열을 보고 exit 4(리뷰 안 됨)로 끝낸다.
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", "replace").strip()


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


def _extract_json(raw: str):
    """agy 출력에서 리뷰 JSON 을 뽑는다 (래핑 형태가 바뀌어도 견디게).

    ⚠ **첫 매칭에서 반환하지 않는다.** `_json_candidates` 는 문서 왼쪽부터
    훑으므로, LLM 이 사고 과정에서 만든 예시 JSON 이 앞에 있고 최종 답이 뒤에
    오면 앞의 것을 집어 진짜 리뷰를 버리게 된다. 끝까지 훑어 **마지막**을 쓴다.
    """
    best = None
    for candidate in _json_candidates(raw):
        if _looks_like_review(candidate):
            best = candidate
            continue
        if isinstance(candidate, dict):
            for key in ("result", "response", "output", "content"):
                inner = candidate.get(key)
                if _looks_like_review(inner):
                    best = inner
                elif isinstance(inner, str):
                    for c2 in _json_candidates(inner):
                        if _looks_like_review(c2):
                            best = c2
    return best


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
