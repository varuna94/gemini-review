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

import importlib.util
import inspect
import os
import io
import sys

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


def main():
    gr = _load()
    fails = []

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
    if "not os.path.isabs(cand)" not in inspect.getsource(gr._find_agy):
        fails.append("_find_agy 의 상대경로 스킵이 사라졌다 — 저장소가 심은 agy 가 실행될 수 있다")
    src = open(_TARGET, encoding="utf-8").read()
    if "atexit.register" not in src:
        fails.append("tmpdir 정리가 사라졌다 — /tmp 에 diff 평문이 쌓인다")
    if "--mode" not in src or '"plan"' not in src:
        fails.append("--mode plan 고정이 사라졌다 — Gemini 가 파일을 수정할 수 있게 된다")

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

    # [26.08.27] agy 가 비정상 종료했는데 stdout 을 리뷰 원문으로 삼으면,
    # 에러 문구가 exit 6(구조화 실패)의 '리뷰 결과'로 저장된다.
    if "proc.returncode != 0" not in src:
        fails.append("_retry_as_text 가 agy 종료 코드를 보지 않는다")

    total = len(_PATH_CASES) + len(_EXIT_CASES) + 6
    if fails:
        print("회귀 %d건 / 검사 %d건" % (len(fails), total))
        for f in fails:
            print("  ✗ %s" % f)
        return 1
    print("통과 — 검사 %d건" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
