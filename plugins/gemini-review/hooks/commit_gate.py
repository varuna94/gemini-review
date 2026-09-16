#!/usr/bin/env python3
"""gemini-review 커밋 게이트 — Claude Code `PreToolUse`(Bash) hook (1.6.0).

Claude 가 Bash 도구로 `git commit` 을 실행하려 할 때, **스테이징된 변경과 같은 바이트로
통과한 리뷰**가 결과 폴더에 없으면 그 명령을 막는다.

왜 있는가: 지금까지 게이트는 "커밋 전에 `/gemini-review` 를 부른다" 는 **지시를 모델이
기억하는 데** 기댔다. 긴 작업 끝에 그 지시가 빠지면 리뷰 없는 커밋이 조용히 들어간다.

켜는 법은 사용자의 git 설정이다 — `git config [--global] gemini-review.gate true`.
**기본은 꺼짐**이다. 꺼진 저장소에서는 아무것도 막지 않는다.

종료 코드(Claude Code hook 규약): 0 허용(평소 권한 흐름으로 넘긴다) · 2 막는다(stderr 가
모델에게 전달된다). 그 밖의 코드는 Claude Code 가 **막지 않고 넘기므로**, 이 모듈은 0 과 2
말고는 내지 않는다 — 진입점 셸(`commit-gate.sh`)도 다른 코드를 받으면 막는 쪽으로 바꾼다.

⚠ 위협 모델은 **망각**이다. 모델이 일부러 게이트를 속이는 경우(결과 파일 위조, 설정 파일
  직접 편집, git alias)는 막지 않는다. 그 대신 모델이 흔히 쓰는 꼴(`git add … && git commit`,
  `-am`, heredoc 메시지)은 해석하고, 해석하지 못한 꼴은 **막는다**(fail-closed).
"""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys

GATE_KEY = "gemini-review.gate"
EXIT_ALLOW = 0
EXIT_BLOCK = 2

_HERE = os.path.dirname(os.path.abspath(__file__))
_REVIEW_SCRIPT = os.path.join(_HERE, os.pardir, "skills", "gemini-review", "gemini_review.py")
_GIT_TIMEOUT = 20
_RESULT_SCAN_CAP = 500          # mtime 최신순으로 이만큼만 읽는다 — 커밋마다 도는 경로다
_MAX_NESTING = 4                # bash -c · eval 재귀 한도

# 커밋보다 **앞에** 같은 명령에 있어도 되는 git 하위 명령 — 인덱스를 바꾸지 않는다.
#   ⛔ 이 목록 밖은 막는다. `git add -A && git commit` 에서 게이트가 보는 인덱스는 add **전**의
#     것이라, 리뷰 뒤에 스테이징된 변경이 해시 비교를 비껴 커밋된다.
_READ_ONLY_GIT = frozenset((
    "status", "diff", "log", "show", "rev-parse", "branch", "remote", "fetch", "ls-files",
    "describe", "blame", "grep", "shortlog", "reflog", "cat-file", "version", "help",
    "symbolic-ref", "for-each-ref", "show-ref", "ls-tree", "merge-base", "var",
    "check-ignore", "count-objects", "rev-list", "name-rev", "range-diff",
))

# `git commit` 옵션 — 값을 **다음 인자로** 받는 것. 이것을 모르면 메시지를 경로로 오인한다.
_COMMIT_SHORT_WITH_VALUE = "mFCct"
_COMMIT_SHORT_OPTIONAL = "uS"   # 값은 붙여서만 받는다(`-uno` · `-Skey`)
# ⛔ 인덱스가 아닌 내용을 커밋하는 옵션 — 리뷰한 스테이징과 커밋 내용이 달라진다.
_COMMIT_SHORT_UNSAFE = "aiop"
_COMMIT_LONG_WITH_VALUE = (
    "message", "file", "reuse-message", "reedit-message", "template", "author", "date",
    "cleanup", "fixup", "squash", "trailer")
_COMMIT_LONG_UNSAFE = ("all", "include", "only", "interactive", "patch", "pathspec-from-file")
_COMMIT_LONG_SAFE = (
    "amend", "no-edit", "edit", "quiet", "verbose", "signoff", "no-signoff", "no-verify",
    "verify", "allow-empty", "allow-empty-message", "no-post-rewrite", "status", "no-status",
    "reset-author", "short", "branch", "porcelain", "long", "null", "untracked-files",
    "gpg-sign", "no-gpg-sign", "dry-run", "pathspec-file-nul")

_GIT_GLOBAL_WITH_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "--namespace",
                          "--config-env", "--super-prefix")
