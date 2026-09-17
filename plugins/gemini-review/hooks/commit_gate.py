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
  `-am`, `git commit -- 경로`, heredoc 메시지)은 해석하고, 해석하지 못한 꼴은 **막는다**(fail-closed).
⚠ [1.7.0] 커밋 형태를 막지 않고 **커밋이 담을 내용을 모사**해 해시를 잰다(`gemini_review._commit_diff`).
  1.6.x 는 `add && commit` · 경로 지정 커밋을 형태만 보고 막아, 두 세션이 작업 트리를 공유할 때 쓰는
  경로 지정 커밋이 아예 불가능했다(다른 프로젝트 운영 보고).
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
#   `git add` 는 따로 다룬다 — 임시 인덱스에서 **모사**해 커밋 내용에 반영한다(1.7.0).
#   ⛔ 이 목록 밖(reset · rm · stash · checkout …)은 모사하지 않으므로 막는다. 게이트가 보는 인덱스는
#     그 명령 **전**의 것이라, 리뷰 뒤에 바뀐 내용이 해시 비교를 비껴 커밋된다.
_READ_ONLY_GIT = frozenset((
    "status", "diff", "log", "show", "rev-parse", "branch", "remote", "fetch", "ls-files",
    "describe", "blame", "grep", "shortlog", "reflog", "cat-file", "version", "help",
    "symbolic-ref", "for-each-ref", "show-ref", "ls-tree", "merge-base", "var",
    "check-ignore", "count-objects", "rev-list", "name-rev", "range-diff",
))

# `git commit` 옵션 — 값을 **다음 인자로** 받는 것. 이것을 모르면 메시지를 경로로 오인한다.
_COMMIT_SHORT_WITH_VALUE = "mFCct"
_COMMIT_SHORT_OPTIONAL = "uS"   # 값은 붙여서만 받는다(`-uno` · `-Skey`)
# 커밋이 담을 내용을 바꾸는 옵션 — 막지 않고 모사한다(1.7.0). 짧은 이름 → 긴 이름.
_COMMIT_CONTENT_FLAGS = {"a": "all", "i": "include", "o": "only"}
# ⛔ 게이트가 모사할 수 없는 옵션 — 대화형 선택 · 파일로 넘긴 경로.
_COMMIT_SHORT_UNSUPPORTED = "p"
_COMMIT_LONG_UNSUPPORTED = ("interactive", "patch", "pathspec-from-file")
_COMMIT_LONG_WITH_VALUE = (
    "message", "file", "reuse-message", "reedit-message", "template", "author", "date",
    "cleanup", "fixup", "squash", "trailer")
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


def commit_plan(args):
    """`git commit` 인자 → 커밋이 담을 내용의 **모사 계획**. 반환 (계획, 문제).

    계획은 `{"mode": index · all · include · only, "pathspec": [...]}` 이고, 문제가 있으면 계획은 None 이다.
    ⚠ git 이 받는 규칙을 따른다: 경로가 있으면 기본이 `--only`, `-i` 는 경로가 있어야 하고, `-a` 는 경로와
      함께 쓸 수 없다. git 이 거부하는 조합은 막는다 — 모사할 대상이 없다.
    ⛔ 모르는 긴 옵션도 막는다. 값을 받는 옵션인지 모르면 메시지를 경로로 읽거나 경로를 값으로 삼킨다.
    """
    flags = set()
    pathspec = []
    long_names = (_COMMIT_LONG_SAFE + _COMMIT_LONG_WITH_VALUE + _COMMIT_LONG_UNSUPPORTED
                  + tuple(_COMMIT_CONTENT_FLAGS.values()))
    k = 0
    while k < len(args):
        a = args[k]
        if a == "--":
            pathspec.extend(args[k + 1:])
            break
        if a.startswith("--"):
            name, eq, _value = a[2:].partition("=")
            if name not in long_names:
                # git 은 긴 옵션의 **고유한 앞부분**도 받는다(`--inc` → `--include`).
                found = [full for full in long_names if full.startswith(name)]
                if len(found) != 1:
                    return None, "게이트가 모르는 옵션 `%s`" % a
                name = found[0]
            if name in _COMMIT_LONG_UNSUPPORTED:
                return None, "`%s` 는 게이트가 모사할 수 없다" % a
            if name in _COMMIT_LONG_WITH_VALUE and not eq:
                k += 1                    # 값은 다음 인자다
            for short, full in _COMMIT_CONTENT_FLAGS.items():
                if name == full:
                    flags.add(short)
            k += 1
            continue
        if a.startswith("-") and a != "-":
            cluster = a[1:]
            for pos, ch in enumerate(cluster):
                if ch in _COMMIT_SHORT_UNSUPPORTED:
                    return None, "`-%s` 는 게이트가 모사할 수 없다" % ch
                if ch in _COMMIT_CONTENT_FLAGS:
                    flags.add(ch)
                    continue
                if ch in _COMMIT_SHORT_WITH_VALUE:
                    if pos == len(cluster) - 1:
                        k += 1           # 값은 다음 인자다
                    break
                if ch in _COMMIT_SHORT_OPTIONAL:
                    break
            k += 1
            continue
        pathspec.append(a)
        k += 1
    if len(flags & {"a", "i", "o"}) > 1:
        return None, "`-a` · `-i` · `-o` 를 함께 썼다(git 이 거부한다)"
    if "a" in flags:
        if pathspec:
            return None, "`-a` 와 경로를 함께 썼다(git 이 거부한다)"
        return {"mode": "all", "pathspec": []}, None
    if "i" in flags:
        if not pathspec:
            return None, "`-i` 에 경로가 없다(git 이 거부한다)"
        return {"mode": "include", "pathspec": pathspec}, None
    if pathspec:
        return {"mode": "only", "pathspec": pathspec}, None
    return {"mode": "index", "pathspec": []}, None


