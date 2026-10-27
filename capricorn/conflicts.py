"""Разбор файла с маркерами конфликта git на блоки и сборка результата по выбранным сторонам.

Поддерживается и обычный стиль, и diff3 (с секцией «||||||| база»). Строки хранятся с переводами строк как есть,
поэтому CRLF-файлы остаются CRLF.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

OURS, THEIRS, BOTH, AGENT = "ours", "theirs", "both", "agent"
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


class ConflictParseError(ValueError):
    pass


def _marker(line: str, ch: str) -> str | None:
    """Метка маркера («HEAD» у «<<<<<<< HEAD») или None, если строка — не маркер."""
    body = line.rstrip("\r\n")
    if body == ch * 7:
        return ""
    if body.startswith(ch * 7 + " "):
        return body[8:]
    return None


@dataclass
class Hunk:
    ours: list[str]
    theirs: list[str]
    base: list[str] | None = None
    ours_label: str = ""
    theirs_label: str = ""
    base_label: str = ""
    choice: str | None = None
    proposal: list[str] | None = None        # вариант агента; становится результатом при choice == AGENT

    def result(self) -> list[str]:
        if self.choice == OURS:
            return self.ours
        if self.choice == THEIRS:
            return self.theirs
        if self.choice == BOTH:
            return self.ours + self.theirs
        if self.choice == AGENT and self.proposal is not None:
            return self.proposal
        return self.markers()

    def markers(self) -> list[str]:
        eol = _eol(self.ours + self.theirs)
        out = [f"<<<<<<< {self.ours_label}".rstrip() + eol, *self.ours]
        if self.base is not None:
            out += [f"||||||| {self.base_label}".rstrip() + eol, *self.base]
        return out + ["=======" + eol, *self.theirs, f">>>>>>> {self.theirs_label}".rstrip() + eol]

    @property
    def resolved(self) -> bool:
        return self.choice is not None and (self.choice != AGENT or self.proposal is not None)


@dataclass
class ConflictFile:
    segments: list[list[str] | Hunk] = field(default_factory=list)

    @property
    def hunks(self) -> list[Hunk]:
        return [s for s in self.segments if isinstance(s, Hunk)]

    @property
    def unresolved(self) -> int:
        return sum(not h.resolved for h in self.hunks)

    def render(self) -> str:
        return "".join("".join(s.result() if isinstance(s, Hunk) else s) for s in self.segments)

    def context(self, hunk: Hunk, lines: int = 3) -> tuple[list[str], list[str]]:
        """Несколько строк до и после блока — чтобы было видно, где он в файле."""
        i = self.segments.index(hunk)
        before = self.segments[i - 1] if i > 0 and not isinstance(self.segments[i - 1], Hunk) else []
        after = (self.segments[i + 1] if i + 1 < len(self.segments)
                 and not isinstance(self.segments[i + 1], Hunk) else [])
        return list(before[-lines:]), list(after[:lines])


def _eol(lines: list[str]) -> str:
    return "\r\n" if any(l.endswith("\r\n") for l in lines) else "\n"


def parse(text: str) -> ConflictFile:
    cf = ConflictFile()
    plain: list[str] = []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        label = _marker(lines[i], "<")
        if label is None:
            plain.append(lines[i])
            i += 1
            continue
        if plain:
            cf.segments.append(plain)
            plain = []
        hunk = Hunk([], [], ours_label=label)
        part = hunk.ours
        i += 1
        while True:
            if i >= len(lines):
                raise ConflictParseError("конфликтный блок не закрыт маркером >>>>>>>")
            line = lines[i]
            if (b := _marker(line, "|")) is not None and part is hunk.ours:
                hunk.base, hunk.base_label = [], b
                part = hunk.base
            elif _marker(line, "=") == "" and part is not hunk.theirs:
                part = hunk.theirs
            elif (t := _marker(line, ">")) is not None and part is hunk.theirs:
                hunk.theirs_label = t
                i += 1
                break
            elif _marker(line, "<") is not None:
                raise ConflictParseError("маркер <<<<<<< внутри незакрытого блока")
            else:
                part.append(line)
            i += 1
        cf.segments.append(hunk)
    if plain:
        cf.segments.append(plain)
    return cf


def proposal_lines(answer: str, like: list[str]) -> list[str]:
    """Ответ агента → строки блока. Берём последний блок кода, если он есть; перевод строки — как в файле."""
    fences = FENCE_RE.findall(answer)
    body = fences[-1] if fences else answer.strip("\n") + "\n"
    eol = _eol(like)
    return [l.rstrip("\r\n") + eol for l in body.splitlines()]


def agent_prompt(path: str, cf: ConflictFile, hunk: Hunk, context: int = 20) -> str:
    before, after = cf.context(hunk, context)
    base = (f"\nОбщий предок (база):\n```\n{''.join(hunk.base)}```\n" if hunk.base is not None else "")
    return (f"Разреши конфликт слияния git в файле {path}.\n\n"
            f"Контекст до блока:\n```\n{''.join(before)}```\n\n"
            f"Наша версия ({hunk.ours_label or 'ours'}):\n```\n{''.join(hunk.ours)}```\n"
            f"\nИх версия ({hunk.theirs_label or 'theirs'}):\n```\n{''.join(hunk.theirs)}```\n{base}\n"
            f"Контекст после блока:\n```\n{''.join(after)}```\n\n"
            "Объедини обе версии так, чтобы сохранить смысл обеих изменений. Ответь одним блоком кода — "
            "только текст, который должен стоять вместо конфликтного блока, без маркеров и без контекста. "
            "После блока — одна строка объяснения.")


RESOLVER_SYSTEM = ("Ты разрешаешь конфликты слияния git. Отвечаешь только блоком кода с итоговым текстом "
                   "и одной строкой пояснения. Файлы не читаешь и не меняешь.")