# 저장소 위치를 명령이 스스로 바꾸는 방법 — 게이트가 어느 저장소의 커밋인지 확정할 수 없다.
_REPO_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# 감싸는 명령마다 **값을 받는** 옵션. 명령마다 다르다 — `timeout -k 5` 는 값을 받지만 `sudo -k` 는
#   받지 않는다. 한 표로 뭉치면 `sudo -k git commit` 의 `git` 을 옵션 값으로 삼켜 커밋을 놓친다.
_WRAPPER_VALUE_OPTS = {
    "env": ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"),
    "sudo": ("-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C",
             "--close-from", "-D", "--chdir", "-r", "--role", "-t", "--type", "-U",
             "--other-user", "-T", "--command-timeout"),
    "nice": ("-n", "--adjustment"),
    "timeout": ("-s", "--signal", "-k", "--kill-after"),
}
# ⛔ 실행 폴더를 바꾸는 옵션. 이것을 벗겨 버리면 게이트가 **엉뚱한 저장소**(cwd)를 본다 —
#   cwd 의 스테이징이 비어 있으면 검증 없이 통과시켰다(26.09.16 교차 리뷰 HIGH, 실측 재현).
#   값은 `git -C` 처럼 따라간다. 대상 저장소의 설정을 읽어야 하기 때문이다(3회차 CRITICAL).
_WRAPPER_CHDIR = {"env": ("-C", "--chdir"), "sudo": ("-D", "--chdir")}
_SHELLS = ("bash", "sh", "zsh", "dash", "ksh")
_PREFIX_WORDS = ("!", "{", "}", "(", "then", "do", "else", "elif", "if", "while", "until",
                 "time", "command", "builtin", "exec", "nohup")


# ---------------------------------------------------------------------------
# 셸 명령 해석
# ---------------------------------------------------------------------------

class _ParseError(ValueError):
    pass


def split_commands(text):
    """셸 명령 문자열을 **실행 순서대로** 단순 명령의 단어 목록으로 나눈다.

    ⛔ `shlex` 로는 안 된다. Claude 가 쓰는 커밋 메시지 꼴
      `git commit -m "$(cat <<'EOF' … EOF)"` 에서 heredoc 본문의 `"` 가 바깥 큰따옴표를
      닫은 것으로 읽혀 단어 경계가 무너진다. 실제 bash 는 `$( … )` 안을 새 문맥으로 읽는다.
    ⚠ 명령 치환(`$( … )` · 백틱)의 안쪽 명령은 바깥 명령보다 **먼저** 실행되므로 먼저 담는다.
    ⚠ 완전한 셸 해석기가 아니다. 해석할 수 없는 꼴은 `_ParseError` 로 알리고, 호출부가
      게이트가 켜진 저장소에서는 막는다.
    """
    out = []
    _scan(text, 0, out, None)
    return out


def _scan(text, i, out, stop):
    n = len(text)
    st = {"words": [], "word": None, "skip": False}
    heredocs = []

    def flush_word():
        if st["word"] is not None:
            if st["skip"]:
                st["skip"] = False       # 리다이렉션 대상은 명령 인자가 아니다
            else:
                st["words"].append(st["word"])
            st["word"] = None

    def flush_segment():
        flush_word()
        if st["words"]:
            out.append(st["words"])
        st["words"] = []
        st["skip"] = False

    def add(s):
        st["word"] = (st["word"] or "") + s

    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 < n:
                if text[i + 1] != "\n":   # 줄 이음은 아무것도 남기지 않는다
                    add(text[i + 1])
                i += 2
            else:
                add(c)
                i += 1
        elif c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                raise _ParseError("닫는 작은따옴표가 없다")
            add(text[i + 1:j])
            i = j + 1
        elif c == '"':
            s, i = _scan_dquote(text, i + 1, out)
            add(s)
        elif c == "$":
            s, i = _scan_dollar(text, i, out)
            add(s)
        elif c == "`":
            i = _scan_backtick(text, i, out)
            add("`...`")
        elif c in "<>":
            if text.startswith("<<<", i):
                flush_word()
                i += 3
                st["skip"] = True
            elif text.startswith("<<", i):
                flush_word()
                i += 2
                strip = i < n and text[i] == "-"
                if strip:
                    i += 1
                while i < n and text[i] in " \t":
                    i += 1
                delim, i = _read_delimiter(text, i)
                heredocs.append((delim, strip))
            elif i + 1 < n and text[i + 1] == "(":
                i = _scan(text, i + 2, out, ")")     # 프로세스 치환
                add("<(...)")
            else:
                if st["word"] is not None and st["word"].isdigit():
                    st["word"] = None                # `2>` 의 파일 서술자 번호
                else:
                    flush_word()
                i += 1
                if i < n and text[i] in ">&|":
                    i += 1
                st["skip"] = True
        elif c == "&":
            if text.startswith("&>", i):
                flush_word()
                i += 3 if text.startswith("&>>", i) else 2
                st["skip"] = True
            else:
                flush_segment()
                i += 2 if text.startswith("&&", i) else 1
        elif c == "|":
            flush_segment()
            i += 2 if text.startswith("||", i) or text.startswith("|&", i) else 1
        elif c == ";":
            flush_segment()
            i += 1
        elif c == "(":
            flush_segment()
            i += 1
        elif c == ")":
            flush_segment()
            if stop == ")":
                return i + 1
            i += 1
        elif c == "\n":
            flush_segment()
            i += 1
            for delim, strip in heredocs:
                i = _skip_heredoc(text, i, delim, strip)
            heredocs = []
        elif c == "#" and st["word"] is None:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in " \t\r":
            flush_word()
            i += 1
        else:
            add(c)
            i += 1
    if stop is not None:
        raise _ParseError("닫는 괄호가 없다")
    flush_segment()
    return n