def add_problem(args):
    """커밋 앞의 `git add` 를 게이트가 모사할 수 없으면 설명, 아니면 None."""
    for a in args:
        if a == "--":
            break
        if a.startswith("--"):
            name = a[2:].split("=", 1)[0]
            if any(full.startswith(name) for full in ("patch", "interactive", "edit",
                                                     "pathspec-from-file")) and name:
                return "`git add %s` 는 게이트가 모사할 수 없다" % a
        elif a.startswith("-") and a != "-" and set(a[1:]) & set("pie"):
            return "`git add %s` 는 게이트가 모사할 수 없다(대화형)" % a
    return None


def _is_dry_run(args):
    return any(a == "--dry-run" for a in args)


# `git config` 에서 값을 **다음 인자로** 받는 옵션 — 그 값을 설정 키로 읽지 않는다.
_CONFIG_VALUE_OPTS = ("-f", "--file", "--blob", "--type", "--default", "--comment", "--value", "--url")
# 새 문법(`git config set 키 값`)의 하위 명령.
_CONFIG_SUBCOMMANDS = ("get", "set", "unset", "list", "edit", "rename-section", "remove-section")


def config_write_problem(step):
    """게이트 설정(`gemini-review.*`)을 **바꾸거나 지우는** git config 명령이면 설명, 아니면 None.

    ⛔ 게이트 설정은 사용자의 결정이다(`--allow-sensitive` 와 같은 규약). 막지 않는 쓰기는 게이트를
      **켜는** 것 하나다. [1.7.0] 대체 리뷰 인정(`gateFallback`) 같은 다른 키도 보호한다 — 게이트를
      느슨하게 하는 쪽이든 조이는 쪽이든 운영자의 선택이다.
    ⚠ 키는 **옵션 값을 건너뛴 첫 위치 인자**다. 인자 아무 데서나 `gemini-review.` 를 찾으면
      `git config --file gemini-review.conf user.name foo` 처럼 무관한 명령을 막는다(26.09.17 교차 리뷰
      MEDIUM, 실측 재현).
    ⚠ `git config -e`(편집기로 여는 것)는 키를 적지 않으므로 여기서 막지 않는다 — 설정 파일을 직접
      고치는 것과 같이 **위협 모델(망각) 밖**이다.
    """
    if step.get("sub") != "config":
        return None
    lowered = [a.lower() for a in step["args"]]
    flags, positional = [], []
    k = 0
    while k < len(lowered):
        a = lowered[k]
        if a == "--":
            positional.extend(lowered[k + 1:])
            break
        if a.startswith("-") and a != "-":
            name = a.split("=", 1)[0]
            flags.append(name)
            if name in _CONFIG_VALUE_OPTS and "=" not in a:
                k += 1                   # 옵션 값은 키가 아니다
            k += 1
            continue
        positional.append(a)
        k += 1
    if positional and positional[0] in _CONFIG_SUBCOMMANDS:
        flags.append(positional.pop(0))
    if not positional:
        return None
    key = positional[0]
    if not (key == "gemini-review" or key.startswith("gemini-review.")):
        return None
    removes = ("--unset", "--unset-all", "--remove-section", "--rename-section", "unset",
               "remove-section", "rename-section")
    if any(f in removes for f in flags):
        return "게이트 설정을 지우는 명령"
    reads = ("--get", "--get-all", "--get-regexp", "--list", "-l", "get", "list")
    if any(f in reads for f in flags):
        return None
    value = positional[1] if len(positional) > 1 else None
    if value is None:
        return None                      # 값이 없으면 읽기다
    if key == GATE_KEY and value in ("true", "yes", "on", "1"):
        return None
    return "게이트 설정을 바꾸는 명령"


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


