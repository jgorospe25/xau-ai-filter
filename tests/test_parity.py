"""Parity against the MT5 indicator. Skipped until DumpSignals has been run and copied (xauai.parity)."""

import pandas as pd
import pytest

from xauai import config
from xauai.parity import DUMP_NAME, compare, load_dump

DUMP = config.MT5_DIR / DUMP_NAME
pytestmark = pytest.mark.skipif(
    not (DUMP.exists() and config.RAW_H1_PATH.exists()), reason="needs data/mt5 dump and raw parquet"
)


@pytest.fixture(scope="module")
def result():
    return compare(load_dump(DUMP), pd.read_parquet(config.RAW_H1_PATH))


def test_same_history_start(result):
    assert result.same_first_bar


def test_signal_times_identical(result):
    assert result.signals_match, f"only MT5:\n{result.only_mt5}\nonly Python:\n{result.only_py}"


def test_indicator_values_after_warmup(result):
    assert result.values_match, result.ind_max_diff_warm