def _scan_dquote(text, i, out):
    n = len(text)
    buf = []
    while i < n:
        c = text[i]
        if c == '"':
            return "".join(buf), i + 1
        if c == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in '"\\$`':
                buf.append(nxt)
                i += 2
                continue
            if nxt == "\n":
                i += 2
                continue
        if c == "$":
            s, i = _scan_dollar(text, i, out)
            buf.append(s)
            continue
        if c == "`":
            i = _scan_backtick(text, i, out)
            buf.append("`...`")
            continue
        buf.append(c)
        i += 1
    raise _ParseError("닫는 큰따옴표가 없다")


def _scan_dollar(text, i, out):
    """`$` 로 시작하는 확장. 반환 (단어에 붙일 문자열, 다음 위치)."""
    n = len(text)
    if text.startswith("$((", i):
        depth, j = 2, i + 3
        while j < n and depth:
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        if depth:
            raise _ParseError("산술 확장이 닫히지 않았다")
        return "$((...))", j
    if text.startswith("$(", i):
        return "$(...)", _scan(text, i + 2, out, ")")
    if text.startswith("${", i):
        j = text.find("}", i + 2)
        if j < 0:
            raise _ParseError("변수 확장이 닫히지 않았다")
        return text[i:j + 1], j + 1
    return "$", i + 1


def _scan_backtick(text, i, out):
    n = len(text)
    j = i + 1
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "`":
            inner = re.sub(r"\\([`\\$])", r"\1", text[i + 1:j])
            _scan(inner, 0, out, None)
            return j + 1
        j += 1
    raise _ParseError("닫는 백틱이 없다")


def _read_delimiter(text, i):
    n = len(text)
    buf = []
    while i < n and text[i] not in " \t\n;&|<>()":
        c = text[i]
        if c in "'\"":
            j = text.find(c, i + 1)
            if j < 0:
                raise _ParseError("heredoc 구분자의 따옴표가 닫히지 않았다")
            buf.append(text[i + 1:j])
            i = j + 1
        elif c == "\\" and i + 1 < n:
            buf.append(text[i + 1])
            i += 2
        else:
            buf.append(c)
            i += 1
    if not buf:
        raise _ParseError("heredoc 구분자가 없다")
    return "".join(buf), i


def _skip_heredoc(text, i, delim, strip):
    n = len(text)
    while i < n:
        j = text.find("\n", i)
        line = text[i:n if j < 0 else j]
        i = n if j < 0 else j + 1
        if (line.lstrip("\t") if strip else line) == delim:
            return i
    return n


# ---------------------------------------------------------------------------
# 단순 명령 → 게이트가 보는 단계
# ---------------------------------------------------------------------------