def fallback_state(where):
    """대체 리뷰 기록(`fallback_reviewed`)을 게이트가 인정하는가. 반환 "on" · "off".

    기본은 인정한다 — 대체 리뷰는 같은 내용의 Gemini 시도가 **수행되지 않았을 때만** 기록되기
    때문이다(`gemini_review._record_fallback`). 운영자가 `gemini-review.gateFallback false` 로 끈다.
    ⛔ 값을 읽지 못하거나 불리언이 아니면 **인정하지 않는다**(fail-closed).
    """
    cwd = where if where and os.path.isdir(where) else os.path.expanduser("~")
    rc, out = _git_rc(["config", "--bool", "--get", "gemini-review.gatefallback"], cwd)
    if rc == 1:
        return "on"
    return "on" if rc == 0 and out.strip() == "true" else "off"


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
                    "%s이다. 게이트 설정을 바꾸거나 지우는 것은 사용자가 직접 한다." % problem,
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

    alone = "`git commit` 을 **단독 명령**으로(앞에 다른 git 명령을 잇지 않고) 다시 실행한다."
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
    adds = []
    for k, step in enumerate(steps[:at]):
        if step["kind"] == "git" and step["sub"] == "add":
            problem = add_problem(step["args"])
            add_dir = _resolve_dir(steps, k, cwd)
            if problem or add_dir is None:
                return EXIT_BLOCK, _message(
                    (problem or "앞의 `git add` 가 어느 폴더에서 도는지 확정할 수 없다") + ".",
                    "스테이징을 먼저 끝내고, 리뷰를 통과시킨 뒤 " + alone)
            adds.append((add_dir, list(step["args"])))
        elif step["kind"] in ("git", "opaque") and step["sub"] not in _READ_ONLY_GIT:
            return EXIT_BLOCK, _message(
                "커밋 앞에 인덱스를 바꾸는 `git %s` 가 같은 명령에 있다 — 게이트는 이 명령을 "
                "모사하지 않는다." % step["sub"],
                "스테이징을 먼저 끝내고, 리뷰를 통과시킨 뒤 " + alone)
    commit = steps[at]
    plan, problem = commit_plan(commit["args"])
    if problem:
        return EXIT_BLOCK, _message(problem + ".", "옵션을 고쳐 " + alone)
    if _is_dry_run(commit["args"]):
        return EXIT_ALLOW, ""
    return _check_review(where, plan, adds)


def _review_hint(plan, adds):
    """막을 때 `해결:` 줄 — 커밋 형태마다 **같은 내용을 리뷰하는 방법**이 다르다."""
    if plan["mode"] == "only":
        return ("/gemini-review 를 `--paths %s` 로 돌려 exit 0 을 받은 뒤, 작업 트리를 바꾸지 않고 "
                "같은 명령을 다시 실행한다." % " ".join(plan["pathspec"]))
    first = ""
    if adds or plan["mode"] in ("all", "include"):
        first = ("커밋이 담을 내용을 먼저 스테이징하고(앞의 `git add` 를 따로 실행하거나 `-a` · `-i` "
                 "대신 `git add`), ")
    return (first + "/gemini-review 를 --staged 로 돌려 exit 0 을 받은 뒤, 스테이징을 바꾸지 않고 "
            "같은 명령을 다시 실행한다.")


