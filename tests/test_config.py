import pytest

from bruin_mcp_service.config import ConfigError, load_settings


def test_valid_dev_settings(base_env):
    s = load_settings(base_env)
    assert s.env == "dev"
    assert s.port == 18443
    assert s.max_upload_bytes == 100 * 1024 * 1024
    assert s.user_quota_bytes == 5 * 1024 ** 3
    assert s.allowed_input_ext == frozenset({".csv", ".xlsx"})


@pytest.mark.parametrize("host", ["0.0.0.0", "172.16.21.108", "::"])
def test_dev_refuses_non_loopback_bind(base_env, host):
    with pytest.raises(ConfigError, match="loopback"):
        load_settings({**base_env, "BMCP_BIND_HOST": host})


def test_prod_allows_lan_bind(base_env):
    s = load_settings({**base_env, "BMCP_ENV": "prod", "BMCP_BIND_HOST": "172.16.21.108",
                       "BMCP_PUBLIC_URL": "https://bruin-mcp.example.internal:8443"})
    assert s.bind_host == "172.16.21.108"


def test_prod_requires_https_public_url(base_env):
    with pytest.raises(ConfigError, match="https"):
        load_settings({**base_env, "BMCP_ENV": "prod", "BMCP_BIND_HOST": "172.16.21.108",
                       "BMCP_PUBLIC_URL": "http://172.16.21.108:8443"})


def test_dev_requires_loopback_public_url(base_env):
    with pytest.raises(ConfigError, match="loopback"):
        load_settings({**base_env, "BMCP_PUBLIC_URL": "http://172.16.21.108:18443"})


@pytest.mark.parametrize("url", ["127.0.0.1:18443", "http://127.0.0.1:18443/mcp",
                                 "ftp://127.0.0.1", "http://u:p@127.0.0.1:18443"])
def test_malformed_public_url(base_env, url):
    with pytest.raises(ConfigError, match="BMCP_PUBLIC_URL"):
        load_settings({**base_env, "BMCP_PUBLIC_URL": url})


def test_hostname_is_not_an_ip(base_env):
    with pytest.raises(ConfigError, match="IP address"):
        load_settings({**base_env, "BMCP_BIND_HOST": "localhost"})


def test_unknown_env_refused(base_env):
    with pytest.raises(ConfigError, match="BMCP_ENV"):
        load_settings({**base_env, "BMCP_ENV": "staging"})


@pytest.mark.parametrize("key", ["BMCP_WORKSPACE_ROOT", "BRUIN_BIN", "BRUIN_EXPECTED_VERSION"])
def test_missing_required_setting_named(base_env, key):
    env = dict(base_env)
    del env[key]
    with pytest.raises(ConfigError, match=key):
        load_settings(env)


def test_relative_path_refused(base_env):
    with pytest.raises(ConfigError, match="absolute"):
        load_settings({**base_env, "BMCP_LOG_DIR": "var/logs"})


@pytest.mark.parametrize("port", ["0", "70000", "abc"])
def test_bad_port(base_env, port):
    with pytest.raises(ConfigError, match="BMCP_PORT"):
        load_settings({**base_env, "BMCP_PORT": port})


def test_input_ext_subset_ok(base_env):
    s = load_settings({**base_env, "BMCP_ALLOWED_INPUT_EXT": ".CSV"})
    assert s.allowed_input_ext == frozenset({".csv"})


def test_input_ext_unsupported_refused(base_env):
    with pytest.raises(ConfigError, match="BMCP_ALLOWED_INPUT_EXT"):
        load_settings({**base_env, "BMCP_ALLOWED_INPUT_EXT": ".csv,.parquet"})
