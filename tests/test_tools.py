"""Инструменты агентов: права, безопасность путей, работа с файлами, diff, тесты."""
import os
import sys

import pytest

from capricorn.config import load_config
from capricorn.tools import ROLE_TOOLS, Toolbox
from helpers import git, init_repo


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    init_repo(r)
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (r / "README.md").write_text("# demo\n", encoding="utf-8")
    (r / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    (r / "secret.txt").write_text("ignored", encoding="utf-8")
    return r


@pytest.fixture
def box(repo):
    """Общий режим (без worktree) — worktree проверяются в test_worktrees.py."""
    cfg = load_config()
    cfg.tools.worktrees = False
    return Toolbox(repo, cfg)


async def ok(box, name, agent="coder1", **args):
    res = await box.run(agent, name, args)
    assert not res.is_error, res.content
    return res


async def err(box, name, agent="coder1", **args):
    res = await box.run(agent, name, args)
    assert res.is_error, res.content
    assert res.content.startswith("Ошибка:")
    return res.content


# ------------------------------------------------------------------ права

def test_role_permissions(box):
    assert box.allowed("architect") == ROLE_TOOLS["architect"]
    assert "write_file" not in box.allowed("architect")
    assert "write_file" not in box.allowed("reviewer") and "run_tests" in box.allowed("reviewer")
    assert box.allowed("coder3") == [t for t in ROLE_TOOLS["coder"] if t != "sync_branch"]   # без worktree
    assert box.allowed("manager") == ["list_files", "read_file", "git_diff"]
    spec = {s["name"]: s for s in box.specs_for("coder1")}
    assert spec["edit_file"]["parameters"]["required"] == ["path", "old", "new"]


def test_config_switches_and_overrides(box):
    box.cfg.tools.write = False
    box.cfg.tools.run_tests = False
    assert not {"write_file", "edit_file", "run_tests"} & set(box.allowed("coder1"))
    box.cfg.agents["architect"].tools = ["read_file", "nonexistent", "read_file"]
    assert box.allowed("architect") == ["read_file"]
    box.cfg.tools.enabled = False
    assert box.allowed("coder1") == [] and box.specs_for("coder1") == []


async def test_forbidden_tool_and_bad_args(box):
    assert "недоступен" in await err(box, "write_file", agent="architect", path="x", content="")
    assert "недоступен" in await err(box, "rm_rf")
    assert "не хватает аргументов: content" in await err(box, "write_file", path="x")
    assert "лишние аргументы: mode" in await err(box, "read_file", path="README.md", mode="r")
    res = await box.run("coder1", "read_file", "не json")
    assert res.is_error and "JSON-объектом" in res.content


# ---------------------------------------------------------- безопасность путей

@pytest.mark.parametrize("path", ["../outside.txt", "src/../../outside.txt", ".git/config", ".git", "", "   "])
async def test_paths_outside_repo_or_git_are_rejected(box, repo, path):
    await err(box, "write_file", path=path, content="x")
    assert not (repo.parent / "outside.txt").exists()


async def test_absolute_path_inside_ok_outside_rejected(box, repo, tmp_path):
    res = await ok(box, "read_file", path=str(repo / "README.md"))
    assert "# demo" in res.content
    outside = tmp_path / "outside.txt"
    outside.write_text("секрет", encoding="utf-8")
    assert "вне репозитория" in await err(box, "read_file", path=str(outside))


async def test_symlink_escape_is_rejected(box, repo, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    try:
        os.symlink(target, repo / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            pytest.skip("нет прав на symlink")
        import _winapi                                  # junction не требует прав администратора
        _winapi.CreateJunction(str(target), str(repo / "link"))
    assert "вне репозитория" in await err(box, "write_file", path="link/x.txt", content="x")
    assert not (target / "x.txt").exists()


# ------------------------------------------------------------------ файлы

async def test_read_file_with_window(box, repo):
    (repo / "big.txt").write_text("".join(f"line {i}\n" for i in range(1, 11)), encoding="utf-8")
    res = await ok(box, "read_file", path="big.txt", start_line=3, max_lines=2)
    assert res.content == "big.txt — строк: 10, показаны 3-4\nline 3\nline 4\n"
    assert "нет такого файла" in await err(box, "read_file", path="nope.py")
    assert "каталог" in await err(box, "read_file", path="src")


async def test_write_file_creates_dirs_and_reports(box, repo):
    res = await ok(box, "write_file", path="pkg/sub/mod.py", content="x = 1\ny = 2\n")
    assert (repo / "pkg" / "sub" / "mod.py").read_text(encoding="utf-8") == "x = 1\ny = 2\n"
    assert res.content == "pkg/sub/mod.py создан, строк: 2"
    res = await ok(box, "write_file", path="pkg/sub/mod.py", content="кириллица")
    assert "перезаписан, строк: 1" in res.content
    assert (repo / "pkg/sub/mod.py").read_bytes() == "кириллица".encode("utf-8")


async def test_write_keeps_newlines_as_given(box, repo):
    await ok(box, "write_file", path="lf.txt", content="a\nb\n")
    assert (repo / "lf.txt").read_bytes() == b"a\nb\n"            # без подмены на CRLF в Windows


async def test_edit_file_unique_replace(box, repo):
    await ok(box, "edit_file", path="src/app.py", old="return 1", new="return 2")
    assert (repo / "src/app.py").read_text(encoding="utf-8") == "def main():\n    return 2\n"
    assert "не найден" in await err(box, "edit_file", path="src/app.py", old="return 42", new="x")
    (repo / "dup.py").write_text("a = 1\na = 1\n", encoding="utf-8")
    assert "встречается 2 раз" in await err(box, "edit_file", path="dup.py", old="a = 1", new="b")
    assert "old не пустой" in await err(box, "edit_file", path="dup.py", old="", new="b")


async def test_edit_file_matches_crlf_file(box, repo):
    (repo / "win.py").write_bytes(b"def f():\r\n    return 1\r\n")
    await ok(box, "edit_file", path="win.py", old="def f():\n    return 1", new="def f():\n    return 3")
    assert (repo / "win.py").read_bytes() == b"def f():\r\n    return 3\r\n"


async def test_list_files_respects_gitignore_and_filters(box):
    res = await ok(box, "list_files")
    files = res.content.splitlines()
    assert files == [".gitignore", "README.md", "src/app.py"]
    assert (await ok(box, "list_files", path="src")).content == "src/app.py"
    assert (await ok(box, "list_files", pattern="*.md")).content == "README.md"
    assert (await ok(box, "list_files", pattern="*.rs")).content == "(ничего не найдено)"


async def test_list_files_without_git(tmp_path):
    d = tmp_path / "plain"
    (d / "node_modules").mkdir(parents=True)
    (d / "node_modules" / "x.js").write_text("", encoding="utf-8")
    (d / "a.py").write_text("", encoding="utf-8")
    box = Toolbox(d, load_config())
    assert (await ok(box, "list_files")).content == "a.py"


async def test_search(box, repo):
    res = await ok(box, "search", pattern=r"def \w+")
    assert res.content == "src/app.py:1: def main():"
    assert res.summary == "1 совпадений"
    assert (await ok(box, "search", pattern="demo", glob="*.py")).content == "(совпадений нет)"
    assert "некорректное регулярное выражение" in await err(box, "search", pattern="(")


# ------------------------------------------------------------------- git

async def test_git_diff_shows_changes_and_untracked(box, repo):
    assert "изменений в отслеживаемых файлах нет" in (await ok(box, "git_diff")).content
    (repo / "src/app.py").write_text("def main():\n    return 42\n", encoding="utf-8")
    (repo / "new.py").write_text("x", encoding="utf-8")
    res = await ok(box, "git_diff")
    assert "+    return 42" in res.content and "-    return 1" in res.content
    assert "Новые неотслеживаемые файлы" in res.content and "new.py" in res.content
    assert "secret.txt" not in res.content
    assert res.summary == "1 файлов в diff"
    only_readme = await ok(box, "git_diff", path="README.md")
    assert "return 42" not in only_readme.content and "new.py" not in only_readme.content
    git(repo, "add", "src/app.py")
    staged = await ok(box, "git_diff", staged=True)
    assert "+    return 42" in staged.content and "Новые" not in staged.content


async def test_git_diff_outside_git(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    box = Toolbox(d, load_config())
    assert "не git-репозиторий" in await err(box, "git_diff")


# ----------------------------------------------------------------- тесты

def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


async def test_run_tests_reports_exit_code_and_output(box):
    box.cfg.tools.test_command = py("print('3 passed')")
    res = await ok(box, "run_tests")
    assert "код выхода: 0" in res.content and "3 passed" in res.content
    box.cfg.tools.test_command = py("import sys; print('1 failed'); sys.exit(1)")
    res = await ok(box, "run_tests")                      # провал тестов — не ошибка инструмента
    assert "код выхода: 1" in res.content and res.summary == "код выхода 1"


async def test_run_tests_passes_validated_target(box):
    box.cfg.tools.test_command = py("import sys; print('ARGS', sys.argv[1:])")
    res = await ok(box, "run_tests", target="src/app.py")
    assert "ARGS ['src/app.py']" in res.content
    assert "вне репозитория" in await err(box, "run_tests", target="../x")
    assert "нет такого файла" in await err(box, "run_tests", target="tests/missing.py")


async def test_run_tests_string_command_and_cwd(box, repo):
    box.cfg.tools.test_command = f'"{sys.executable}" -c "import os; print(os.getcwd())"'
    res = await ok(box, "run_tests")
    assert str(repo) in res.content


async def test_run_tests_timeout_kills_process(box):
    box.cfg.tools.test_command = py("import time; time.sleep(30)")
    box.cfg.tools.test_timeout = 1
    assert "не уложились в 1 с" in await err(box, "run_tests")


async def test_run_tests_missing_binary(box):
    box.cfg.tools.test_command = ["definitely-not-a-binary-xyz"]
    assert "не удалось запустить" in await err(box, "run_tests")


async def test_run_tests_cyrillic_output_without_utf8_mode(box, monkeypatch):
    """Регрессия: без PYTHONUTF8 у родителя вывод дочернего Python шёл в cp1251 и ломался."""
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    box.cfg.tools.test_command = py("print('тест упал: ошибка'); print('строка 2')")
    res = await ok(box, "run_tests")
    assert "тест упал: ошибка\nстрока 2" in res.content
    assert "\r" not in res.content


async def test_run_tests_non_utf8_runner_falls_back_to_locale(box, monkeypatch):
    import capricorn.tools as tools_mod
    monkeypatch.setattr(tools_mod.locale, "getpreferredencoding", lambda do_setlocale=True: "cp1251")
    box.cfg.tools.test_command = py("import sys; sys.stdout.buffer.write('сбой\\r\\n'.encode('cp1251'))")
    res = await ok(box, "run_tests")
    assert res.content.endswith("сбой\n")


async def test_run_tests_output_is_clipped_from_the_tail(box):
    box.cfg.tools.test_command = py("print('x' * 50000); print('ИТОГ')")
    res = await ok(box, "run_tests")
    assert res.content.rstrip().endswith("ИТОГ") and "обрезано" in res.content
    assert len(res.content) < 21_000