def _check_review(where, plan, adds):
    rc, top = _git_rc(["rev-parse", "--show-toplevel"], where)
    if rc != 0 or not top.strip():
        return EXIT_ALLOW, ""            # 저장소가 아니면 git commit 자신이 실패한다
    root = top.strip()
    real_root = os.path.realpath(root)
    for add_dir, _args in adds:
        real = os.path.realpath(add_dir)
        if real != real_root and not real.startswith(real_root.rstrip(os.sep) + os.sep):
            return EXIT_BLOCK, _message(
                "앞의 `git add` 가 커밋과 다른 저장소에서 돈다.",
                "스테이징을 먼저 끝내고 `git commit` 을 단독 명령으로 다시 실행한다.")
    gr = _load_review()
    # ⛔ 리뷰와 **같은 함수**로 커밋이 담을 diff 를 모으고 같은 바이트로 해시한다. `git diff --cached |
    #   sha256sum` 은 값이 다르다(`_diff_bytes` docstring). 스테이징 그대로이고 앞선 add 가 없으면
    #   `_commit_diff` 가 `--staged` 와 같은 `_collect_diff` 를 부른다.
    diff, _files = gr._commit_diff(root, plan["mode"], plan["pathspec"], where, adds)
    if not diff.strip():
        return EXIT_ALLOW, ""            # 담을 변경이 없다 — 메시지만 고치는 --amend 등
    sha = gr._sha256_hex(gr._diff_bytes(diff))
    base, trust = gr._state_dir(create=False)
    if base is None or trust != "ok":
        return EXIT_BLOCK, _message(
            "결과 폴더를 믿을 수 없다(링크 · 남의 소유 · 공유 임시 폴더) — 남이 심은 통과 기록을 "
            "받아들이지 않는다.",
            "사용자에게 결과 폴더 권한을 확인해 달라고 알린다.")
    match, latest_here = _find_result(base, sha, root)
    short = sha[:12]
    fix = _review_hint(plan, adds)
    what = "경로 지정 커밋이 담을 변경" if plan["mode"] == "only" else "커밋이 담을 변경"
    if match is None:
        if latest_here is not None:
            return EXIT_BLOCK, _message(
                "%s(sha256 %s)에 대한 리뷰가 없다. 이 저장소의 마지막 리뷰 뒤에 "
                "내용이 바뀌었다." % (what, short), fix)
        return EXIT_BLOCK, _message(
            "%s(sha256 %s)에 대한 리뷰가 없다." % (what, short), fix)
    mode, code = match.get("mode"), match.get("exit_code")
    if match.get("passed") is True and mode == "reviewed" and code == 0:
        return EXIT_ALLOW, ""
    if mode == "fallback_reviewed" and code == 0:
        if fallback_state(where) == "on":
            return EXIT_ALLOW, ""
        return EXIT_BLOCK, _message(
            "같은 내용의 대체 리뷰 기록이 있지만 이 저장소는 대체 리뷰를 인정하지 않는다(운영자 설정).",
            "Gemini 리뷰가 수행될 때까지 기다리거나, 커밋하지 못한 사실을 사용자에게 보고한다.")
    if mode == "reviewed":
        why = "같은 변경의 최근 리뷰가 통과가 아니다(exit %s · 지적 있음)." % code
        fix = "지적을 실측으로 검증해 반영하고 다시 리뷰한다. " + fix
    elif mode == "sensitive_blocked":
        why = "같은 변경의 최근 리뷰가 민감 경로 때문에 전송되지 않았다(exit 3)."
        fix = "차단 목록을 사용자에게 보여 주고 판단을 받는다."
    elif code == 4:
        why = "같은 변경의 최근 리뷰가 수행되지 않았다(exit 4 · %s)." % mode
        fix = "화면의 원인 · 해결 줄대로 같은 모델로 재시도한다. " + fix
        if fallback_state(where) == "on":
            # [1.7.0] 수행되지 않은 날에도 커밋할 길 — 운영 규칙이 허용한 대체 리뷰를 **실제로** 마친 뒤에만.
            fix += (" 재시도해도 수행되지 않고 운영 규칙이 대체 리뷰를 허용하면, 대체 리뷰를 실제로 "
                    "마치고 지적을 반영한 뒤 같은 범위 인자에 `--record-fallback --reviewer 이름 "
                    "--summary 요약` 을 붙여 기록한다.")
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
