#!/usr/bin/env python3
# [26.09.14] 변이 검사 — "가드가 실제로 빨개지는가" 를 매번 다시 증명한다.
#
# 왜 있는가: 이 저장소의 가드는 두 번 **조용히 죽어 있었다**(문자열 존재 검사가 다른
# 함수의 같은 문자열로 통과). 테스트가 초록이라는 것만으로는 가드가 살아 있다는 증거가
# 아니다. 여기서는 코드를 한 줄씩 되돌린 복사본에서 스위트를 돌려, 기대한 검사가 **그
# 이유로** 빨개지는지 본다.
#
#   python3 tests/mutation_check.py
#
# 종료 코드: 0 전부 기대대로 / 1 살아남은 변이 · 적용 실패 · 대조군 실패
#
# ⛔ 안전 장치 (26.09.14 사고 — 변이 아래에서 테스트가 `rmtree("/tmp")` 를 실행):
#   · 복사본은 새 임시 폴더에 만들고, 자식 스위트의 TMPDIR · TEMP · TMP 를 **그 안 세 겹
#     아래의 격리 폴더**로 돌린다. 각 층에 카나리를 두고 변이마다 살아 있는지 본다.
#     (같은 날 두 번째 사고: 대상 코드의 정리 경로를 부모 쪽으로 바꾼 변이가 격리 폴더의
#     상위를 지웠다 — 깊이가 없으면 격리 폴더 바깥이 곧 호출자의 작업 폴더다.)
#   · 이 스크립트가 쓰는 파일은 전부 그 임시 폴더 안에 있다. 호출자의 TMPDIR 에 소중한
#     것을 두지 말 것 — 격리의 마지막 층은 결국 거기다.
#   · 기대 실패 문구가 맞아야 "빨강" 으로 친다 — 무관한 검사(버전 · 경로)가 빨개진 것을
#     변이 검출로 오인하지 않는다.
#   · 변이 없는 복사본이 초록인지 **대조군**으로 먼저 확인한다.

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, os.pardir))
_TIMEOUT = 300

_G = os.path.join("plugins", "gemini-review", "skills", "gemini-review", "gemini_review.py")
_S = os.path.join("plugins", "gemini-review", "skills", "gemini-review", "SKILL.md")

