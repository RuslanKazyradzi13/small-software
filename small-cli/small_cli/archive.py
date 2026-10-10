"""Сбор файлов проекта с учётом .gitignore/.railwayignore и упаковка в .tar.gz для Railway."""

import io
import os
import re
import tarfile

# Никогда не загружаются, даже без .gitignore.
ALWAYS_IGNORED = {".git", "node_modules", "__pycache__", ".venv", "venv"}
IGNORE_FILES = (".gitignore", ".railwayignore")


class IgnoreRules:
    """Подмножество синтаксиса .gitignore: *, **, ?, [...], !отрицание, /якорь, хвостовой / для папок.

    Читаются только файлы в корне проекта; вложенные .gitignore не учитываются.
    """

    def __init__(self, lines):
        self.rules = []  # (regex, negate, dir_only)
        for raw in lines:
            line = raw.rstrip("\r\n").rstrip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            if line.startswith("\\"):
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            anchored = "/" in line
            line = line.lstrip("/")
            if not line:
                continue
            pattern = _translate(line)
            if not anchored:
                pattern = "(?:.*/)?" + pattern
            try:
                self.rules.append((re.compile(pattern), negate, dir_only))
            except re.error:
                continue

    @classmethod
    def load(cls, root):
        lines = []
        for name in IGNORE_FILES:
            path = os.path.join(root, name)
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines.extend(f)
        return cls(lines)

    def ignored(self, rel_path, is_dir):
        result = False
        for regex, negate, dir_only in self.rules:
            if dir_only and not is_dir:
                continue
            if regex.fullmatch(rel_path):
                result = not negate
        return result


def _translate(pattern):
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i + 2:]:
            end = pattern.index("]", i + 2)
            body = pattern[i + 1:end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body + "]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def collect_files(root, skip_root_files=()):
    """Относительные пути (через /) всех файлов, которые уйдут в Railway.

    skip_root_files — имена файлов, которые не загружаются, только если лежат в корне (служебные файлы CLI).
    """
    rules = IgnoreRules.load(root)
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in ALWAYS_IGNORED and not rules.ignored(prefix + d, is_dir=True)
        )
        for name in sorted(filenames):
            rel = prefix + name
            if name in ALWAYS_IGNORED or (not prefix and name in skip_root_files):
                continue
            if not rules.ignored(rel, is_dir=False):
                files.append(rel)
    return files


def make_tarball(root, files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", dereference=True) as tar:
        for rel in files:
            tar.add(os.path.join(root, *rel.split("/")), arcname="./" + rel,
                    recursive=False, filter=_anonymize)
    return buf.getvalue()


def _anonymize(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info
