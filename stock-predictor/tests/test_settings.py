import os
import tempfile
import unittest
from datetime import time
from pathlib import Path

from app.settings import PROJECT_ROOT, Settings, load_settings, parse_hhmm


def write_toml(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


class SettingsTest(unittest.TestCase):
    def test_example_file_matches_confirmed_constants(self):
        s = load_settings(PROJECT_ROOT / "config" / "settings.example.toml", environ={})
        self.assertEqual((s.scoring.ma_k, s.scoring.market_div, s.scoring.us_div, s.scoring.volume_ln_base), (1.5, 2.7, 2.5, 4.0))
        self.assertEqual(s.history.load_trading_days, 870)
        self.assertEqual(s.schedule.close_confirm_time, time(16, 30))
        self.assertEqual(s.schedule.prediction_open_time, time(7, 0))
        self.assertEqual((s.seed.kospi_count, s.seed.kosdaq_count), (70, 30))

    def test_empty_file_gives_code_defaults(self):
        # 설정 파일의 내용과 상관없이: 빈 파일이면 코드의 기본값이 그대로 쓰인다
        self.assertEqual(load_settings(write_toml(""), environ={}), Settings())

    def test_default_search_path_works(self):
        # 사용자의 config/settings.toml이 있든 없든 오류 없이 읽힌다(값은 사용자 설정이므로 확인하지 않는다)
        self.assertIsInstance(load_settings(environ={}), Settings)

    def test_password_only_from_environment(self):
        path = write_toml('[database]\nuser = "tester"\n')      # 사용자 이름을 직접 정해서 예시 파일의 내용에 기대지 않는다
        s = load_settings(path, environ={"STOCK_DB_PASSWORD": "p@ss word/1"})
        self.assertEqual(s.database.password, "p@ss word/1")
        self.assertEqual(s.database.url(), "mysql+pymysql://tester:p%40ss+word%2F1@127.0.0.1:3306/stockdb?charset=utf8mb4")  # 특수문자는 인코딩된다

    def test_password_in_file_is_rejected(self):
        path = write_toml('[database]\npassword = "secret"\n')
        with self.assertRaises(ValueError):
            load_settings(path, environ={})

    def test_unknown_key_and_section_are_rejected(self):
        with self.assertRaises(ValueError):
            load_settings(write_toml("[scoring]\nma_kk = 1\n"), environ={})
        with self.assertRaises(ValueError):
            load_settings(write_toml("[scorring]\nma_k = 1\n"), environ={})

    def test_validation(self):
        with self.assertRaises(ValueError):
            load_settings(write_toml("[scoring]\nma_k = 0\n"), environ={})
        with self.assertRaises(ValueError):
            load_settings(write_toml("[history]\nload_trading_days = 100\n"), environ={})
        with self.assertRaises(ValueError):
            load_settings(write_toml('[schedule]\nclose_confirm_time = "4:30pm"\n'), environ={})

    def test_dump_command_setting(self):
        s = load_settings(write_toml("[paths]\ndump_command = 'C:\\Program Files\\MariaDB\\bin\\mariadb-dump.exe'\n"), environ={})
        self.assertEqual(s.paths.dump_command, "C:\\Program Files\\MariaDB\\bin\\mariadb-dump.exe")
        self.assertEqual(load_settings(write_toml("[seed]\nkospi_count = 1\n"), environ={}).paths.dump_command, "")   # 지정하지 않으면 빈 값

    def test_missing_explicit_file(self):
        with self.assertRaises(FileNotFoundError):
            load_settings("/no/such/file.toml", environ={})

    def test_env_config_path(self):
        path = write_toml("[seed]\nkospi_count = 5\nkosdaq_count = 2\n")
        s = load_settings(environ={"STOCK_CONFIG": path})
        self.assertEqual((s.seed.kospi_count, s.seed.kosdaq_count), (5, 2))

    def test_parse_hhmm_and_resolve(self):
        self.assertEqual(parse_hhmm("07:05"), time(7, 5))
        s = load_settings(environ={})
        self.assertEqual(s.resolve("config/extra_holidays.csv"), PROJECT_ROOT / "config" / "extra_holidays.csv")
        self.assertTrue(s.resolve("/abs/path").is_absolute())


if __name__ == "__main__":
    unittest.main()