# (이름, 대상 파일, [(원문, 변이), …], 기대 실패 문구 일부)
#   여러 쌍은 **함께** 적용한다 — 두 겹 방어는 한 겹만 지우면 동작이 같아(등가 변이) 살아남는다.
_MUTATIONS = [
    ("텍스트 재시도 종료 코드 검사 제거", _G,
     [("    if run.rc != 0:\n        _safe_print(\"   텍스트 재시도",
       "    if False:\n        _safe_print(\"   텍스트 재시도")],
     "agy 종료코드 1 의 출력을 리뷰로"),
    ("텍스트 재시도 시간 초과 검사 제거", _G,
     [("    if _is_print_timeout(err) or _has_agy_timeout_line(text):", "    if False:")],
     "잘린 부분 출력을 리뷰로"),
    ("agy 호출 --mode agent", _G,
     [('cmd = [agy, "--mode", "plan",         # ★', 'cmd = [agy, "--mode", "agent",         # ★')],
     "_invoke_schema 의 agy 명령에 `--mode plan`"),
    ("atexit 정리 제거", _G,
     [("    atexit.register(shutil.rmtree, tmpdir, True)\n", "    pass\n")],
     "diff 를 담은 임시 디렉터리"),
    ("생존 확인이 공통 함수를 우회", _G,
     [("    run = _run_agy(agy, model, [\n        \"--output-format\", \"text\",\n        \"--print-timeout\", _PROBE_TIMEOUT,",
       "    run = subprocess.run([agy, \"--model\", model]) and _run_agy(agy, model, [\n        \"--output-format\", \"text\",\n        \"--print-timeout\", _PROBE_TIMEOUT,")],
     "허용 목록 밖에서 프로세스를"),
    ("agy 바깥 하드 상한 제거", _G,
     [("                              stdin=subprocess.DEVNULL, timeout=hard)\n    except subprocess.TimeoutExpired:\n        return _AgyRun(",
       "                              stdin=subprocess.DEVNULL)\n    except subprocess.TimeoutExpired:\n        return _AgyRun(")],
     "하드 상한"),
    ("상대 경로 agy 허용", _G,
     [("        if not cand or not os.path.isabs(cand):\n            continue\n",
       "        if not cand:\n            continue\n")],
     "상대 경로"),
    ("PATH 의 현재 폴더 · 저장소 안 허용", _G,
     [("        if real == cwd or (root_real and _is_within(real, root_real)):\n            continue\n", "")],
     "현재 폴더 · 저장소 안 PATH"),
    ("--out 사전 무효화 제거", _G,
     [("    out_early = _peek_out(argv)\n", "    out_early = None\n")],
     "직전 실행의 approve"),
    ("원자적 기록 제거", _G,
     [('    d = os.path.dirname(os.path.abspath(path))\n    fd, tmp = tempfile.mkstemp(prefix=".gemini_review_", suffix=".tmp", dir=d)\n    try:\n        with os.fdopen(fd, "w", encoding="utf-8") as f:',
       '    tmp = path\n    try:\n        with open(path, "w", encoding="utf-8") as f:')],
     None),
    ("판정 모델 setdefault 복귀", _G,
     [('        payload["model"] = used_model\n', '        payload.setdefault("model", used_model)\n')],
     "실제 판정 모델"),
    ("결과 폴더 링크 검사 제거", _G,
     [("        if (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode)\n                or st.st_uid != os.getuid()):",
       "        if False:")],
     "심볼릭 링크"),
    ("신호 처리기 설치 제거", _G,
     [("    restore, suppress = _install_signal_handlers()\n",
       "    restore, suppress = (lambda: None), (lambda: None)\n")],
     "SIGTERM 처리기를 설치하지 않"),
    ("신호 뒤 처리기 복원 안 함", _G,
     [("    def restore():\n", "    def restore():\n        if state['fired']: return\n")],
     "처리기 미복원"),
    ("agy 실패를 빈 응답으로", _G,
     [('    if run.rc != 0 or _wrapper_status(raw) == "ERROR":',
       "    if run.rc != 0 and not raw:")],
     "생존 확인 · 폴백 없음"),
    ("생존 확인 종료 코드 무시", _G,
     [("    alive = (run.rc == 0\n", "    alive = (run.rc is not None\n")],
     "종료코드 1 의 'OK'"),
    ("리뷰 JSON 둘 이상이면 하나 고름", _G,
     [("    return found[0] if len(found) == 1 else None", "    return found[-1] if found else None")],
     "서로 다른 리뷰 JSON 이 둘"),
    ("git 이름 변경 감지 복귀", _G,
     [('    "-c", "diff.renames=false",\n', "")],
     "이름 변경으로 민감 경로"),
    ("git 실행 예외 두 겹 미처리", _G,
     [("    except FileNotFoundError:\n        # ⛔ [26.09.14 실측] 종전에는 traceback",
       "    except ZeroDivisionError:\n        # ⛔ [26.09.14 실측] 종전에는 traceback"),
      ('    except OSError as exc:\n        raise RuntimeError("git 실행 실패',
       '    except ZeroDivisionError as exc:\n        raise RuntimeError("git 실행 실패')],
     "git 없음"),
    ("exit 3 가 우회를 권함", _G,
     [('        _safe_print("   리뷰는 수행되지 않았다. 목록을 사용자에게 보여 주고, 사용자가")\n        _safe_print("   전송을 명시적으로 승인한 경우에만 --allow-sensitive 로 다시 실행할 것.")',
       '        _safe_print("   의도한 것이면 --allow-sensitive 로 다시 실행하라.")')],
     "승인 조건 없이"),
    ("SKILL.md 가 flash 를 권함", _S,
     [("- 리뷰 1회에 **1~6분** 걸린다. 느리다고 모델을 낮추지 않는다(위 「모델」 절).",
       "- 리뷰 1회에 **1~6분** 걸린다. 급하면 `--model gemini-3.8-flash-high`.")],
     "flash 모델을 --model 로 권한다"),
    ("SKILL.md PowerShell 블록이 종료 코드를 삼킴", _S,
     [("exit $LASTEXITCODE   # ⚠", "# exit $LASTEXITCODE   # ⚠")],
     "삼켜진다"),
    # ⚠ 파괴형 변이다 — 테스트의 가둠(`_contained` · `_nested_tmp`)이 살아 있어야 샌드박스 안에서
    #   멈춘다. 가둠이 빠지면 삭제가 격리 폴더 쪽으로 올라오고, 아래 카나리가 그것을 알린다.
    ("중단 정리가 임시 폴더의 조부모를 지움", _G,
     [('            shutil.rmtree(ctx["tmpdir"], True)\n',
       '            shutil.rmtree(os.path.dirname(os.path.dirname(ctx["tmpdir"])), True)\n')],
     "상위를 지"),
    ("구조화 호출이 도구 권한 거부를 숨김", _G,
     [("    denied = _denied_tool_notice(err)\n    if denied:", "    denied = ''\n    if denied:")],
     "구조화 호출이 빈 응답의 원인"),
    ("텍스트 재시도가 도구 권한 거부를 숨김", _G,
     [('    denied = "" if text else _denied_tool_notice(err)', '    denied = ""')],
     "텍스트 재시도가 빈 응답의 원인"),
    ("프롬프트가 명령 시도를 막지 않음", _G,
     [("        _NO_COMMANDS,\n", "")],
     "명령 시도 금지 줄이 없다"),
]


