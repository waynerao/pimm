# Tests for config loading (TOML format)

from pathlib import Path

from pimm.config import load_all_markets, load_market_config, load_pimm_config, load_universe


class TestPimmConfig:
    def test_load_pimm_config(self, config_path):
        cfg = load_pimm_config(config_path)
        assert cfg.web_port == 8080
        assert cfg.max_staleness_s == 30
        assert cfg.full_batch_interval_m == 10
        assert cfg.min_dispatch_interval_s == 5
        assert cfg.delta_beta_interval_s == 5

    def test_defaults(self, tmp_path):
        toml_file = tmp_path / "empty.toml"
        toml_file.write_text("")
        cfg = load_pimm_config(toml_file)
        assert cfg.web_port == 8080
        assert cfg.max_staleness_s == 30


class TestMarketConfig:
    def test_load_hk_config(self, config_path):
        config = load_market_config(config_path, "HK")
        assert config.name == "HK"
        assert config.order_valid_time_m == 5
        assert config.refresh_buffer_s == 15

    def test_sessions_parsed(self, config_path):
        config = load_market_config(config_path, "HK")
        assert len(config.sessions) == 2
        assert config.sessions[0].start_hour == 9
        assert config.sessions[0].start_minute == 30
        assert config.sessions[0].end_hour == 12
        assert config.sessions[1].start_hour == 13
        assert config.sessions[1].end_hour == 16

    def test_default_cap(self, config_path):
        config = load_market_config(config_path, "HK")
        assert config.single_name_cap == 50000

    def test_override_stock_limit(self, config_path):
        config = load_market_config(config_path, "HK")
        assert config.get_stock_limit("0005.HK") == 100000
        assert config.get_stock_limit("0700.HK") == 20000

    def test_fallback_to_default(self, config_path):
        config = load_market_config(config_path, "HK")
        assert config.get_stock_limit("9988.HK") == 50000

    def test_notional_limits(self, config_path):
        config = load_market_config(config_path, "HK")
        assert config.max_buy_notional == 10_000_000
        assert config.max_sell_notional == 10_000_000

    def test_thresholds(self, config_path):
        config = load_market_config(config_path, "HK")
        assert config.partial_change_threshold == 0.10
        assert config.refill_fill_threshold == 0.50

    def test_alpha_enabled(self, config_path):
        hk = load_market_config(config_path, "HK")
        cn = load_market_config(config_path, "CN")
        assert hk.alpha_enabled is True
        assert cn.alpha_enabled is False

    def test_defaults_merge(self, tmp_path):
        toml_file = tmp_path / "test.toml"
        toml_file.write_text("""
[market_defaults]
order_valid_time_m = 7
single_name_cap = 99999

[market.X]
sessions = ["09:00-12:00"]
order_valid_time_m = 10
""")
        cfg = load_market_config(toml_file, "X")
        assert cfg.order_valid_time_m == 10
        assert cfg.single_name_cap == 99999

    def test_load_all_markets(self, config_path):
        configs = load_all_markets(config_path)
        assert "HK" in configs
        assert configs["HK"].name == "HK"
        assert "CN" in configs
        assert "TW" in configs


class TestUniverseLoading:
    def test_load_universe(self):
        csv_path = Path(__file__).parent.parent / "configs" / "hk_universe.csv"
        rics = load_universe(csv_path)
        assert len(rics) == 5
        assert "0005.HK" in rics
        assert "0700.HK" in rics
