import pytest


@pytest.fixture(autouse=True)
def isolated_environment_file(tmp_path, monkeypatch):
    """Tests, including subprocesses, never load the user's project environment."""
    environment_file = tmp_path / "test.env"
    environment_file.write_text("")
    monkeypatch.setenv("JOB_MATCHER_ENV_FILE", str(environment_file))