def _run_suite(root, iso):
    env = dict(os.environ, TMPDIR=iso, TEMP=iso, TMP=iso)
    env.pop("PYTHONIOENCODING", None)
    try:
        proc = subprocess.run([sys.executable, os.path.join(root, "tests", "test_gemini_review.py")],
                              capture_output=True, timeout=_TIMEOUT, env=env, cwd=root)
    except subprocess.TimeoutExpired as exc:
        # ⚠ [26.09.14 Gemini 교차리뷰] 잡지 않으면 변이 하나가 멈추게 만든 스위트가 이 스크립트를
        #   죽여 **나머지 변이 결과와 요약이 사라진다.** 시간 초과는 "기대한 이유로 빨개지지 않음".
        out = (exc.stdout or b"").decode("utf-8", "replace")
        return "시간초과(%d초)" % _TIMEOUT, out
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


def main():
    base = tempfile.mkdtemp(prefix="gr_mutation_")
    iso = os.path.join(base, "i1", "i2", "iso_tmp")
    canaries = [os.path.join(d, "CANARY") for d in (
        base, os.path.join(base, "i1"), os.path.join(base, "i1", "i2"), iso)]

    def plant():
        os.makedirs(iso, exist_ok=True)
        for c in canaries:
            open(c, "w").close()

    failures = []
    try:
        # 준비도 try 안에서 한다 — 여기서 실패해도 base 를 지운다 [26.09.14 Gemini 교차리뷰].
        plant()
        def fresh_copy():
            root = os.path.join(base, "repo")
            shutil.rmtree(root, True)
            shutil.copytree(_REPO, root, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "docs", "gr_mutation_*"))
            return root

        root = fresh_copy()
        rc, out = _run_suite(root, iso)
        print("대조군(변이 없음): exit %s · 대상 sha %s" % (rc, _sha(os.path.join(root, _G))))
        if rc != 0:
            failures.append("대조군이 초록이 아니다 — 변이 결과를 믿을 수 없다")
            print(out[-800:])
            return 1

        for name, rel, pairs, expect in _MUTATIONS:
            with io.open(os.path.join(_REPO, rel), encoding="utf-8") as fh:
                src = fh.read()
            bad = [old for old, _ in pairs if src.count(old) != 1]
            if bad:
                failures.append("%s: 원문이 정확히 한 번 나오지 않는다 — 목록을 갱신할 것" % name)
                print("!! %s — 적용 실패" % name)
                continue
            for old, new in pairs:
                src = src.replace(old, new)
            root = fresh_copy()
            target = os.path.join(root, rel)
            with io.open(target, "w", encoding="utf-8") as fh:
                fh.write(src)
            rc, out = _run_suite(root, iso)
            fails = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("✗")]
            matched = rc == 1 and bool(fails) and (expect is None or any(expect in f for f in fails))
            gone = [os.path.relpath(c, base) for c in canaries if not os.path.exists(c)]
            alive = not gone
            if not alive:
                failures.append("%s: 격리 폴더의 카나리가 사라졌다(%s) — 샌드박스 밖을 지웠다"
                                % (name, ", ".join(gone)))
                plant()
            if not matched:
                failures.append("%s: 기대한 이유로 빨개지지 않았다(exit %s, 기대 문구 %r)" % (name, rc, expect))
            print("%s %s · exit %s · sha %s%s" % ("RED " if matched else "!!  ", name, rc,
                                                  _sha(target), "" if alive else " · 카나리 사라짐"))
            for f in fails[:2]:
                print("       %s" % f[:140])
    finally:
        # base 는 이 스크립트가 만든 gr_mutation_* 폴더다 — 그 밖은 지우지 않는다.
        if os.path.basename(base).startswith("gr_mutation_"):
            shutil.rmtree(base, True)

    if failures:
        print("\n실패 %d건" % len(failures))
        for f in failures:
            print("  ✗ %s" % f)
        return 1
    print("\n전 변이 %d개가 기대한 이유로 빨개졌다" % len(_MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
