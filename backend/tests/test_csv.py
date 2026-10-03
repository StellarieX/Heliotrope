"""CSV provider: valid loads, strict rejects, fill interpolates, big gaps refuse."""

from datetime import timedelta

import pytest

from app.domain.carbon import Quality
from app.services.providers.csv_provider import CSVConfig, CSVProvider, ProviderDataInvalid


def write_csv(path, rows):
    path.write_text("timestamp,carbon_intensity_gco2_per_kwh\n" + "\n".join(rows), encoding="utf-8")


def day_rows(start, values):
    return [f"{(start + timedelta(minutes=15 * i)).isoformat()},{v}" for i, v in enumerate(values)]


def test_valid_csv_loads(tmp_path, window):
    start, _ = window
    f = tmp_path / "grid.csv"
    write_csv(f, day_rows(start, [400 + i for i in range(96)]))
    points, missing, interpolated = CSVProvider(CSVConfig(path=str(f))).get_signal(start, start + timedelta(hours=24), 15)
    assert len(points) == 96
    assert missing == 0 and interpolated == 0
    assert all(p.quality == Quality.MEASURED for p in points)
    assert points[0].gco2_per_kwh == 400


def test_bad_timestamp_rejected(tmp_path, window):
    start, _ = window
    f = tmp_path / "bad.csv"
    write_csv(f, ["not-a-time,400"])
    with pytest.raises(ProviderDataInvalid, match="invalid timestamp"):
        CSVProvider(CSVConfig(path=str(f))).get_signal(start, start + timedelta(hours=24), 15)


def test_negative_rejected(tmp_path, window):
    start, _ = window
    f = tmp_path / "neg.csv"
    write_csv(f, day_rows(start, [-5] * 96))
    with pytest.raises(ProviderDataInvalid, match="negative"):
        CSVProvider(CSVConfig(path=str(f))).get_signal(start, start + timedelta(hours=24), 15)


def test_duplicate_rejected(tmp_path, window):
    start, _ = window
    rows = day_rows(start, [400] * 96)
    rows[10] = rows[9]
    f = tmp_path / "dup.csv"
    write_csv(f, rows)
    with pytest.raises(ProviderDataInvalid, match="duplicate"):
        CSVProvider(CSVConfig(path=str(f))).get_signal(start, start + timedelta(hours=24), 15)


def test_strict_missing_errors(tmp_path, window):
    start, _ = window
    rows = day_rows(start, [400] * 96)
    del rows[40:42]
    f = tmp_path / "gap.csv"
    write_csv(f, rows)
    with pytest.raises(ProviderDataInvalid, match="missing"):
        CSVProvider(CSVConfig(path=str(f), missing_data="strict")).get_signal(start, start + timedelta(hours=24), 15)


def test_fill_small_gap_interpolates(tmp_path, window):
    start, _ = window
    values = [400.0] * 96
    rows = day_rows(start, values)
    del rows[40]  # single 15-min hole between 400 and 400
    f = tmp_path / "fill.csv"
    write_csv(f, rows)
    points, missing, interpolated = CSVProvider(CSVConfig(path=str(f), missing_data="fill")).get_signal(
        start, start + timedelta(hours=24), 15
    )
    assert len(points) == 96
    assert missing == 1 and interpolated == 1
    assert points[40].gco2_per_kwh == pytest.approx(400.0)
    assert points[40].quality == Quality.INTERPOLATED


def test_fill_large_gap_refuses(tmp_path, window):
    start, _ = window
    rows = day_rows(start, [400] * 96)
    del rows[40:50]  # 150-min hole > 60-min guard
    f = tmp_path / "big.csv"
    write_csv(f, rows)
    with pytest.raises(ProviderDataInvalid, match="exceeds max"):
        CSVProvider(CSVConfig(path=str(f), missing_data="fill")).get_signal(start, start + timedelta(hours=24), 15)


def test_missing_file_is_invalid(tmp_path, window):
    start, _ = window
    with pytest.raises(ProviderDataInvalid, match="cannot read"):
        CSVProvider(CSVConfig(path=str(tmp_path / "nope.csv"))).get_signal(start, start + timedelta(hours=24), 15)
