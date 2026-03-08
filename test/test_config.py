# Tests for config loading

from pathlib import Path

from configs.config import load_config, load_universe


class TestConfigLoading:
    def test_load_hk_config(self, config_path):
        config = load_config(config_path, "HK")
        assert config.name == "HK"
        assert config.timezone == "Asia/Hong_Kong"
        assert config.order_valid_time == 5
        assert config.refresh_buffer == 15

    def test_sessions_parsed(self, config_path):
        config = load_config(config_path, "HK")
        assert len(config.sessions) == 2
        assert config.sessions[0].start_hour == 9
        assert config.sessions[0].start_minute == 30
        assert config.sessions[0].end_hour == 12
        assert config.sessions[1].start_hour == 13
        assert config.sessions[1].end_hour == 16

    def test_default_cap(self, config_path):
        config = load_config(config_path, "HK")
        assert config.single_name_cap == 50000

    def test_override_stock_limit(self, config_path):
        config = load_config(config_path, "HK")
        assert config.get_stock_limit("0005.HK") == 100000
        assert config.get_stock_limit("0700.HK") == 20000

    def test_fallback_to_default(self, config_path):
        config = load_config(config_path, "HK")
        assert config.get_stock_limit("9988.HK") == 50000

    def test_notional_limits(self, config_path):
        config = load_config(config_path, "HK")
        assert config.max_buy_notional == 10_000_000
        assert config.max_sell_notional == 10_000_000

    def test_new_config_fields(self, config_path):
        config = load_config(config_path, "HK")
        assert config.partial_change_threshold == 0.10
        assert config.refill_fill_threshold == 0.50


class TestUniverseLoading:
    def test_load_universe(self):
        csv_path = Path(__file__).parent.parent / "configs" / "hk_universe.csv"
        rics = load_universe(csv_path)
        assert len(rics) == 5
        assert "0005.HK" in rics
        assert "0700.HK" in rics