def analyze(command, depth=0):
    """명령을 게이트가 판단할 **단계 목록**으로 바꾼다(실행 순서).

    단계 종류: `cd`(경로 또는 None=알 수 없음) · `git`(하위 명령과 인자, 저장소 위치) ·
    `opaque`(git commit 을 **안에 품었지만** 형태를 확정할 수 없는 명령 — xargs · find -exec 등).
    """
    steps = []
    repo_env = False                     # 앞선 `export GIT_DIR=…`
    for words in split_commands(command):
        argv, assigns, chdirs, split = _unwrap(words)
        if split is not None or ((_basename(argv[0]) if argv else "") in _SHELLS + ("eval",)):
            inner = split + " " + " ".join(argv) if split is not None else _inner_script(
                _basename(argv[0]), argv)
            if inner is not None:
                if depth >= _MAX_NESTING:
                    raise _ParseError("셸 중첩이 너무 깊다")
                nested = analyze(inner, depth + 1)
                # ⛔ 바깥에서 저장소 위치를 바꾼 조건은 **안쪽 명령에 그대로 이어진다** — 폴더 이동,
                #   앞선 `export GIT_DIR`, 이 명령의 `GIT_DIR=` 대입. 처음엔 폴더 이동만 넘겨
                #   `GIT_DIR=/x bash -c "git commit"` 이 cwd 로 판정됐다(26.09.16 교차 리뷰 HIGH, 실측 재현).
                outer_unknown = bool(chdirs) or repo_env or any(
                    a.split("=", 1)[0] in _REPO_ENV for a in assigns)
                for step in nested:
                    if step["kind"] == "git" and outer_unknown:
                        step["repo_unknown"] = True
                steps.extend(nested)
                continue
        if not argv:
            if any(a.split("=", 1)[0] in _REPO_ENV for a in assigns):
                repo_env = True
            continue
        name = _basename(argv[0])
        if name == "export" and any(a.split("=", 1)[0] in _REPO_ENV for a in argv[1:]):
            repo_env = True
            continue
        if name in ("cd", "pushd"):
            steps.append({"kind": "cd", "path": _cd_target(argv)})
            continue
        if name in ("git", "git.exe"):
            step = _git_step(argv)
            step["dirs"] = chdirs + step["dirs"]   # `env -C a git -C b` 는 a 안의 b 다
            step["repo_unknown"] = step["repo_unknown"] or repo_env or any(
                a.split("=", 1)[0] in _REPO_ENV for a in assigns)
            steps.append(step)
            continue
        hidden = _hidden_git(name, argv)
        if hidden is not None:
            steps.append({"kind": "opaque", "name": name, "sub": hidden})
    return steps


def _unwrap(words):
    """앞쪽의 예약어 · 변수 대입 · 흔한 감싸는 명령을 벗긴다.

    반환 (argv, 대입 목록, 바꾼 실행 폴더 목록, `env -S` 문자열 또는 None).
    """
    words = list(words)
    assigns = []
    chdirs = []
    split = None
    while words:
        w = words[0]
        if w in _PREFIX_WORDS:
            words.pop(0)
        elif _ASSIGN.match(w):
            assigns.append(words.pop(0))
        elif w in _WRAPPER_VALUE_OPTS:
            words.pop(0)
            takes = _WRAPPER_VALUE_OPTS[w]
            while words and (words[0].startswith("-") or (w == "env" and _ASSIGN.match(words[0]))):
                opt = words.pop(0)
                if opt == "--":
                    break
                if _ASSIGN.match(opt):
                    assigns.append(opt)
                    continue
                name, eq, value = opt.partition("=")
                if name in takes:
                    if not eq:
                        value = words.pop(0) if words else ""
                elif not name.startswith("--") and name[:2] in takes and len(name) > 2:
                    name, value = name[:2], name[2:]      # `-Cdir` 꼴
                else:
                    continue                              # 값을 받지 않는 옵션
                if name in _WRAPPER_CHDIR.get(w, ()):
                    chdirs.append(value)
                elif w == "env" and name in ("-S", "--split-string"):
                    split = value
            if w == "timeout" and words:
                words.pop(0)             # 시간 인자
        else:
            break
    while words and words[-1] in ("}", ")"):
        words.pop()
    return words, assigns, chdirs, split


def _basename(word):
    return re.split(r"[\\/]", word)[-1]


def _inner_script(name, argv):
    if name == "eval":
        return " ".join(argv[1:])
    for k, a in enumerate(argv[1:], 1):
        if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
            return argv[k + 1] if k + 1 < len(argv) else ""
        if not a.startswith("-"):
            return None                  # 스크립트 파일 실행 — 안을 볼 수 없다
    return None


def _cd_target(argv):
    args = [a for a in argv[1:] if a not in ("-L", "-P", "-e", "--")]
    if not args:
        return "~"
    target = args[0]
    if target == "-" or "$" in target or "`" in target:
        return None
    return target


