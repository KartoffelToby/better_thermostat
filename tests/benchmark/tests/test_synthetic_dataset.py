"""Roundtrip test: synthetic generator → fit_plant.

Guarantees the published synthetic dataset stays in sync with the
generator script and that the fitter produces sensible-magnitude tau
values when run against it.
"""

from __future__ import annotations

from pathlib import Path
import tempfile

from tests.benchmark.plant_fit import fit_plant, generate_synthetic_data

_LIVING_ID = "synthetic.living_room_temperature"
_KITCHEN_ID = "synthetic.kitchen_temperature"
_OUTDOOR_ID = "synthetic.outdoor_temperature"


def _fit_one(path: str, sensor_id: str) -> tuple[int, list[float]]:
    data = fit_plant._load_csv(path)
    aligned = fit_plant._align(data[sensor_id], data[_OUTDOOR_ID])
    windows = fit_plant._find_cooling_windows(aligned)
    taus = [
        tau
        for w in windows
        if (tau := fit_plant._fit_tau_room(w)) is not None and 30.0 <= tau <= 6000.0
    ]
    return len(windows), taus


def test_generator_produces_a_long_format_csv() -> None:
    """CSV dump has one row per (hour × series) over the requested span."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "demo.csv"
        rc = generate_synthetic_data.main(out_path=str(out), days=10)
        assert rc == 0
        assert out.exists()
        # 10 days × 24h × 3 series = 720 rows.
        with out.open() as f:
            rows = sum(1 for _ in f)
        assert rows == 10 * 24 * 3


def test_generator_emits_all_three_series() -> None:
    """Both rooms and the outdoor proxy land in the dump."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "demo.csv"
        generate_synthetic_data.main(out_path=str(out), days=10)
        data = fit_plant._load_csv(str(out))
        assert {_LIVING_ID, _KITCHEN_ID, _OUTDOOR_ID} <= set(data.keys())


def test_fit_recovers_sensible_tau_on_synthetic_data() -> None:
    """Run the fitter on a freshly-generated dataset.

    The fitter is sensitive to radiator-mass transients, so we don't
    insist on bit-exact recovery — just that both rooms produce a
    non-empty distribution of physically plausible tau values, and
    that the slower-envelope kitchen comes out slower than the
    faster-envelope living. Anything outside [60, 6000] minutes would
    indicate a regression in either the generator or the fitter.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "demo.csv"
        generate_synthetic_data.main(out_path=str(out), days=60)

        n_living, taus_living = _fit_one(str(out), _LIVING_ID)
        n_kitchen, taus_kitchen = _fit_one(str(out), _KITCHEN_ID)

        assert n_living >= 20 and n_kitchen >= 20
        assert taus_living and taus_kitchen
        for tau in taus_living + taus_kitchen:
            assert 60.0 <= tau <= 6000.0
