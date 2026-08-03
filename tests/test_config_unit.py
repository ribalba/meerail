"""The one place a setting lives, and what is allowed to override it.

meerail used to configure the same thing twice: `STORE_RAW_MIME` and
`CONTENT_WINDOW_MONTHS` sat in both `.env` and `agent/config.toml`, with the file
quietly winning in the one process that read either (issue #1). Now there is one
`meerail.toml`, the environment overrides it, and nothing else is read.

These tests pin the order — environment > .env > meerail.toml > default — because
it is invisible at the call site: every consumer just reads `get_settings().x`,
so a regression here would surface as mail silently stored the wrong way rather
than as an error.

Pure unit test: no database, no containers. Each case builds `Settings` directly
against a temp file, which skips `get_settings()`'s lru_cache and its data_dir
mkdir.
"""

import pytest

from core.config import AccountConfig, Settings

CONFIG = """\
[database]
url = "postgresql+psycopg://file@filehost/filedb"

[server]
secret_key = "from-file"
session_max_age_days = 7

[agent]
store_raw_mime = false
content_window_months = 18

[[agent.account]]
email = "you@proton.me"
password = "bridge-pw"
"""


def load() -> Settings:
    """Settings with the repository's own .env taken out of the picture.

    `.env` sits between the environment and meerail.toml by design, so on a
    checkout that has one it would answer these assertions instead of the file
    under test. `_env_file=None` is pydantic-settings' per-instance override.
    """
    return Settings(_env_file=None)


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Write meerail.toml and point the loader at it, with a clean environment."""
    path = tmp_path / "meerail.toml"
    path.write_text(CONFIG)
    monkeypatch.setenv("MEERAIL_CONFIG", str(path))
    # A developer's real environment must not decide what these assertions see.
    for name in ("DATABASE_URL", "SECRET_KEY", "STORE_RAW_MIME", "CONTENT_WINDOW_MONTHS",
                 "SERVER_PASSWORD", "SESSION_MAX_AGE_DAYS", "DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    return path


def test_file_supplies_every_section(config):
    s = load()
    assert s.database_url == "postgresql+psycopg://file@filehost/filedb"   # [database]
    assert s.secret_key == "from-file"                                      # [server]
    assert s.store_raw_mime is False                                        # [agent]


def test_defaults_fill_the_gaps(config):
    # Not mentioned anywhere in CONFIG.
    assert load().poll_interval == 30
    assert load().contacts_scan_years == 1


def test_environment_overrides_the_file(config, monkeypatch):
    # The whole point of the layout: a container can override without editing
    # the file, and a `${VAR:-default}` in a compose file is therefore dangerous.
    monkeypatch.setenv("STORE_RAW_MIME", "true")
    monkeypatch.setenv("CONTENT_WINDOW_MONTHS", "3")
    s = load()
    assert s.store_raw_mime is True
    assert s.content_window_months == 3
    # Untouched keys still come from the file rather than reverting to defaults.
    assert s.session_max_age_days == 7


def test_dotenv_sits_between_the_two(config, tmp_path, monkeypatch):
    """.env beats meerail.toml, and the environment still beats .env."""
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=from-dotenv\nSESSION_MAX_AGE_DAYS=99\n")
    monkeypatch.setenv("SESSION_MAX_AGE_DAYS", "1")
    s = Settings(_env_file=env_file)
    assert s.secret_key == "from-dotenv"      # .env over the file's "from-file"
    assert s.session_max_age_days == 1        # environment over .env's 99
    assert s.store_raw_mime is False          # file, untouched by either


def test_env_only_mode_reads_no_files(tmp_path, monkeypatch):
    """MEERAIL_CONFIG="" skips meerail.toml *and* .env — what `make test` uses."""
    (tmp_path / "meerail.toml").write_text(CONFIG)
    monkeypatch.setenv("MEERAIL_CONFIG", "")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    s = Settings()
    assert s.secret_key == "dev-insecure-secret-change-me"   # the built-in default
    assert s.accounts == []


def test_unknown_key_is_rejected_not_ignored(tmp_path, monkeypatch):
    # A typo that silently does nothing is the failure mode issue #1 reported.
    path = tmp_path / "meerail.toml"
    path.write_text('[agent]\nstore_raw_mime = false\nstore_raw_mim = true\n')
    monkeypatch.setenv("MEERAIL_CONFIG", str(path))
    with pytest.raises(SystemExit, match="unknown key 'store_raw_mim'"):
        load()


def test_old_agent_config_is_named_not_half_read(tmp_path, monkeypatch):
    """The hard cut: the pre-unification format gets a migration command, not a
    partial load that would drop every account on the floor."""
    path = tmp_path / "meerail.toml"
    path.write_text('store_raw_mime = false\n\n[[account]]\nemail = "you@proton.me"\n')
    monkeypatch.setenv("MEERAIL_CONFIG", str(path))
    with pytest.raises(SystemExit, match="python -m core.config migrate"):
        load()


def test_accounts_load_from_the_agent_section(config):
    (account,) = load().accounts
    assert account.email == "you@proton.me"
    # Filled in from `email`, which the old loader did by hand at parse time.
    assert account.username == "you@proton.me"


@pytest.mark.parametrize("mode", ["STARTTLS", " StartTLS "])
def test_security_mode_is_normalised(mode):
    # imap.py/smtp.py compare these strings exactly; an unnormalised "STARTTLS"
    # used to fall through to an unencrypted socket.
    assert AccountConfig(email="a@b.c", imap_security=mode).imap_security == "starttls"


def test_unknown_security_mode_is_rejected():
    with pytest.raises(ValueError, match="not valid"):
        AccountConfig(email="a@b.c", smtp_security="tls")


def test_send_addresses_dedupes_case_insensitively():
    account = AccountConfig(email="You@proton.me",
                            addresses=["alias@proton.me", "you@PROTON.me"])
    assert account.send_addresses() == ["You@proton.me", "alias@proton.me"]
