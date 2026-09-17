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
import re
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, os.pardir))
_TIMEOUT = 300

_G = os.path.join("plugins", "gemini-review", "skills", "gemini-review", "gemini_review.py")
_S = os.path.join("plugins", "gemini-review", "skills", "gemini-review", "SKILL.md")
_H = os.path.join("plugins", "gemini-review", "hooks", "commit_gate.py")
_SH = os.path.join("plugins", "gemini-review", "hooks", "commit-gate.sh")

# (이름, 대상 파일, [(원문, 변이), …], 기대 실패 문구 일부)
#   여러 쌍은 **함께** 적용한다 — 두 겹 방어는 한 겹만 지우면 동작이 같아(등가 변이) 살아남는다.
_MUTATIONS = [
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
     [("                              stdin=subprocess.DEVNULL, timeout=hard)\n    except subprocess.TimeoutExpired as exc:",
       "                              stdin=subprocess.DEVNULL)\n    except subprocess.TimeoutExpired as exc:")],
     "하드 상한"),
    # [1.5.0 C1] 가장 비싼 실패(쿼터 + 하드 상한)를 지키는 두 겹. 각각 따로 되돌린다.
    ("C1 하드 상한의 부분 출력 버리기", _G,
     [("        return _AgyRun(None, stdout=_decode_stream(exc.stdout),\n                       stderr=_decode_stream(exc.stderr),\n                       elapsed=time.time() - started",
       "        return _AgyRun(None,\n                       elapsed=time.time() - started")],
     "하드 상한의 부분 출력을 버렸다"),
    ("C1 하드 상한 앞의 쿼터 선판정 제거", _G,
     [('        detail = _agy_error_detail(run.stdout, run.stderr)\n        if _looks_like_quota_error(detail):\n            return "quota", "", _first_line(detail)\n        return "timeout", "hard",',
       '        return "timeout", "hard",')],
     "하드 상한 + stderr 쿼터(C1)"),
    # [1.5.0 A2] 해시가 기기마다 달라지는 경로와, 해시 자체가 빠지는 경로를 각각 죽인다.
    ("A2 diff 를 텍스트 모드로 쓰기", _G,
     [('    with open(diff_path, "wb") as f:\n        f.write(diff_bytes)',
       '    with open(diff_path, "w", encoding="utf-8", newline="\\r\\n") as f:\n        f.write(diff)')],
     "CRLF"),
    ("A2 finish 의 diff_sha256 대입 제거", _G,
     [('        if state["diff_sha256"]:\n            meta["diff_sha256"] = state["diff_sha256"]\n'
       '            meta["diff_bytes"] = state["diff_bytes"]\n', "")],
     "diff_sha256"),
    ("A3 detached HEAD 가드 제거", _G,
     [('        try:\n            return _git(["symbolic-ref", "--short", "-q", "HEAD"], root).strip() or None\n        except RuntimeError:\n            return None',
       '        return _git(["symbolic-ref", "--short", "-q", "HEAD"], root).strip() or None')],
     "detached HEAD"),
    ("시각을 로컬로", _G,
     [('    return datetime.now(timezone.utc).replace(tzinfo=None)',
       '    return datetime.now()')],
     "UTC 가 아니다"),
    # [1.5.0 A4] 쿼터 단락 — 실측 609초의 낭비를 없애는 자리. 세 겹을 각각 죽인다.
    ("A4 단락을 건너뛴다", _G,
     [("    if not args.ignore_quota_cache:\n        state_path = _quota_guard(_quota_state_path)",
       "    if False:\n        state_path = _quota_guard(_quota_state_path)")],
     "구조화 호출 1회 (기대 0)"),
    ("A4 차단 판정이 여유를 둔다", _G,
     [("        if isinstance(frac, bool) or not isinstance(frac, (int, float)) or frac > 0:",
       "        if isinstance(frac, bool) or not isinstance(frac, (int, float)) or frac > 0.01:")],
     "구조화 호출 0회 (기대 1)"),
    ("A4 상태 부작용이 종료 코드를 바꾼다", _G,
     [('        _say_note("쿼터 상태를 다루지 못했다",', '        raise exc\n        _say_note("쿼터 상태를 다루지 못했다",')],
     "상태 기록이 실패해도"),
    ("A4 _parse_hms 가 빈 문자열을 받는다", _G,
     [("    if not m or not any(m.groups()):\n        return None",
       "    if not m:\n        return None")],
     None),
    # [1.5.0 A5] 집계 — 파일 하나로 전체를 잃지 않고, 약어로 리뷰 인자가 새지 않는다.
    ("A5 깨진 파일이 전체 집계를 죽인다", _G,
     [("        row = _stats_read(path)\n        if row is None:\n            unreadable += 1\n            continue",
       "        row = _stats_read(path)\n        if row is None:\n            unreadable += 1")],
     "파일 하나로 전체 집계를 잃는다"),
    ("A5 충돌 검사가 약어를 놓친다", _G,
     [("            if name.startswith(head) and name not in hit:",
       "            if name == head and name not in hit:")],
     "_named_in_argv"),
    ("A5 루프 종류를 가르지 않는다", _G,
     [("        (grew, fixed) = (grew + 1, fixed) if sizes[-1] > sizes[0] * 1.5 else (grew, fixed + 1)",
       "        (grew, fixed) = (grew, fixed + 1)")],
     "루프 종류를 가르지 못했다"),
    # [26.09.16 교차리뷰 HIGH] 로컬 시각 → UTC 부호 역전. CI 는 UTC 라 시간대를 강제해야 잡힌다.
    ("A5 파일명 시각을 로컬 그대로 쓴다", _G,
     [("    try:\n        stamp = time.mktime(local.timetuple())\n    except (OverflowError, ValueError):\n        return None\n    return datetime(1970, 1, 1) + timedelta(seconds=stamp)",
       "    return local")],
     "_file_time [Asia/Seoul]"),
    # [1.5.1] 스위트 전체 상태 샌드박스 — 격리를 잊은 검사가 있어도 진짜 폴더에 못 쓰게 하는 보호막.
    ("스위트 상태 샌드박스를 안 건다", os.path.join("tests", "test_gemini_review.py"),
     [("    _isolate_suite_state()                # ⛔ 검사보다 먼저 — 위 docstring 참조\n", "")],
     "스위트 상태 샌드박스가 걸려 있지 않다"),
    # [1.5.1] 격리 가드가 헬퍼 구간을 `end_lineno` 로 잰다 — 3.12 에서는 똑같고 3.7 에서만 죽는다.
    ("격리 가드가 end_lineno 에 기댄다", os.path.join("tests", "test_gemini_review.py"),
     [("            helper_nodes.update(id(c) for c in ast.walk(n))\n",
       "            helper_nodes.update(id(c) for c in ast.walk(n)\n"
       "                                if getattr(c, \"lineno\", 0) <= (getattr(n, \"end_lineno\", None) or n.lineno))\n")],
     "end_lineno 가 없는 파이썬(3.7)"),
    # [1.5.1] 정의만 있고 등록 안 된 검사(죽은 검사)를 잡는 가드 — 실제로 한 번 새었다.
    ("검사 하나를 실행 목록에서 뺀다", os.path.join("tests", "test_gemini_review.py"),
     [("    _check_hms_parser,\n", "")],
     "정의만 있고 실행 목록에 없는 검사"),
    # [1.6.0] 커밋 게이트 — 조용히 열리는 갈래를 하나씩 되돌린다.
    # [1.7.0] 커밋 형태를 막지 않고 **담을 내용을 모사**한다 — 모사의 각 갈래를 되돌린다.
    ("게이트가 -a 를 스테이징 그대로로 읽는다", _H,
     [('_COMMIT_CONTENT_FLAGS = {"a": "all", "i": "include", "o": "only"}',
       '_COMMIT_CONTENT_FLAGS = {"i": "include", "o": "only"}')],
     "게이트 커밋 형태 [-am x]"),
    ("게이트가 커밋 앞의 모사하지 않는 인덱스 명령을 허용", _H,
     [('        elif step["kind"] in ("git", "opaque") and step["sub"] not in _READ_ONLY_GIT:',
       '        elif False:')],
     "모사하지 않는 인덱스 명령"),
    ("게이트가 앞선 git add 를 모사에 넘기지 않는다", _H,
     [('            adds.append((add_dir, list(step["args"])))\n', '            pass\n')],
     "add -A 가 리뷰 안 된 새 파일을 더한다"),
    ("모사가 앞선 git add 를 버린다", _G,
     [('        for add_cwd, add_args in adds:\n            _git(["add"] + list(add_args), add_cwd, env=env)\n', '')],
     "커밋 모사 [git add b.py && git commit -m x]"),
    ("경로 지정 커밋을 HEAD 가 아니라 스테이징 위에서 모사한다", _G,
     [('        elif mode == "only":\n', '        elif False:\n')],
     "커밋 모사 [git commit -m x -- b.py c.py]"),
    ("경로 지정 모사가 스테이징된 삭제를 빠뜨린다", _G,
     [('            known |= set(_git(["diff", "--cached", "--name-only", "-z", "--diff-filter=D", "--"]\n'
       '                              + list(pathspec), where, _DIFF_CONFIG, env=env).split("\\0"))\n', '')],
     "커밋 모사 [git commit -m x -- f.py]"),
    ("경로 지정 모사가 파일 목록을 명령줄로 펼친다", _G,
     [('                _git(["update-index", "--add", "--remove", "-z", "--stdin"], root, env=env,\n'
       '                     stdin=b"".join(p.encode("utf-8") + b"\\0" for p in sorted(known)))\n',
       '                _git(["update-index", "--add", "--remove", "--"] + sorted(known), root, env=env)\n')],
     "명령줄로 펼친다"),
    ("게이트가 옵션 값을 설정 키로 읽는다", _H,
     [('            if name in _CONFIG_VALUE_OPTS and "=" not in a:\n                k += 1                   # 옵션 값은 키가 아니다\n', '')],
     "게이트 설정 보호 [--file gemini-review.conf user.name foo]"),
    ("모사가 진짜 인덱스를 쓴다", _G,
     [('        env = dict(os.environ, GIT_INDEX_FILE=work)\n', '        env = None\n')],
     "진짜 인덱스"),
    # [1.7.0] 대체 리뷰 기록 — Gemini 가 수행되지 않았을 때만, 운영자가 끄지 않았을 때만.
    ("대체 리뷰 기록이 Gemini 시도를 확인하지 않는다", _G,
     [('    why = None\n    if attempt is None:\n', '    why = None\n    if False:\n')],
     "대체 리뷰 기록 [Gemini 시도 없음]"),
    ("대체 리뷰가 Gemini 의 지적을 덮는다", _G,
     [('    elif mode == "reviewed":\n        why = ("같은 내용에 Gemini 가 판정을 냈다',
       '    elif False:\n        why = ("같은 내용에 Gemini 가 판정을 냈다')],
     "대체 리뷰 기록 [Gemini 가 지적을 낸 뒤]"),
    ("게이트가 운영자의 대체 리뷰 끄기를 무시한다", _H,
     [('        if fallback_state(where) == "on":\n            return EXIT_ALLOW, ""',
       '        if True:\n            return EXIT_ALLOW, ""')],
     "대체 리뷰를 끈 저장소"),
    ("게이트가 대체 리뷰 기록을 인정하지 않는다", _H,
     [('    if mode == "fallback_reviewed" and code == 0:\n', '    if False:\n')],
     "대체 리뷰 기록 뒤"),
    ("--paths 리뷰가 경로 지정 모사를 쓰지 않는다", _G,
     [('            diff, files = _commit_diff(root, "only", args.paths, os.getcwd())\n',
       '            diff, files = _collect_diff(root, None, None, True)\n')],
     "리뷰한 경로 지정 커밋"),
    ("게이트가 최신 결과 대신 통과 기록을 찾는다", _H,
     [('        if meta.get("diff_sha256") == sha:\n            return meta, latest_here',
       '        if meta.get("diff_sha256") == sha and meta.get("passed") is True:\n            return meta, latest_here')],
     "통과 뒤 같은 내용이 request_changes"),
    ("게이트가 통과 아닌 결과를 허용", _H,
     [('    if match.get("passed") is True and mode == "reviewed" and code == 0:',
       '    if mode == "reviewed":')],
     "통과 뒤 같은 내용이 request_changes"),
    ("게이트 해시를 git 원본 바이트로 잰다", _H,
     [('    sha = gr._sha256_hex(gr._diff_bytes(diff))',
       '    sha = gr._sha256_hex(diff.encode("utf-8") + b"\\n")')],
     "통과 리뷰 뒤"),
    ("게이트가 다른 저장소의 결과로 판정", _H,
     [('        if not (isinstance(scope.get("repo"), str)\n                and os.path.realpath(scope["repo"]) == real_root):\n            continue\n', '')],
     "게이트가 다른 저장소의 결과로 판정했다"),
    ("게이트가 대상을 모르면 현재 폴더 설정만 본다", _H,
     [('    here = gate_state(cwd)\n    if here != "off":\n        return here\n',
       '    return gate_state(cwd)\n')],
     "게이트 대상 판정 [전역으로 켰고 대상을 확정할 수 없다]"),
    ("셸 진입점이 전역 설정을 보지 않는다", _SH,
     [('if [ "$global" != "true" ] && {', 'if {')],
     "저장소는 끄고 전역은 켬"),
    ("게이트가 셸 안으로 GIT_DIR 을 넘기지 않는다", _H,
     [('                outer_unknown = bool(chdirs) or repo_env or any(\n                    a.split("=", 1)[0] in _REPO_ENV for a in assigns)\n',
       '                outer_unknown = bool(chdirs)\n')],
     "게이트 저장소 위치 [GIT_DIR=/elsewhere/.git bash -c"),
    ("게이트가 env -C 의 폴더 이동을 버린다", _H,
     [('                if name in _WRAPPER_CHDIR.get(w, ()):\n                    chdirs.append(value)\n',
       '                if name in _WRAPPER_CHDIR.get(w, ()):\n                    pass\n')],
     "게이트 저장소 위치 [env -C /elsewhere git commit -m x]"),
    ("게이트 내부 오류를 통과시킨다", _H,
     [('    except Exception as exc:\n        lowered = raw.lower()',
       '    except Exception as exc:\n        return EXIT_ALLOW, ""\n        lowered = raw.lower()')],
     "게이트 내부 오류 [gate=true]"),
    ("게이트 끄기를 허용", _H,
     [('    return "게이트 설정을 바꾸는 명령"\n', '    return None\n')],
     "게이트 설정 보호 [--global gemini-review.gate false]"),
    ("게이트가 다른 gemini-review 설정 쓰기를 허용", _H,
     [('    if key == GATE_KEY and value in ("true", "yes", "on", "1"):',
       '    if value in ("true", "yes", "on", "1"):')],
     "게이트 설정 보호 [gemini-review.gateFallback true]"),
    ("heredoc 본문을 명령으로 읽는다", _H,
     [('            for delim, strip in heredocs:\n                i = _skip_heredoc(text, i, delim, strip)\n', '')],
     "게이트 명령 해석"),
    ("셸 진입점이 게이트 비정상 종료를 통과", _SH,
     [('  if [ "$rc" -eq 0 ] || [ "$rc" -eq 2 ]; then', '  if true; then')],
     "게이트 스크립트 비정상 종료"),
    ("셸 빠른 경로가 commit 을 건너뛴다", _SH,
     [('  *commit*|*gemini-review.gate*|*-section*gemini-review*) ;;',
       '  *gemini-review.gate*|*-section*gemini-review*) ;;')],
     "파이썬 없음 · 켜진 저장소"),
    ("hooks.json 매처 오타", os.path.join("plugins", "gemini-review", "hooks", "hooks.json"),
     [('"matcher": "Bash"', '"matcher": "bash"')],
     "hooks.json 에 PreToolUse(Bash)"),
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
     [('            shutil.rmtree(ctx["tmpdir"], True)\n        return _record_interrupt(',
       '            shutil.rmtree(os.path.dirname(os.path.dirname(ctx["tmpdir"])), True)\n        return _record_interrupt(')],
     "상위를 지"),
    ("프롬프트가 명령 시도를 막지 않음", _G,
     [("        _NO_COMMANDS,\n", "")],
     "명령 시도 금지 줄이 없다"),
    # ── agy 원인 분류 (1.4.0 — 세 호출이 `_classify_run` 한 곳을 쓴다) ──
    ("agy 실패 판정에서 종료 코드 무시", _G,
     [('    if run.rc != 0 or (structured and _wrapper_status(raw) == "ERROR"):',
       '    if structured and _wrapper_status(raw) == "ERROR":')],
     "agy 종료코드 1 의 출력을 리뷰로"),
    ("agy 실패를 빈 응답으로", _G,
     [('    if run.rc != 0 or (structured and _wrapper_status(raw) == "ERROR"):',
       "    if run.rc != 0 and not raw:")],
     "생존 확인 · 폴백 없음"),
    ("출력 시간 초과 검사 제거", _G,
     [("    if _is_print_timeout(err):\n        return \"timeout\", \"agy\"",
       "    if False:\n        return \"timeout\", \"agy\""),
      ("    if not structured and _has_agy_timeout_line(raw):", "    if False:")],
     "잘린 부분 출력을 리뷰로"),
    ("구조화 시간 초과를 빈 응답으로", _G,
     [("    if _is_print_timeout(err):\n        return \"timeout\", \"agy\"",
       "    if False:\n        return \"timeout\", \"agy\"")],
     "계약표 [agy 출력 시간 초과]"),
    ("도구 권한 거부 원인을 숨김", _G,
     [("        denied = _denied_tool_notice(err)\n        return (\"tool_denied\"",
       "        denied = ''\n        return (\"tool_denied\"")],
     "빈 응답의 원인(도구 권한 거부)"),
    # ⚠ [1.5.0 C1] 쿼터 판정 자리가 둘이 됐다(rc≠0 갈래 · 하드 상한 선판정). 앞 줄을 붙여
    #   **원래 자리만** 겨냥한다 — 선판정 쪽은 바로 아래 전용 항목이 따로 죽인다.
    ("쿼터를 도구 오류로", _G,
     [("        detail = _agy_error_detail(raw, err)\n        if _looks_like_quota_error(detail):\n",
       "        detail = _agy_error_detail(raw, err)\n        if False:\n")],
     "쿼터"),
    ("시간 초과 뒤 재시도", _G,
     [('_FAILURE_CAUSES = ("tool_error", "model_unavailable", "quota", "timeout")',
       '_FAILURE_CAUSES = ("tool_error", "model_unavailable", "quota")')],
     "계약표 [agy 출력 시간 초과]"),
    # ── 결과 계약 (1.4.0) ──
    ("진단 모델로 리뷰 요청(1.3.x 폴백 판정 복귀)", _G,
     [("    # ② 살아 있다 → 형식을 포기하고",
       "    _fb = _resolve_probe_model(args)\n"
       "    if _fb:\n"
       "        _r2 = _invoke_schema(agy, _fb, args, root, os.path.join(os.path.dirname(diff_path), 'schema.json'), '')\n"
       "        _p2 = _extract_json(_r2.stdout)\n"
       "        if _p2 is not None:\n"
       "            return 'reviewed', dict(_p2), {'rc': _exit_code_for(_p2)}\n"
       "    # ② 살아 있다 → 형식을 포기하고")],
     "주 모델이 아닌 모델로 리뷰를 요청"),
    ("LLM 응답 키를 거르지 않음", _G,
     [("    review = dict((k, payload[k]) for k in _REVIEW_KEYS if k in payload)",
       "    review = dict(payload)")],
     "심은 결과 계약 키"),
    ("판정 모델 기록이 LLM 값을 따름", _G,
     [("    review = dict((k, payload[k]) for k in _REVIEW_KEYS if k in payload)",
       "    review = dict(payload)"),
      ('        body["model"] = meta["model"]', '        body.setdefault("model", meta["model"])')],
     "실제 판정 모델"),
    ("실패 경로 최상위 model 누락(1.3.x 호환)", _G,
     [('        body = dict(body)\n        body["model"] = meta["model"]', '        body = dict(body)')],
     "최상위 model"),
    ("passed 가 종료 코드만 봄", _G,
     [('    return mode == "reviewed" and exit_code == EXIT_PASSED',
       "    return exit_code == EXIT_PASSED")],
     "passed"),
    ("빈 스테이징 exit 0 복귀", _G,
     [("        if args.staged and not args.allow_empty:", "        if False:")],
     "스테이징 비어 있음"),
    ("내부 오류를 기록하지 않음", _G,
     [("    except Exception as exc:\n        # ⛔ [26.09.14 Eng 교차리뷰 → 1.4.0]",
       "    except ZeroDivisionError as exc:\n        # ⛔ [26.09.14 Eng 교차리뷰 → 1.4.0]")],
     "_check_result_contract_matrix 가 예외로"),
    ("도움말에 cp949 밖 문자", _G,
     [('    ("3", "민감 경로: 전송 중단"),', '    ("3", "민감 경로 \u2014 전송 중단"),')],
     "cp949"),
    # ── --check (1.4.0) ──
    ("점검이 저장소에서 agy 를 돌림", _G,
     [('    where = tempfile.mkdtemp(prefix="gemini_review_check_")\n',
       '    where = root or cwd\n'),
      ("        shutil.rmtree(where, True)\n\n\ndef _check_agy_surface(",
       "        pass\n\n\ndef _check_agy_surface(")],
     "코드를 보내면 안 된다"),
    ("점검이 시간 초과 안내 변화를 못 알아봄", _G,
     [('    if cause == "timeout" and kind == "agy":\n        mark("시간 초과 안내", True',
       '    if True:\n        mark("시간 초과 안내", True')],
     "--check [시간 초과 안내문이 바뀜]"),
    ("점검이 래퍼 변화를 모델 무응답으로", _G,
     [("    if missing:\n        mark(\"모델 응답\", None,", "    if False:\n        mark(\"모델 응답\", None,")],
     "--check [래퍼 필드가 바뀜]"),
    # ⚠ [1.4.0 교차리뷰 HIGH] 원문을 `## 1.4.0 …` 로 박아 두면 다음 판 절이 추가된 뒤에는 옛 절만
    #   바꿔 **거짓 실패**가 난다 — 지금 판을 스크립트에서 읽어 그 절을 바꾼다(`_current_version_heading`).
    ("CHANGELOG 에 지금 판이 없음", "CHANGELOG.md",
     lambda: _current_version_heading(),
     "CHANGELOG.md 에 지금 판"),
    ("변이 검사가 cp949 보호를 잃음", os.path.join("tests", "mutation_check.py"),
     [("    _make_stdout_safe()\n    base = tempfile.mkdtemp", "    base = tempfile.mkdtemp")],
     "cp949 에서 결과를 끝까지"),
    ("중단 · 내부 오류 기록이 model 을 잃음", _G,
     [('    ctx["model"] = args.model\n', "    pass\n")],
     "중단 기록의 model"),
]


