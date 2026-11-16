from app.serve import server_config


def test_local_server_keepalive_has_margin_over_gateway_idle_eviction():
    config = server_config()
    assert config.host == "127.0.0.1" and config.port == 8099 and config.factory
    assert config.timeout_keep_alive >= 10
