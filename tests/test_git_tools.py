import subprocess

from tools.git_tools import changed_files, diff


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_diff_and_changed_files_on_a_real_repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "a.c").write_text("int a;\n")
    (tmp_path / "gone.c").write_text("int g;\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "a.c").write_text("int a = 1;\n")
    (tmp_path / "b.c").write_text("int b;\n")
    (tmp_path / "gone.c").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "change")

    text = diff("main", repo=tmp_path)
    assert "+int a = 1;" in text
    assert changed_files(text) == ["a.c", "b.c"]