def _current_version_heading():
    """CHANGELOG.md 에서 **지금 판**(스크립트 `__version__`) 절 제목을 찾아 (원문, 변이) 쌍으로 돌려준다."""
    with io.open(os.path.join(_REPO, _G), encoding="utf-8") as fh:
        m = re.search(r'^__version__ = "([^"]+)"', fh.read(), re.M)
    ver = m.group(1) if m else "?"
    with io.open(os.path.join(_REPO, "CHANGELOG.md"), encoding="utf-8") as fh:
        heads = [ln for ln in fh.read().splitlines()
                 if ln == "## " + ver or ln.startswith("## %s " % ver)]
    # 못 찾으면 원문이 없는 쌍을 돌려준다 → "정확히 한 번 나오지 않는다" 로 드러난다(조용히 넘기지 않는다).
    head = heads[0] if len(heads) == 1 else "## %s (CHANGELOG 에서 못 찾음)" % ver
    return [(head + "\n", head.replace("## " + ver, "## 0.0.0-mutated", 1) + "\n")]


def _make_stdout_safe():
    """cp949 콘솔에서 결과를 찍다 죽지 않게 한다(`tests/test_gemini_review.py` 와 같은 보호).

    ⛔ [1.4.0 교차리뷰 HIGH — 재현] 이 스크립트는 보호 없이 `print` 로 em dash 를 찍어,
      `PYTHONIOENCODING=cp949` 에서 적용 실패를 알리는 첫 줄에서 UnicodeEncodeError 로 죽었다 —
      **무엇이 실패했는지 한 줄도 못 본다.** 1.3.2 부터 있던 결함이다.
    """
    for stream in ("stdout", "stderr"):
        try:
            getattr(sys, stream).reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


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
    _make_stdout_safe()
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
            if callable(pairs):
                pairs = pairs()
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