def _hidden_git(name, argv):
    """git 이 첫 단어가 아닌 명령(`xargs git add` · `find … -exec git commit`)의 git 하위 명령.

    반환: 하위 명령 이름 · 알 수 없으면 "?" · git 이 없으면 None.
    ⚠ 셸에 넘긴 문자열을 해석하지 못한 경우(`bash -o pipefail -c "…"`)는 문자열 안을 본다.
      다만 **셸이 아닌 명령의 인자 문자열은 보지 않는다** — `gh pr create --body "… git commit …"`
      같은 평범한 문구가 막히면 게이트가 매일 오탐을 낸다.
    """
    names = [_basename(w) for w in argv[1:]]
    for k, w in enumerate(names):
        if w in ("git", "git.exe"):
            rest = [x for x in names[k + 1:] if not x.startswith("-")]
            return rest[0] if rest else "?"
    if name in _SHELLS:
        joined = " ".join(argv[1:])
        if re.search(r"\bgit\b", joined):
            return "commit" if re.search(r"\bcommit\b", joined) else "?"
    return None


def _git_step(argv):
    dirs = []
    repo_unknown = False
    k = 1
    while k < len(argv):
        a = argv[k]
        if a in _GIT_GLOBAL_WITH_VALUE:
            value = argv[k + 1] if k + 1 < len(argv) else ""
            if a == "-C":
                dirs.append(value)
            elif a in ("--git-dir", "--work-tree"):
                repo_unknown = True
            k += 2
            continue
        if a.startswith(("--git-dir=", "--work-tree=")):
            repo_unknown = True
        if a.startswith("-"):
            k += 1
            continue
        return {"kind": "git", "sub": a, "args": argv[k + 1:], "dirs": dirs,
                "repo_unknown": repo_unknown}
    return {"kind": "git", "sub": None, "args": [], "dirs": dirs, "repo_unknown": repo_unknown}


def commit_form_problem(args):
    """`git commit` 인자가 **인덱스 그대로를 커밋하는지** 본다. 문제가 있으면 그 설명, 없으면 None.

    ⛔ `-a` · 경로 지정 · `--include` · `--only` · `--patch` 는 게이트가 해시를 잰 인덱스와
      다른 내용을 커밋한다. 해석을 넓히는 대신 막고, 스테이징 뒤 단독 커밋을 안내한다.
    """
    k = 0
    while k < len(args):
        a = args[k]
        if a == "--":
            return "경로 지정(`--` 뒤)" if k + 1 < len(args) else None
        if a.startswith("--"):
            name = a[2:].split("=", 1)[0]
            if name in _COMMIT_LONG_SAFE:
                k += 1
                continue
            if name in _COMMIT_LONG_WITH_VALUE:
                k += 1 if "=" in a else 2
                continue
            # git 은 긴 옵션의 **고유한 앞부분**도 받는다(`--inc` → `--include`).
            if any(u.startswith(name) for u in _COMMIT_LONG_UNSAFE):
                return "`%s`" % a
            prefixed = [v for v in _COMMIT_LONG_WITH_VALUE if v.startswith(name)]
            if len(prefixed) == 1 and "=" not in a:
                k += 2
                continue
            k += 1
            continue
        if a.startswith("-") and a != "-":
            cluster = a[1:]
            for pos, ch in enumerate(cluster):
                if ch in _COMMIT_SHORT_UNSAFE:
                    return "`-%s`" % ch
                if ch in _COMMIT_SHORT_WITH_VALUE:
                    if pos == len(cluster) - 1:
                        k += 1           # 값은 다음 인자다
                    break
                if ch in _COMMIT_SHORT_OPTIONAL:
                    break
            k += 1
            continue
        return "경로 지정(`%s`)" % a
    return None


def _is_dry_run(args):
    return any(a == "--dry-run" for a in args)


def config_write_problem(step):
    """게이트 설정을 **끄거나 지우는** git config 명령이면 설명, 아니면 None.

    ⛔ 게이트를 끄는 것은 사용자의 결정이다(`--allow-sensitive` 와 같은 규약). 켜는 것은 막지 않는다.
    """
    if step.get("sub") != "config":
        return None
    args = step["args"]
    lowered = [a.lower() for a in args]
    touches = [a for a in lowered if a == GATE_KEY or a == "gemini-review"
               or a.startswith(GATE_KEY + "=")]
    if not touches:
        return None
    reads = ("--get", "--get-all", "--get-regexp", "--list", "-l", "get", "list",
             "--show-origin", "--show-scope")
    # ⚠ `git config -e`(편집기로 여는 것)는 키를 적지 않으므로 여기 오지 않는다 — 설정 파일을 직접
    #   고치는 것과 같이 **위협 모델(망각) 밖**이다. 도달하지 않는 항목을 목록에 두지 않는다
    #   (26.09.16 교차 리뷰가 "차단 코드가 배선 단절" 로 짚었다).
    removes = ("--unset", "--unset-all", "--remove-section", "--rename-section", "unset",
               "remove-section", "rename-section")
    if any(a in removes for a in lowered):
        return "게이트 설정을 지우는 명령"
    if any(a in reads for a in lowered) and not any(a in ("set",) for a in lowered):
        return None
    try:
        at = lowered.index(GATE_KEY)
    except ValueError:
        return "게이트 설정을 바꾸는 명령"
    value = lowered[at + 1] if at + 1 < len(lowered) else None
    if value is None or value in ("true", "yes", "on", "1"):
        return None                      # 값이 없으면 읽기다
    return "게이트를 끄는 명령"


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------

