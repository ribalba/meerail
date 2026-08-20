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

from core.config import AccountConfig, EXAMPLE_CONFIG_PATH, Settings

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
    # A year, not everything. A search costs what it has to look at, so the
    # install that never touches this setting is the one that most needs it to
    # be narrow — see app/routers/search.py, where a request that names no
    # window gets this one rather than twenty years of mail.
    assert load().default_search_years == 1


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


def test_the_shipped_example_configuration_loads(tmp_path, monkeypatch):
    """The documented starting configuration must be accepted verbatim."""
    path = tmp_path / "meerail.toml"
    path.write_text(EXAMPLE_CONFIG_PATH.read_text())
    monkeypatch.setenv("MEERAIL_CONFIG", str(path))
    # Do not let an operator's own environment turn this into a test of their
    # settings. The sample itself is the complete input under test.
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    assert load().llm_timeout_seconds == 180


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


def test_account_name_applies_to_every_address():
    account = AccountConfig(email="you@proton.me", name="Your Name",
                            addresses=["alias@proton.me"])
    assert account.send_identities() == [("Your Name", "you@proton.me"),
                                         ("Your Name", "alias@proton.me")]


def test_bracketed_address_names_that_one_address():
    account = AccountConfig(email="you@proton.me", name="Your Name",
                            addresses=["Work You <work@example.com>", "alias@proton.me"])
    assert account.send_identities() == [("Your Name", "you@proton.me"),
                                         ("Work You", "work@example.com"),
                                         ("Your Name", "alias@proton.me")]
    # The address alone is what the rest of the system compares against.
    assert account.send_addresses() == ["you@proton.me", "work@example.com",
                                        "alias@proton.me"]


def test_primary_can_be_named_by_listing_it():
    # The one reason to repeat the primary under `addresses`: it takes the name
    # and keeps its place at the front, rather than appearing twice.
    account = AccountConfig(email="You@proton.me",
                            addresses=["Arne Tarara <you@proton.me>", "alias@proton.me"])
    assert account.send_identities() == [("Arne Tarara", "You@proton.me"),
                                         ("", "alias@proton.me")]


def test_display_name_case_is_preserved():
    # The whole entry used to be lower-cased on its way to the database, so a
    # name punched in as "Arne Tarara <...>" went out as "arne tarara".
    account = AccountConfig(email="you@proton.me",
                            addresses=["Arne Tarara <ARNE@green-coding.io>"])
    assert account.send_identities()[1] == ("Arne Tarara", "ARNE@green-coding.io")


def test_address_without_an_address_is_rejected():
    with pytest.raises(ValueError, match="no email address in it"):
        AccountConfig(email="you@proton.me", addresses=["Arne Tarara"])


# --- presentation pinned in the file -----------------------------------------


def test_nothing_is_pinned_by_default():
    # The ordinary install: Settings owns name, colour and footer, and the agent
    # has nothing to say about them.
    assert AccountConfig(email="you@proton.me").presentation() == {}


def test_only_the_fields_written_in_the_file_are_pinned():
    account = AccountConfig(email="you@proton.me", label="Personal", color="#ff0000")
    assert account.presentation() == {"label": "Personal", "color": "#ff0000"}


def test_an_empty_footer_is_pinned_rather_than_ignored():
    # "" and "absent" are different answers: one says this account has no
    # footer, the other says Settings decides. Falsiness must not merge them.
    assert AccountConfig(email="you@proton.me", footer="").presentation() == {"footer": ""}


@pytest.mark.parametrize("field, value", [("label", "x" * 201), ("color", "x" * 33)])
def test_presentation_too_wide_for_its_column_is_rejected(field, value):
    # Otherwise this surfaces as a psycopg error on the agent's first pass,
    # naming neither the file nor the key.
    with pytest.raises(ValueError, match="the limit is"):
        AccountConfig(email="you@proton.me", **{field: value})


def test_presentation_loads_from_the_account_block(tmp_path, monkeypatch):
    path = tmp_path / "meerail.toml"
    path.write_text(
        '[[agent.account]]\nemail = "you@proton.me"\n'
        'label = "Personal"\ncolor = "#1d6ff2"\nfooter = "Sent from meerail"\n'
    )
    monkeypatch.setenv("MEERAIL_CONFIG", str(path))
    (account,) = load().accounts
    assert account.presentation() == {"label": "Personal", "color": "#1d6ff2",
                                      "footer": "Sent from meerail"}
