#!/usr/bin/env python3
# [26.08.26] gemini_review 회귀 테스트 — 표준 라이브러리만 쓴다.
#
# 왜 있는가: 이 스킬은 두 PC 에서 각각 고쳐지다가 판이 갈라진 적이 있다
# (2026-08-25). 한쪽에는 종료코드 5·6 이, 다른 쪽에는 민감경로 가드 수정이
# 있었고, 최신본을 그냥 덮어썼다면 **가드 다섯 개가 조용히 사라졌을** 것이다.
# 여기 적힌 것은 전부 그때 실측으로 확인한 동작이다. 병합 뒤 이 파일을 돌려
# 양쪽 기능이 모두 살아 있는지 확인한다.
#
#   python3 tests/test_gemini_review.py
#
# 종료 코드: 0 전부 통과 / 1 회귀 발생

import contextlib
import importlib.util
import json
import os
import io
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_TARGET = os.path.join(_HERE, os.pardir, "plugins", "gemini-review",
                       "skills", "gemini-review", "gemini_review.py")


def _load():
    spec = importlib.util.spec_from_file_location("gemini_review", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# (경로, 기대 판정, 이 케이스가 지키는 것)
_PATH_CASES = [
    ("src/clientSecret.json",       "block", "camelCase 경계 — 없으면 키가 그대로 전송된다"),
    ("config/dbPassword.txt",       "block", "camelCase 경계"),
    ("config/serviceAccount.json",  "block", "구분자 통일 — GCP 서비스 계정 키"),
    ("config/service_account.json", "block", "구분자 통일"),
    ("client_secret.json",          "block", "기본 민감 파일명"),
    ("deploy/id_rsa",               "block", "개인키"),
    ("src/secrets/apiKey.mjs",      "",      ".mjs 는 코드다 — 오탐이면 --allow-sensitive 가 습관이 된다"),
    ("scripts/deploy.bash",         "",      ".bash 는 코드다"),
    ("src/design-tokens.ts",        "",      "평범한 프론트엔드 파일을 막지 않는다"),
    (".env.example",                "warn",  "placeholder 관례는 차단이 아니라 경고"),
    # [26.08.27] 교차리뷰가 잡은 가드 우회 — 실측으로 재현하고 고쳤다
    ("config/DBPassword.txt",       "block", "약어 뒤 경계 — 없으면 dbpassword 한 덩어리로 빠져나간다"),
    ("config/AWSCredentials.json",  "block", "약어 뒤 경계"),
    ("my secret.txt",               "block", "공백도 구분자 — 없으면 'my secret' 한 덩어리가 된다"),
    ("api key.csv",                 "block", "공백도 구분자"),
    ("src/auth/credentials/validator.ts", "", "코드 파일은 오탐하지 않는다"),
    ("service account.json",        "block", "구분자 일반화 — service/account 단독은 민감어가 아니라 basename 이 유일한 가드다"),
    ("service.account.json",        "block", "구분자 일반화"),
]

# (payload, 기대 종료코드)
_EXIT_CASES = [
    ({"verdict": "approve"},               0),
    ({"verdict": "approve_with_comments"}, 0),
    ({"verdict": "request_changes"},       5),
    ({"verdict": "REQUEST_CHANGES"},       5),
    ({"verdict": "  Approve  "},           0),
    ({"verdict": "판정불가"},               5),  # fail-closed
    ({"verdict": ""},                      5),
    ({},                                   5),
    ({"verdict": None},                    5),
]


_ROOT = os.path.join(_HERE, os.pardir)
_SKILL_MD = os.path.join(_ROOT, "plugins", "gemini-review",
                         "skills", "gemini-review", "SKILL.md")
_PLUGIN_JSON = os.path.join(_ROOT, "plugins", "gemini-review",
                            ".claude-plugin", "plugin.json")
# ⚠ `.claude-plugin/marketplace.json` 은 **일부러 읽지 않는다** — 아래 버전
#   가드 주석 참조(마켓플레이스 메타는 플러그인 버전이 아니다).


def _make_stdout_safe():
    """cp949 콘솔에서 이 파일이 죽지 않게 한다.

    ⛔ **cp949 보호를 검증하는 테스트가 정작 cp949 에서 죽고 있었다**
      (26.09.14 Windows 실측). `—`·`✗`·`⛔` 는 cp949 에 매핑이 없어
      `UnicodeEncodeError` 로 프로세스가 끝난다 — 검사 결과를 **한 줄도 못 본다.**
      우분투(UTF-8)에서만 돌려 왔기 때문에 3주 동안 드러나지 않았다.
    ⚠ 스킬 본체는 `util/console_safe` 계열의 보호를 이미 갖고 있다. 이 함수는
      **테스트 자신**을 위한 것이고, 표준 라이브러리만 쓴다는 이 파일의 제약을
      지킨다.
    ⚠ **`stderr` 도 맞춰 둔다 — 다만 근거는 '죽는다' 가 아니다.**
      [26.09.14 교차 리뷰 4회차 HIGH 를 실측으로 일부 기각] 지적은 traceback 이
      stderr 로 나갈 때 `UnicodeEncodeError` 로 죽는다고 했으나 **그렇지 않다.**
      파이썬의 stderr 는 기본 에러 핸들러가 `backslashreplace` 라 죽지 않는다:

          보호 없이  →  `\\u26d4 traceback 항목`   exit 0   (실측)
          보호 후    →  `? traceback 항목`        exit 0

      그래도 맞추는 이유는 **읽기 쉬움** 하나다 — 실패를 조사하는 사람이
      `\\u26d4` 를 해독하지 않아도 된다. 안전 요건이 아니므로 실패해도 그냥
      넘어간다.
    """
    for stream in ("stdout", "stderr"):
        try:
            getattr(sys, stream).reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass                  # 3.6 이하·리다이렉트 등 — 보호 없이 진행한다


def _join_shell_continuations(lines):
    """셸 연속선(`\\`)으로 나뉜 명령을 한 줄로 잇는다.

    ⚠ **[26.09.14 교차 리뷰 3회차 LOW]** 아래 경로 가드가 한 줄 안에서
      `python` 과 `gemini_review.py` 를 함께 찾는데, 명령을 두 줄로 쪼개면
      어느 줄도 둘을 동시에 갖지 않아 **가드를 빠져나간다.**
    ⚠ 이어 붙인 결과만 돌려준다 — 원본 줄 번호는 이 가드가 쓰지 않는다.
    """
    out, buf = [], None
    for ln in lines:
        cur = (buf + " " + ln.strip()) if buf is not None else ln
        if cur.rstrip().endswith("\\"):
            buf = cur.rstrip()[:-1]
        else:
            out.append(cur)
            buf = None
    if buf is not None:
        out.append(buf)
    return out


def _version_of(path, *keys):
    """JSON 에서 버전 문자열을 꺼낸다."""
    import json
    with io.open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for k in keys:
        data = data[k]
    return data


def _frontmatter_version(path):
    """SKILL.md frontmatter 의 `version:` 값. 못 읽으면 `None`.

    ⚠ **본문까지 훑지 않는다** — 본문에 `version:` 으로 시작하는 줄이 생기면
      엉뚱한 값을 읽는다. frontmatter 는 첫 `---` 와 다음 `---` 사이다.
    ⛔ **닫는 마커가 없으면 전체를 훑지 말고 `None` 을 돌려준다**
      [26.09.14 교차 리뷰 MEDIUM]. 종전에는 `end == -1` 일 때 `head = src` 로
      떨어져 **위 주석이 약속한 것과 정반대로** 파일 전체를 스캔했다. 그러면
      frontmatter 가 깨진 파일에서 본문의 임의 문자열을 버전으로 오인한다 —
      가드가 자기 docstring 을 지키지 않는 형태다.
    """
    with io.open(path, encoding="utf-8") as fh:
        src = fh.read()
    if not src.startswith("---"):
        return None
    end = src.find("\n---", 3)
    if end < 0:
        return None               # 닫는 마커 없음 = frontmatter 를 못 읽었다
    for line in src[:end].splitlines():
        if line.startswith("version:"):
            # ⚠ **YAML 표기를 벗긴다** [26.09.14 교차 리뷰 3회차 MEDIUM].
            #   `version: "1.3.1"` 이나 `version: 1.3.1 # 릴리즈` 는 정상 YAML
            #   인데, 그대로 비교하면 JSON 파서가 낸 `1.3.1` 과 달라
            #   **의미상 같은 버전인데 배포가 막힌다**(오탐).
            val = line.split(":", 1)[1]
            val = val.split("#", 1)[0].strip()
            return val.strip("\"'")
    return None


# ── 동작 검사 ────────────────────────────────────────────────────────────────
#
# ⛔ [26.09.14] **소스에서 문자열을 찾는 가드를 쓰지 않는다.** 종전 가드
#   `"proc.returncode != 0" in src` 는 **파일 전체**를 뒤졌고, 같은 문자열이
#   `_invoke_schema` 에 있어서 v1.3.0 병합이 `_retry_as_text` 의 검사를 지운
#   뒤에도 v1.3.0·v1.3.1 두 판 동안 초록이었다. `atexit.register` · `--mode`
#   검사도 같은 모양이었다 — 주석이나 다른 함수에 그 낱말만 있으면 통과한다.
#   → 여기 검사는 전부 **그 함수를 실제로 돌려** 결과를 본다.
#
# 각 검사는 제너레이터다. 하위 검사마다 통과면 `None`, 실패면 문구를 낸다.
# 검사 수는 낸 개수로 센다 — 손으로 센 상수가 조용히 어긋나지 않게.


@contextlib.contextmanager
def _patched(obj, name, value):
    """`obj.name` 을 잠시 바꾼다. 검사가 예외로 끝나도 되돌린다."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def _rmtree_sandbox(path, sandbox):
    """`sandbox` **안**의 경로만 지운다. 밖이면 지우지 않고 예외를 낸다.

    ⛔ [26.09.14 사고] 결과 폴더 검사의 정리 코드가 "경로가 임시 폴더 아래면 그
      폴더를 지운다" 는 조건이었다. 변이(기본 결과 폴더를 `tempfile.gettempdir()`
      로 되돌림) 아래에서 그 폴더가 **`/tmp` 자체**가 되어 `shutil.rmtree("/tmp")`
      가 실행됐고, 사용자 소유 `/tmp` 파일과 **다른 Claude Code 세션의 작업
      폴더**가 지워졌다. 테스트는 코드가 계산한 경로를 지우면 안 된다 — 코드가
      틀리면(바로 그것을 검사하는 중이다) 그 경로가 어디든 될 수 있다.
    """
    real = os.path.realpath(path)
    root = os.path.realpath(sandbox)
    # 샌드박스 자체도 확인한다: 테스트가 만든 `gr_test_*` 폴더여야 하고 임시 폴더 루트면 안 된다.
    if (not os.path.basename(root).startswith("gr_test_")
            or root == os.path.realpath(tempfile.gettempdir())):
        raise RuntimeError("샌드박스가 아닌 폴더 삭제 거부: %s" % sandbox)
    if real != root and not real.startswith(root + os.sep):
        raise RuntimeError("샌드박스 밖 삭제 거부: %s (샌드박스 %s)" % (path, sandbox))
    shutil.rmtree(real, True)


@contextlib.contextmanager
def _quiet():
    """스킬이 찍는 진행 문구를 삼킨다 — 검사 결과만 화면에 남긴다."""
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        yield


def _fake_subprocess(calls, returncode=0, stdout=b"", stderr=b""):
    """`gr.subprocess` 자리에 넣을 가짜 모듈. `run` 만 바꾼다.

    ⚠ 진짜 `subprocess.run` 을 덮어쓰지 않는다 — 그러면 같은 프로세스의
      다른 검사(자식 프로세스를 띄우는 검사)까지 가짜를 탄다.
      예외 클래스·`DEVNULL` 은 진짜를 그대로 쓴다.
    """
    def run(cmd, **_kwargs):
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=returncode,
                                     stdout=stdout, stderr=stderr)
    attrs = {k: getattr(subprocess, k) for k in dir(subprocess)
             if not k.startswith("_")}
    attrs["run"] = run
    return types.SimpleNamespace(**attrs)


def _check_retry_as_text_rejects_failed_agy(gr):
    """텍스트 재시도가 **리뷰가 아닌 출력**을 리뷰로 반환하지 않는다.

    [26.08.27] 교차리뷰가 짚어 고쳤던 결함이 v1.3.0 병합에서 되살아났다.
    실측(26.09.14): agy 가 exit 1 과 함께 stdout 에 `Error: authentication
    required…` 를 내면 `_retry_as_text` 가 그 문구를 반환했고, `main()` 은
    그것을 `mode: text_fallback` 의 원문으로 저장해 **exit 6** 을 냈다.
    리뷰가 안 됐는데 "리뷰는 받았다(형식만 실패)" 로 읽힌다.
    """
    args = types.SimpleNamespace(timeout="10m")

    def call(**proc):
        with _quiet(), _patched(gr, "subprocess", _fake_subprocess([], **proc)):
            return gr._retry_as_text("agy", "m", args, ".", "changes.diff",
                                     ["a.py"], "")

    got = call(returncode=1, stdout=b"Error: authentication required.")
    yield (None if got == "" else
           "_retry_as_text 가 agy 종료코드 1 의 출력을 리뷰로 반환한다: %r"
           % got[:60])

    # ⚠ [26.09.14 실측] agy 는 `--print-timeout` 에 걸려도 **exit 0** 이고,
    #   안내문은 stderr 로, 그때까지의 부분 출력은 stdout 으로 낸다. 종료 코드만
    #   보면 **잘린 리뷰**가 온전한 리뷰로 저장된다 — 판정 줄까지만 오고 지적이
    #   잘리면 통과로 읽힌다.
    got = call(returncode=0, stdout="판정: approve".encode("utf-8"),
               stderr=b"[agy] print timeout after 10m0s with turn in progress; "
                      b"returning partial output")
    yield (None if got == "" else
           "_retry_as_text 가 출력 시간 초과로 잘린 부분 출력을 리뷰로 반환한다: %r"
           % got[:60])

    # [26.09.14 Gemini 교차리뷰] stderr 가 stdout 에 섞이는 환경 — 안내 줄이 본문에 온다.
    got = call(returncode=0,
               stdout=("판정: approve\n[agy] print timeout after 10m0s with turn "
                       "in progress; returning partial output").encode("utf-8"))
    yield (None if got == "" else
           "_retry_as_text 가 본문에 섞인 시간 초과 안내 줄을 리뷰로 반환한다: %r"
           % got[:60])

    # 대조군 — 없으면 '언제나 빈 문자열' 로 고쳐도 위 검사들이 통과한다.
    review = "판정: request_changes\n요약: 대조군"
    got = call(returncode=0, stdout=review.encode("utf-8"))
    yield (None if got == review else
           "대조군: _retry_as_text 가 정상 응답(exit 0)을 버린다: %r" % got[:60])

    # 대조군 3 — [26.09.14 Gemini 교차리뷰 2회차] 리뷰 **중간** 줄이 안내문 꼴로
    #   시작해도 버리지 않는다(안내문은 출력 끝에 붙는다).
    mid = ("판정: request_changes\n[agy] print timeout after 10m0s with turn in progress; "
           "returning partial output\n  내용: 위 안내문이 stdout 에 섞이는 경우를 다룬다\n요약: 끝")
    got = call(returncode=0, stdout=mid.encode("utf-8"))
    yield (None if got == mid else
           "대조군: 중간 줄이 '[agy] print timeout' 으로 시작하는 정상 리뷰를 버린다: %r"
           % got[:60])

    # 대조군 2 — 이 저장소를 리뷰한 정상 결과는 "print timeout" 을 **인용**한다.
    #   본문 전체에서 문구를 찾으면 그런 리뷰를 버린다(오탐 → exit 4).
    quoted = ("판정: request_changes\n  내용: `_is_print_timeout` 은 \"print timeout\" "
              "과 \"turn in progress\" 를 찾는다")
    got = call(returncode=0, stdout=quoted.encode("utf-8"))
    yield (None if got == quoted else
           "대조군: 'print timeout' 을 인용한 정상 리뷰를 버린다: %r" % got[:60])


def _check_agy_calls_are_plan_mode(gr):
    """agy 를 부르는 **모든 자리**가 `--mode plan` 을 정확히 한 번 넘긴다.

    ⚠ 종전 가드는 파일 어딘가에 `--mode` 와 `"plan"` 이 있는지만 봤다 — 세 자리
      중 하나에서 빠져도 통과한다. 풀리면 Gemini 가 저장소 파일을 수정할 수 있다.
    ⚠ agy 를 부르는 함수를 새로 만들면 **여기에 추가할 것.** 이 목록 밖의
      호출은 이 검사가 보지 못한다.
    """
    args = types.SimpleNamespace(timeout="10m")
    sites = (
        ("_invoke_schema", lambda: gr._invoke_schema(
            "agy", "m", args, ".", "schema.json", "prompt")),
        ("_probe_alive", lambda: gr._probe_alive("agy", "m", ".")),
        ("_retry_as_text", lambda: gr._retry_as_text(
            "agy", "m", args, ".", "changes.diff", ["a.py"], "")),
    )
    for name, invoke in sites:
        calls = []
        with _quiet(), _patched(gr, "subprocess",
                                _fake_subprocess(calls, stdout=b"OK")):
            invoke()
        if len(calls) != 1:
            yield "%s 가 agy 를 %d번 불렀다 (기대 1번)" % (name, len(calls))
            continue
        cmd = calls[0]
        # `--mode plan` 과 `--mode=plan` 을 같은 것으로 본다 [26.09.14 Gemini 교차리뷰].
        values = []
        for i, a in enumerate(cmd):
            if a == "--mode":
                values.append(cmd[i + 1] if i + 1 < len(cmd) else None)
            elif a.startswith("--mode="):
                values.append(a.split("=", 1)[1])
        ok = values == ["plan"]
        yield (None if ok else
               "%s 의 agy 명령에 `--mode plan` 이 정확히 한 번 있지 않다 — "
               "Gemini 가 파일을 수정할 수 있게 된다: %s" % (name, cmd[:6]))


def _check_find_agy_skips_relative_candidates(gr):
    """작업 디렉터리에 심은 `agy` 를 실행하지 않는다.

    리눅스·맥에는 `LOCALAPPDATA` 가 없어 첫 후보가 `agy/bin/agy.exe` 라는
    **상대 경로**가 된다. 리뷰는 대상 저장소 안에서 돌므로, 그 경로를 품은
    저장소를 clone 해 리뷰하면 저장소가 심은 파일이 agy 대신 실행된다.
    """
    sandbox = tempfile.mkdtemp(prefix="gr_test_agy_")
    planted = os.path.join(sandbox, "agy", "bin", "agy.exe")
    # `_AGY_CANDIDATES` 가 import 시점에 `LOCALAPPDATA=""` 로 만드는 값과 같다.
    relative = os.path.join("agy", "bin", "agy.exe")
    old_cwd = os.getcwd()
    try:
        os.makedirs(os.path.dirname(planted))
        open(planted, "w").close()
        # 실행 권한을 준다 — `_find_agy` 가 실행 가능 여부를 보도록 바뀌어도
        # 대조군이 거짓 빨강이 되지 않게 [26.09.14 Gemini 교차리뷰].
        os.chmod(planted, 0o755)
        os.chdir(sandbox)
        with _patched(gr, "_AGY_CANDIDATES", [relative]):
            got = gr._find_agy()
        yield (None if got is None else
               "_find_agy 가 상대 경로 %r 를 골랐다 — 저장소가 심은 파일이 "
               "agy 대신 실행된다" % got)
        # 대조군 — 없으면 '언제나 None' 으로 고쳐도 위 검사가 통과한다.
        with _patched(gr, "_AGY_CANDIDATES", [planted]):
            got = gr._find_agy()
        yield (None if got == planted else
               "대조군: _find_agy 가 절대 경로 후보를 찾지 못한다: %r" % got)
    finally:
        os.chdir(old_cwd)
        _rmtree_sandbox(sandbox, sandbox)


# 자식 프로세스에서 `main()` 을 가짜 agy 로 끝까지 돌린다.
# argv: 스크립트 경로 · 가짜 저장소 루트 · --out 경로
_CHILD_RUN = r'''
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("gemini_review", sys.argv[1])
gr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gr)
root, out = sys.argv[2], sys.argv[3]
seen = []

def fake_invoke(agy, model, args, root_, schema_path, prompt):
    d = os.path.dirname(schema_path)
    if os.path.isfile(os.path.join(d, "changes.diff")):
        seen.append(os.path.basename(d))
    return (json.dumps({"verdict": "approve", "summary": "t", "findings": []}),
            0.0, None)

gr._find_agy = lambda: "agy"
gr._git_root = lambda start: root
gr._collect_diff = lambda *a, **k: ("diff --git a/x.py b/x.py\n+x = 1\n", ["x.py"])
gr._invoke_schema = fake_invoke
rc = gr.main(["--out", out])
sys.stdout.write("\nCHILD_DIFF_DIRS=%s\n" % "|".join(seen))
sys.exit(rc)
'''


def _check_tmpdir_removed_after_real_run(gr):
    """리뷰 프로세스가 끝나면 **diff 를 담은 임시 디렉터리가 남지 않는다.**

    이 정리는 두 번 사라졌다 — 처음엔 `/tmp` 에 50개 1.5MB, 두 번째는 3,116개
    (26.09.11). 담기는 것이 `changes.diff`(저장소 코드 전문)라 용량보다 내용이
    문제다.
    ⚠ **자식 프로세스로 돌린다.** `atexit` 는 프로세스가 끝날 때 돌므로 같은
      프로세스 안에서는 "지워진다" 를 확인할 수 없다. 등록 여부만 보면 엉뚱한
      경로를 등록해도 통과한다 — 여기서는 **그 diff 가 있던 디렉터리**가 실제로
      사라졌는지 본다.
    ⚠ 자식의 임시 디렉터리를 샌드박스로 돌린다(`TMPDIR`·`TEMP`·`TMP`). 정리가
      깨져 있어도 이 검사 자체가 `/tmp` 에 흔적을 남기지 않는다.
    """
    del gr  # 자식이 스크립트를 따로 불러온다
    sandbox = tempfile.mkdtemp(prefix="gr_test_tmp_")
    try:
        env = dict(os.environ, TMPDIR=sandbox, TEMP=sandbox, TMP=sandbox)
        env.pop("PYTHONIOENCODING", None)
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_RUN, os.path.abspath(_TARGET),
             sandbox, os.path.join(sandbox, "out.json")],
            env=env, capture_output=True, timeout=120, check=False)
        out = proc.stdout.decode("utf-8", "replace")
        m = re.search(r"CHILD_DIFF_DIRS=(\S*)", out)
        dirs = [d for d in (m.group(1).split("|") if m else []) if d]
        if proc.returncode != 0 or len(dirs) != 1:
            tail = (out + proc.stderr.decode("utf-8", "replace")).strip()[-300:]
            yield ("임시 디렉터리 검사를 수행하지 못했다 (자식 exit %d · diff "
                   "디렉터리 %d개 · 기대 exit 0 · 1개): %s"
                   % (proc.returncode, len(dirs), tail))
            return
        left = os.path.join(sandbox, dirs[0])
        yield (None if not os.path.exists(left) else
               "프로세스가 끝났는데 diff 를 담은 임시 디렉터리 %s 가 남았다 — "
               "저장소 코드가 평문으로 쌓인다" % dirs[0])
    finally:
        _rmtree_sandbox(sandbox, sandbox)


class _Skip(object):
    """동작 검사를 이 환경에서 돌릴 수 없을 때 낸다. 검사 수에 넣지 않고 따로 알린다.

    ⚠ 조용히 건너뛰지 않는다 — 요약 줄에 "건너뜀 N건: 이유" 로 남긴다.
    """
    def __init__(self, reason):
        self.reason = reason


_STALE_APPROVE = {"verdict": "approve", "summary": "어제의 리뷰", "findings": []}


def _main_inprocess(gr, argv, sandbox, payload=None):
    """가짜 agy · git 으로 `main()` 을 같은 프로세스에서 돌린다. 반환 `(rc, 모델 호출 목록)`.

    ⚠ `--out` 을 주지 않는 호출은 기본 결과 폴더(`$XDG_STATE_HOME`)를 쓰므로,
      이 함수는 그 변수를 샌드박스로 돌려 **실제 홈에 흔적을 남기지 않는다.**
    """
    calls = []

    def fake_invoke(agy, model, args, root, schema_path, prompt):
        calls.append(model)
        body = payload if payload is not None else {
            "verdict": "approve", "summary": "t", "findings": []}
        return json.dumps(body), 0.0, None

    env = dict(os.environ, XDG_STATE_HOME=os.path.join(sandbox, "state"))
    with _quiet(), \
            _patched(os, "environ", env), \
            _patched(gr, "_find_agy", lambda: "agy"), \
            _patched(gr, "_git_root", lambda start: sandbox), \
            _patched(gr, "_collect_diff",
                     lambda *a, **k: ("diff --git a/x.py b/x.py\n+x = 1\n", ["x.py"])), \
            _patched(gr, "_invoke_schema", fake_invoke):
        try:
            rc = gr.main(argv)
        except SystemExit as exc:          # argparse 오류 · --help
            rc = exc.code
    return rc, calls


def _read_json(path):
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _check_out_never_keeps_stale_result(gr):
    """`--out` 에 **직전 실행의 결과가 남는 경로가 없다.**

    ⛔ [26.09.14 Eng 교차리뷰 P1 — 재현] ① 인자 오류면 무효화 전에 argparse 가
      exit 2 로 끝나 파일이 어제의 approve 그대로였다. ② 쓸 수 없는 `--out` 이면
      무효화 · 최종 기록이 모두 조용히 실패해 **exit 5 인데 파일은 approve** 였다.
      파일만 믿는 자동화(hook)는 둘 다 통과로 읽는다.
    """
    sandbox = tempfile.mkdtemp(prefix="gr_test_out_")
    try:
        out = os.path.join(sandbox, "out.json")
        with io.open(out, "w", encoding="utf-8") as fh:
            json.dump(_STALE_APPROVE, fh)
        rc, _ = _main_inprocess(gr, ["--out", out, "--no-such-flag"], sandbox)
        body = _read_json(out)
        yield (None if body.get("verdict") != "approve" else
               "인자 오류(exit %s)인데 --out 에 직전 실행의 approve 가 남았다" % rc)

        # 시작 무효화는 됐는데 **최종 기록만** 실패 → 판정(approve)이 아니라 exit 2
        out2 = os.path.join(sandbox, "out2.json")
        real_atomic = gr._write_json_atomic

        def fail_on_verdict(path, payload):
            if "verdict" in payload:
                raise OSError("디스크 가득 참(흉내)")
            return real_atomic(path, payload)

        with _patched(gr, "_write_json_atomic", fail_on_verdict):
            rc, _ = _main_inprocess(gr, ["--out", out2], sandbox)
        mode = _read_json(out2).get("mode")
        yield (None if rc == 2 and mode == "in_progress" else
               "최종 기록이 실패했는데 exit %s · 파일 mode %r — 통과로 읽힐 수 있다"
               % (rc, mode))

        # ⚠ 아래는 환경에 따라 건너뛴다 — 환경과 무관한 검사는 **이 앞에** 둘 것
        #   [26.09.14 Gemini 교차리뷰: 건너뜀 뒤 return 이 뒤 검사까지 삼켰다].
        # 쓸 수 없는 --out → 리뷰를 시작하지 않고(외부 호출 0회) exit 2
        if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            yield _Skip("쓰기 불가 폴더를 만들 수 없는 환경(Windows · root)")
            return
        locked = os.path.join(sandbox, "locked")
        os.makedirs(locked)
        target = os.path.join(locked, "out.json")
        with io.open(target, "w", encoding="utf-8") as fh:
            json.dump(_STALE_APPROVE, fh)
        os.chmod(locked, 0o500)
        try:
            rc, calls = _main_inprocess(
                gr, ["--out", target], sandbox,
                payload={"verdict": "request_changes", "summary": "s", "findings": []})
        finally:
            os.chmod(locked, 0o700)
        yield (None if rc == 2 and not calls else
               "쓸 수 없는 --out 인데 리뷰가 진행됐다(exit %s · agy 호출 %d회) — "
               "종료 코드와 파일이 다른 이야기를 한다" % (rc, len(calls)))

    finally:
        _rmtree_sandbox(sandbox, sandbox)


def _check_write_out_is_atomic(gr):
    """결과 기록 도중 끊겨도 **잘린 JSON 이 남지 않는다.**

    [26.09.14 실측] `open(path, "w")` + `json.dump` 도중 `SystemExit` 이면
    잘린 JSON 이 남았다(`JSONDecodeError`).
    """
    sandbox = tempfile.mkdtemp(prefix="gr_test_atomic_")
    try:
        out = os.path.join(sandbox, "out.json")
        with io.open(out, "w", encoding="utf-8") as fh:
            json.dump({"mode": "in_progress"}, fh)

        def dying_dump(obj, fp, **kw):
            fp.write('{"verdict": "appro')
            raise SystemExit(143)

        with _quiet(), _patched(gr.json, "dump", dying_dump):
            try:
                gr._write_out(out, _STALE_APPROVE)
            except SystemExit:
                pass
        try:
            body = _read_json(out)
            ok = body == {"mode": "in_progress"}
        except ValueError:
            ok = False
        leftovers = [n for n in os.listdir(sandbox) if n != "out.json"]
        yield (None if ok and not leftovers else
               "기록 도중 끊기자 결과 파일이 깨졌거나 임시 파일이 남았다(%s)" % leftovers)
    finally:
        _rmtree_sandbox(sandbox, sandbox)


def _check_verdict_model_is_recorded_by_code(gr):
    """`--out` 의 `model` 은 **코드가 실제로 부른 모델**이다.

    ⛔ [26.09.14 CEO 스펙 리뷰 — 확인] `payload.setdefault("model", used_model)` 은
      LLM 응답에 `model` 키가 있으면 그 값을 남겼다. 폴백 모델이 판정했는데
      `"model": "gemini-3.1-pro-high"` 가 기록될 수 있고, diff 속 프롬프트 주입으로도
      위조된다.
    """
    sandbox = tempfile.mkdtemp(prefix="gr_test_model_")
    try:
        out = os.path.join(sandbox, "out.json")
        forged = {"verdict": "approve", "summary": "t", "findings": [],
                  "model": "gemini-3.1-pro-high", "elapsed_seconds": 0.0}
        rc, calls = _main_inprocess(gr, ["--out", out, "--model", "m-actual"],
                                    sandbox, payload=forged)
        body = _read_json(out)
        yield (None if body.get("model") == "m-actual" else
               "LLM 응답의 model 값(%r)이 실제 판정 모델(m-actual) 대신 기록됐다"
               % body.get("model"))
    finally:
        _rmtree_sandbox(sandbox, sandbox)


def _check_default_result_dir_is_private(gr):
    """`--out` 이 없을 때 결과는 **사용자 전용** 폴더에 0600 으로 쌓인다.

    [26.09.14 실측] 종전에는 공유 `/tmp` 에 `-rw-rw-r--` 로 쌓였다(137개).
    남이 먼저 만든 폴더 · 링크는 쓰지 않는다(Eng 리뷰).
    """
    if os.name == "nt":
        yield _Skip("POSIX 권한 검사(Windows 는 %LOCALAPPDATA% 가 사용자별)")
        return
    sandbox = tempfile.mkdtemp(prefix="gr_test_resdir_")
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp_in_sandbox(suffix=None, prefix=None, dir=None):
        # 코드가 대체 폴더를 만들더라도 샌드박스 안에 만들게 한다 — 정리는 샌드박스째.
        return real_mkdtemp(suffix=suffix, prefix=prefix, dir=sandbox)

    stray = []                             # 코드가 샌드박스 밖에 쓴 **파일**(폴더는 안 지움)
    try:
        env = dict(os.environ, XDG_STATE_HOME=os.path.join(sandbox, "state"))
        with _quiet(), _patched(os, "environ", env), \
                _patched(gr.tempfile, "mkdtemp", mkdtemp_in_sandbox):
            path = gr._write_out(None, _STALE_APPROVE)
        if path and not os.path.realpath(path).startswith(os.path.realpath(sandbox) + os.sep):
            stray.append(path)
        mode_dir = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
        mode_file = stat.S_IMODE(os.stat(path).st_mode)
        inside = os.path.dirname(path) == os.path.join(sandbox, "state", "gemini-review")
        yield (None if inside and mode_dir == 0o700 and mode_file == 0o600 else
               "기본 결과 위치 · 권한이 기대와 다르다: %s (폴더 %o · 파일 %o)"
               % (path, mode_dir, mode_file))

        # [26.09.14 Gemini 교차리뷰 HIGH] 내 소유인데 쓰기 권한이 없는 폴더(0500)도 고쳐 쓴다
        state3 = os.path.join(sandbox, "state3")
        locked = os.path.join(state3, "gemini-review")
        os.makedirs(locked)
        os.chmod(locked, 0o500)
        env = dict(os.environ, XDG_STATE_HOME=state3)
        with _quiet(), _patched(os, "environ", env), \
                _patched(gr.tempfile, "mkdtemp", mkdtemp_in_sandbox):
            path = gr._write_out(None, _STALE_APPROVE)
        os.chmod(locked, 0o700)            # 검사 결과와 무관하게 정리할 수 있게
        if path and not os.path.realpath(path).startswith(os.path.realpath(sandbox) + os.sep):
            stray.append(path)
        yield (None if path and os.path.dirname(path) == locked else
               "쓰기 권한 없는 내 결과 폴더(0500)를 고치지 않아 결과가 %r 에 갔다" % path)

        # 결과 폴더 자리가 다른 곳을 가리키는 링크면 따라가지 않는다
        state2 = os.path.join(sandbox, "state2")
        elsewhere = os.path.join(sandbox, "elsewhere")
        os.makedirs(state2)
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(state2, "gemini-review"))
        env = dict(os.environ, XDG_STATE_HOME=state2)
        with _quiet(), _patched(os, "environ", env), \
                _patched(gr.tempfile, "mkdtemp", mkdtemp_in_sandbox):
            path = gr._write_out(None, _STALE_APPROVE)
        if path and not os.path.realpath(path).startswith(os.path.realpath(sandbox) + os.sep):
            stray.append(path)
        yield (None if not os.listdir(elsewhere) else
               "결과 폴더 자리의 심볼릭 링크를 따라가 다른 곳에 결과를 썼다")
    finally:
        # ⛔ 샌드박스 밖에 생긴 것은 **그 파일 하나**만, 이름 규칙이 맞을 때만 지운다.
        for f in stray:
            if os.path.isfile(f) and os.path.basename(f).startswith("gemini_review_"):
                os.remove(f)
        _rmtree_sandbox(sandbox, sandbox)


_BEHAVIOR_CHECKS = (
    _check_retry_as_text_rejects_failed_agy,
    _check_agy_calls_are_plan_mode,
    _check_find_agy_skips_relative_candidates,
    _check_tmpdir_removed_after_real_run,
    _check_out_never_keeps_stale_result,
    _check_write_out_is_atomic,
    _check_verdict_model_is_recorded_by_code,
    _check_default_result_dir_is_private,
)


def main():
    _make_stdout_safe()
    gr = _load()
    fails = []

    # [26.09.14] 플러그인과 함께 배포되는 두 파일의 버전이 갈리면 잡는다.
    #   ⛔ **`marketplace.json` 은 보지 않는다.** 그 `metadata.version` 은
    #     마켓플레이스 자체의 메타이지 플러그인 버전이 아니다 — 실측 반례가
    #     `.gemini-review.md` 「이미 검증하고 기각한 지적」에 있다(어느 설치본은
    #     marketplace 1.0.0 인데 plugin.json 의 1.1.0 으로 설치됐고 캐시 경로도
    #     `…/1.1.0/` 이었다, 26.08.27). 셋을 묶으면 **플러그인만 패치해도
    #     정당한 배포가 막힌다** — 26.09.14 교차 리뷰가 HIGH 로 짚었다.
    #   정본은 `plugin.json` 이다(Claude Code 가 읽는다).
    _v_plugin = _version_of(_PLUGIN_JSON, "version")
    _v_skill = _frontmatter_version(_SKILL_MD)
    if _v_skill is None:
        fails.append("SKILL.md frontmatter 에서 version 을 못 읽었다")
    elif _v_plugin != _v_skill:
        fails.append("버전이 갈렸다 — plugin.json=%s · SKILL.md=%s "
                     "(정본은 plugin.json)" % (_v_plugin, _v_skill))

    # [26.09.14] 스킬이 안내하는 실행 경로가 홈 경로로 되돌아가면 **플러그인
    #   설치 환경에서 그 자리에 파일이 없어 리뷰가 아예 안 돌아간다.**
    #   ⚠ 홈 경로 자체를 금지하지는 않는다 — 손으로 배치해 쓰는 경우를 설명하는
    #     산문이 정당하게 있다. 재는 것은 **실행 예시**, 즉 코드 펜스 안이거나
    #     인라인 코드로 감싼 명령줄이다.
    #   ⛔ **문장의 한국어 낱말로 예외를 가르지 않는다** [26.09.14 교차 리뷰
    #     MEDIUM]. 종전에는 `"손으로 배치" not in ln` 처럼 특정 문구에 기댔는데,
    #     기여자가 같은 뜻을 다르게 쓰면 정당한 편집이 회귀로 오탐된다.
    #   ⛔ **경로의 생김새로도 가르지 않는다** [26.09.14 교차 리뷰 2회차 HIGH].
    #     그다음 판이 `"~/.claude/skills" in ln` 으로 **홈 경로 모양**을 찾았는데,
    #     Windows 예시(`$env:USERPROFILE\.claude\skills\…`)나 절대 경로를 적으면
    #     그 조건이 거짓이라 **조용히 빠져나갔다.** 이 문서에는 실제로 Windows
    #     경로가 있으므로 도달 가능한 우회로였다.
    #   ⛔ **마크다운 펜스도 파싱하지 않는다** [같은 라운드 MEDIUM]. ``` 만 보면
    #     `~~~` 펜스와 4칸 들여쓰기 블록을 놓친다(이 문서에 들여쓰기 블록이 있다).
    #   → 술어를 뒤집는다: **인터프리터가 `gemini_review.py` 를 인자로 받는
    #     명령 꼴**이면 그것이 실행 예시이고, 거기에 `${CLAUDE_PLUGIN_ROOT}` 가
    #     없으면 회귀다. 경로 모양도 블록 형식도 보지 않으므로 셋 다 닫힌다.
    #   ⚠ **낱말 두 개가 한 줄에 있는 것만으로는 안 된다** [4회차 MEDIUM].
    #     "이 플러그인은 python 런타임에서 gemini_review.py 를 호출합니다" 같은
    #     **산문이 오탐**된다. 그래서 인터프리터 **바로 뒤에 그 경로가 오는지**를
    #     본다 — 사이에 다른 말이 끼면 명령이 아니다.
    #   ⚠ 인터프리터 이름을 하나로 단정하지 않는다 — 우분투에는 `python` 이
    #     없고 Windows 기본 설치에는 `python3` 가 없다(`.gemini-review.md`).
    #   ⚠ **인터프리터 옵션을 허용한다** [5회차 HIGH]. `python -u <경로>` 처럼
    #     사이에 옵션이 끼면 종전 패턴이 매칭에 실패해 **조용히 빠져나갔다.**
    #     다만 아무거나 허용하면 산문이 다시 오탐되므로 `-옵션` 꼴만 받는다.
    #   ⛔ **판정은 줄 전체가 아니라 '매칭된 명령' 안에서 한다** [5회차 HIGH].
    #     줄 단위로 보면 명령은 옛 경로인데 **같은 줄 주석에 변수 이름만 적어도**
    #     통과했다(`… gemini_review.py  # ${CLAUDE_PLUGIN_ROOT} 권장`).
    #   ⛔ **[5회차 MEDIUM 기각]** *"수동 설치 안내에 복사 가능한 전체 명령을
    #     넣으면 오탐된다"* 는 지적이 있었다. 사실이지만 **의도다** — 그런 줄이
    #     문서에 생기면 그것은 복사되어 실행되고, 플러그인 사용자가 복사하면
    #     파일을 못 찾는다. 지금 문서는 수동 설치를 **경로만** 인라인으로
    #     적어(명령 꼴이 아니라) 이 가드에 걸리지 않는다. 정말 필요해지는 날
    #     그 자리에서 예외를 설계할 것 — 아직 오지 않은 요구를 위해 가드를
    #     미리 느슨하게 두면 **실제 회귀를 놓친다.**
    #   ⚠ **따옴표 안에는 공백이 올 수 있다** [6회차 HIGH]. Windows 경로가
    #     `"C:\Old Plugins\gemini_review.py"` 처럼 띄어쓰기를 품으면 종전
    #     패턴이 공백에서 끊겨 **매칭 자체가 실패**했다 — 명백히 틀린 경로인데
    #     조용히 통과한다. 따옴표 갈래를 따로 둔다.
    #     ⚠ 따옴표 **없는** 갈래는 공백을 계속 막는다 — 안 그러면 산문
    #       ("python 런타임에서 gemini_review.py")이 다시 걸린다.
    _CMD_RE = re.compile(
        r'(?:python3?|py)\b(?:\s+-\S+)*\s+'
        r'(?:"[^"]*gemini_review\.py"|[^\s"]*gemini_review\.py)')
    with io.open(_SKILL_MD, encoding="utf-8") as fh:
        _skill_lines = fh.read().splitlines()
    if not any("${CLAUDE_PLUGIN_ROOT}" in ln for ln in _skill_lines):
        fails.append("SKILL.md 가 ${CLAUDE_PLUGIN_ROOT} 를 쓰지 않는다 — "
                     "플러그인으로 설치하면 안내된 경로에 파일이 없다")
    _bad = []
    for ln in _join_shell_continuations(_skill_lines):
        _m = _CMD_RE.search(ln)
        if _m and "${CLAUDE_PLUGIN_ROOT}" not in _m.group(0):
            _bad.append(ln.strip())
    if _bad:
        fails.append("실행 예시가 ${CLAUDE_PLUGIN_ROOT} 를 쓰지 않는다 (%d줄): %s"
                     % (len(_bad), _bad[0][:70]))

    for path, want, why in _PATH_CASES:
        got = gr._classify_path(path)
        if got != want:
            fails.append("_classify_path(%r) → %r, 기대 %r  [%s]" % (path, got, want, why))

    for payload, want in _EXIT_CASES:
        got = gr._exit_code_for(payload)
        if got != want:
            fails.append("_exit_code_for(%r) → %r, 기대 %r" % (payload, got, want))

    # 코드로만 확인할 수 있는 가드들
    if gr.EXIT_REQUEST_CHANGES != 5 or gr.EXIT_UNSTRUCTURED != 6:
        fails.append("종료코드 상수가 바뀌었다 (5·6 이어야 한다)")

    # [26.09.14] 문자열 검사 넷(상대경로 스킵 · tmpdir 정리 · --mode plan ·
    #   텍스트 재시도 종료코드)을 동작 검사로 바꿨다 — 위 「동작 검사」 주석 참조.
    behavior_total = 0
    skipped = []
    for check in _BEHAVIOR_CHECKS:
        try:
            for failure in check(gr):
                if isinstance(failure, _Skip):
                    skipped.append("%s: %s" % (check.__name__, failure.reason))
                    continue
                behavior_total += 1
                if failure:
                    fails.append(failure)
        except Exception as exc:          # 검사 자체가 죽어도 조용히 넘기지 않는다
            behavior_total += 1
            fails.append("%s 가 예외로 끝났다: %r" % (check.__name__, exc))

    # [26.08.27] _safe_print 폴백이 utf-8 이면 치환이 일어나지 않아 두 번째
    # UnicodeEncodeError 로 죽는다 — 리뷰 결과를 한 줄도 못 남긴다.
    class _NoEnc(io.TextIOBase):
        encoding = None
        def write(self, s):
            s.encode("cp949")
            return len(s)
    _orig = sys.stdout
    sys.stdout = _NoEnc()
    try:
        gr._safe_print("⛔ 인코딩 폴백 회귀")
        _crashed = False
    except UnicodeEncodeError:
        _crashed = True
    finally:
        sys.stdout = _orig
    if _crashed:
        fails.append("_safe_print 폴백이 두 번째 UnicodeEncodeError 로 죽는다")

    # ⚠ 뒤의 상수는 **손으로 센** 코드 검사 수다(표 두 개와 동작 검사가 아닌 것들).
    #   가드를 추가하면 여기도 함께 올릴 것 — 안 올리면 개수만 조용히 어긋난다.
    #   [26.09.14] 6 → 9: 버전 일치(plugin.json ↔ SKILL.md) ·
    #   `${CLAUDE_PLUGIN_ROOT}` 존재 · 실행 예시 회귀.
    #   [26.09.14] 9 → 5: 문자열 검사 넷을 동작 검사로 옮겼다(`behavior_total`).
    total = len(_PATH_CASES) + len(_EXIT_CASES) + 5 + behavior_total
    # 건너뛴 검사는 조용히 넘기지 않는다 — 결과 앞에 이유와 함께 남긴다.
    for sk in skipped:
        print("  - 건너뜀: %s" % sk)
    if fails:
        print("회귀 %d건 / 검사 %d건" % (len(fails), total))
        for f in fails:
            print("  ✗ %s" % f)
        return 1
    print("통과 — 검사 %d건%s" % (total, " (건너뜀 %d건)" % len(skipped) if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