def _git_rc(args, cwd):
    """반환 (종료 코드, 표준 출력). git 을 실행하지 못하면 (None, "")."""
    try:
        proc = subprocess.run(["git", "-c", "core.quotePath=false"] + args, cwd=cwd,
                              capture_output=True, timeout=_GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def gate_state(where):
    """`gemini-review.gate` 의 값. 반환 "on" · "off" · "invalid"(값이 불리언이 아니다)."""
    cwd = where if where and os.path.isdir(where) else os.path.expanduser("~")
    rc, out = _git_rc(["config", "--bool", "--get", GATE_KEY], cwd)
    if rc is None:
        return "off"                     # git 이 없으면 커밋도 없다
    if rc == 0:
        return "on" if out.strip() == "true" else "off"
    if rc == 1:
        return "off"                     # 설정이 없다
    return "invalid"


def unknown_target_state(cwd):
    """대상 저장소를 **확정할 수 없을 때**의 게이트 상태 — 현재 폴더와 전역 설정 가운데 엄한 쪽.

    ⛔ [26.09.16 교차 리뷰 CRITICAL · 실측 재현] 처음엔 cwd 설정만 봐서, 게이트가 꺼진 폴더에서
      `cd $REPO && git commit` 을 하면 전역으로 켠 게이트도 조용히 통과했다.
    ⚠ 모르는 대상을 무조건 막지는 않는다 — 게이트를 켠 적 없는 사용자의 명령까지 막게 된다.
      그래서 **저장소에만 켠 게이트**는 대상을 확정할 수 없는 명령을 막지 못한다(README 에 적는다).
    """
    here = gate_state(cwd)
    if here != "off":
        return here
    rc, out = _git_rc(["config", "--global", "--bool", "--get", GATE_KEY], os.path.expanduser("~"))
    if rc == 0:
        return "on" if out.strip() == "true" else "off"
    return "off" if rc in (None, 1) else "invalid"


def _resolve_dir(steps, upto, cwd):
    """`upto` 번째 단계가 실행될 폴더. 확정할 수 없으면 None."""
    here = cwd
    for step in steps[:upto]:
        if step["kind"] == "cd":
            if step["path"] is None:
                return None
            here = os.path.join(here, os.path.expanduser(step["path"]))
    step = steps[upto]
    if step.get("repo_unknown"):
        return None
    for d in step.get("dirs", []):
        if "$" in d or "`" in d:
            return None
        here = os.path.join(here, os.path.expanduser(d))
    return os.path.normpath(here)


def evaluate(command, cwd):
    """반환 (종료 코드, stderr 에 쓸 문구). 허용이면 문구는 빈 문자열이다."""
    lowered = command.lower()
    if "commit" not in lowered and "gemini-review" not in lowered:
        return EXIT_ALLOW, ""
    try:
        steps = analyze(command)
        parse_error = None
    except _ParseError as exc:
        steps, parse_error = [], str(exc)

    for step in steps:
        if step["kind"] == "git":
            problem = config_write_problem(step)
            if problem:
                return EXIT_BLOCK, _message(
                    "%s이다. 게이트를 끄거나 지우는 것은 사용자가 직접 한다." % problem,
                    "이 명령을 실행하지 않는다. 게이트가 커밋을 막고 있다면 리뷰를 통과시킨다.")

    commits = [k for k, s in enumerate(steps) if s["kind"] == "git" and s["sub"] == "commit"]
    opaque = [s for s in steps if s["kind"] == "opaque" and s["sub"] == "commit"]
    suspicious = parse_error is not None and "git" in lowered and "commit" in lowered
    if not commits and not opaque and not suspicious:
        return EXIT_ALLOW, ""

    where = _resolve_dir(steps, commits[0], cwd) if commits else None
    state = gate_state(where) if where is not None else unknown_target_state(cwd)
    if state == "off":
        return EXIT_ALLOW, ""
    if state == "invalid":
        return EXIT_BLOCK, _message(
            "`%s` 값이 불리언이 아니다." % GATE_KEY,
            "사용자에게 설정 값을 확인해 달라고 알린다.")

    alone = "`git commit` 을 **단독 명령**으로(앞에 `git add` 등을 잇지 않고) 다시 실행한다."
    if suspicious:
        return EXIT_BLOCK, _message("명령을 해석하지 못했다(%s)." % parse_error, alone)
    if opaque:
        return EXIT_BLOCK, _message(
            "`%s` 안의 git commit 은 형태를 확인할 수 없다." % opaque[0]["name"], alone)
    if len(commits) > 1:
        return EXIT_BLOCK, _message("한 명령에 git commit 이 %d번 있다." % len(commits), alone)
    at = commits[0]
    if where is None:
        return EXIT_BLOCK, _message(
            "어느 저장소의 커밋인지 확정할 수 없다(`cd -` · 변수 경로 · GIT_DIR 등).",
            "저장소 폴더에서 " + alone)
    for step in steps[:at]:
        if step["kind"] in ("git", "opaque") and step["sub"] not in _READ_ONLY_GIT:
            return EXIT_BLOCK, _message(
                "커밋 앞에 인덱스를 바꿀 수 있는 `git %s` 가 같은 명령에 있다 — 게이트는 그 전의 "
                "스테이징만 볼 수 있다." % step["sub"],
                "스테이징을 먼저 끝내고, 리뷰를 통과시킨 뒤 " + alone)
    commit = steps[at]
    form = commit_form_problem(commit["args"])
    if form:
        return EXIT_BLOCK, _message(
            "%s 은 스테이징과 다른 내용을 커밋한다." % form,
            "`git add` 로 스테이징한 뒤 리뷰를 통과시키고, 옵션 없이 " + alone)
    if _is_dry_run(commit["args"]):
        return EXIT_ALLOW, ""
    return _check_review(where)


def _check_review(where):
    rc, top = _git_rc(["rev-parse", "--show-toplevel"], where)
    if rc != 0 or not top.strip():
        return EXIT_ALLOW, ""            # 저장소가 아니면 git commit 자신이 실패한다
    root = top.strip()
    gr = _load_review()
    # ⛔ 리뷰와 **같은 함수**로 diff 를 모으고 같은 바이트로 해시한다. `git diff --cached |
    #   sha256sum` 은 값이 다르다(`_diff_bytes` docstring).
    diff, _files = gr._collect_diff(root, None, None, True)
    if not diff.strip():
        return EXIT_ALLOW, ""            # 스테이징이 비었다 — 메시지만 고치는 --amend 등
    sha = gr._sha256_hex(gr._diff_bytes(diff))
    base, trust = gr._state_dir(create=False)
    if base is None or trust != "ok":
        return EXIT_BLOCK, _message(
            "결과 폴더를 믿을 수 없다(링크 · 남의 소유 · 공유 임시 폴더) — 남이 심은 통과 기록을 "
            "받아들이지 않는다.",
            "사용자에게 결과 폴더 권한을 확인해 달라고 알린다.")
    match, latest_here = _find_result(base, sha, root)
    short = sha[:12]
    fix = ("/gemini-review 를 --staged 로 돌려 exit 0 을 받은 뒤, 스테이징을 바꾸지 않고 "
           "`git commit` 을 단독 명령으로 다시 실행한다.")
    if match is None:
        if latest_here is not None:
            return EXIT_BLOCK, _message(
                "스테이징된 변경(sha256 %s)에 대한 리뷰가 없다. 이 저장소의 마지막 리뷰 뒤에 "
                "스테이징이 바뀌었다." % short, fix)
        return EXIT_BLOCK, _message(
            "스테이징된 변경(sha256 %s)에 대한 리뷰가 없다." % short, fix)
    mode, code = match.get("mode"), match.get("exit_code")
    if match.get("passed") is True and mode == "reviewed" and code == 0:
        return EXIT_ALLOW, ""
    if mode == "reviewed":
        why = "같은 변경의 최근 리뷰가 통과가 아니다(exit %s · 지적 있음)." % code
        fix = "지적을 실측으로 검증해 반영하고 다시 리뷰한다. " + fix
    elif mode == "sensitive_blocked":
        why = "같은 변경의 최근 리뷰가 민감 경로 때문에 전송되지 않았다(exit 3)."
        fix = "차단 목록을 사용자에게 보여 주고 판단을 받는다."
    elif code == 4:
        why = "같은 변경의 최근 리뷰가 수행되지 않았다(exit 4 · %s)." % mode
        fix = "화면의 원인 · 해결 줄대로 같은 모델로 재시도한다. " + fix
    else:
        why = "같은 변경의 최근 리뷰가 통과가 아니다(%s · exit %s)." % (mode, code)
    return EXIT_BLOCK, _message(why, fix)


def _find_result(base, sha, root):
    """반환 (해시가 같은 **가장 최근** 결과의 `_meta`, 이 저장소의 가장 최근 `_meta`).

    ⚠ 같은 해시의 결과가 여럿이면 **가장 최근 것**이 판정한다. 통과 뒤 같은 내용을 다시 돌려
      request_changes 가 나왔다면, 옛 통과로 커밋을 열어 주지 않는다.
    ⛔ **같은 저장소의 결과만** 본다. 결과 폴더는 모든 저장소가 함께 쓰고, 같은 변경은 저장소가
      달라도 해시가 같다(실측: 두 저장소에 같은 파일을 더하면 sha256 이 일치). 저장소마다 리뷰
      관점(`.gemini-review.md`)이 다르므로 남의 판정으로 열거나 닫지 않는다(26.09.16 교차 리뷰 CRITICAL).
    """
    try:
        names = [n for n in os.listdir(base)
                 if n.startswith("gemini_review_") and n.endswith(".json")]
    except OSError:
        return None, None
    paths = []
    for n in names:
        p = os.path.join(base, n)
        try:
            paths.append((os.path.getmtime(p), p))
        except OSError:
            continue
    paths.sort(reverse=True)
    latest_here = None
    real_root = os.path.realpath(root)
    for _mtime, p in paths[:_RESULT_SCAN_CAP]:
        try:
            with io.open(p, encoding="utf-8") as fh:
                meta = json.load(fh).get("_meta")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(meta, dict):
            continue
        scope = meta.get("scope") if isinstance(meta.get("scope"), dict) else {}
        if not (isinstance(scope.get("repo"), str)
                and os.path.realpath(scope["repo"]) == real_root):
            continue
        if meta.get("diff_sha256") == sha:
            return meta, latest_here
        if latest_here is None:
            latest_here = meta
    return None, latest_here


def _message(cause, fix):
    """막을 때 모델에게 보내는 문구.

    ⛔ **게이트를 끄는 방법을 적지 않는다.** 막힌 모델에게 우회 명령을 알려 주면 그것이 곧
      다음 행동이 된다(`--allow-sensitive` 안내를 뺀 것과 같은 이유).
    """
    return ("⛔ 커밋 게이트(gemini-review)가 이 명령을 막았다.\n"
            "   원인: %s\n"
            "   해결: %s\n" % (cause, fix))


_REVIEW_MODULE = []


def _load_review():
    if not _REVIEW_MODULE:
        spec = importlib.util.spec_from_file_location("gemini_review", _REVIEW_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _REVIEW_MODULE.append(mod)
    return _REVIEW_MODULE[0]


def run(raw):
    """hook 입력(JSON 문자열) → (종료 코드, 문구). **예외를 내지 않는다.**

    ⛔ 게이트 자신의 결함은 **게이트가 켜진 곳에서 막는다**(fail-closed). hook 이 예외로 exit 1 을
      내면 Claude Code 는 막지 않고 넘긴다 — 결함이 곧 조용한 통과가 된다.
    """
    cwd = os.getcwd()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
            return EXIT_ALLOW, ""
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str):
            return EXIT_ALLOW, ""
        if isinstance(payload.get("cwd"), str) and payload["cwd"]:
            cwd = payload["cwd"]
        return evaluate(command, cwd)
    except Exception as exc:
        lowered = raw.lower()
        if "commit" not in lowered and "gemini-review" not in lowered:
            return EXIT_ALLOW, ""
        try:
            state = gate_state(cwd)
        except Exception:
            state = "invalid"
        if state == "off":
            return EXIT_ALLOW, ""
        return EXIT_BLOCK, _message(
            "게이트 내부 오류로 명령을 확인하지 못했다(%s: %s)."
            % (exc.__class__.__name__, str(exc)[:200]),
            "사용자에게 이 문구를 그대로 보고한다(게이트 결함이다).")


def main():
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    rc, message = run(raw)
    if rc == EXIT_BLOCK and message:
        sys.stderr.buffer.write(message.encode("utf-8"))
        sys.stderr.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
