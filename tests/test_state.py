"""Atomär JSON-skrivning och dry run-läget."""

from __future__ import annotations

import json

import pytest

from aiaktier.state import DryRunStore, Store, make_store, read_json, write_json


def test_skriver_och_laser_tillbaka(tmp_path):
    target = tmp_path / "data" / "portfolio.json"
    write_json(target, {"cash": 100_000.0, "positions": []})

    assert read_json(target) == {"cash": 100_000.0, "positions": []}


def test_skapar_katalogen_vid_behov(tmp_path):
    target = tmp_path / "djupt" / "nere" / "fil.json"
    write_json(target, {"a": 1})
    assert target.exists()


def test_saknad_fil_ger_default(tmp_path):
    assert read_json(tmp_path / "finns-inte.json") is None
    assert read_json(tmp_path / "finns-inte.json", []) == []


def test_trasig_json_kastar_tydligt_fel(tmp_path):
    target = tmp_path / "trasig.json"
    target.write_text("{inte json", encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        read_json(target)
    assert "trasig" in str(exc.value)


def test_svenska_tecken_skrivs_som_tecken_inte_escape(tmp_path):
    target = tmp_path / "fil.json"
    write_json(target, {"motivering": "stark rapport och låg värdering"})

    text = target.read_text(encoding="utf-8")
    assert "låg värdering" in text
    assert "\\u" not in text


def test_inga_temporarfiler_lamnas_kvar(tmp_path):
    target = tmp_path / "fil.json"
    for i in range(3):
        write_json(target, {"runda": i})

    kvar = sorted(p.name for p in tmp_path.iterdir())
    assert kvar == ["fil.json"]


def test_befintlig_fil_overlever_ett_misslyckat_skrivforsok(tmp_path):
    target = tmp_path / "fil.json"
    write_json(target, {"cash": 100_000})

    class EjSerialiserbar:
        pass

    with pytest.raises(TypeError):
        write_json(target, {"cash": EjSerialiserbar()})

    # Den gamla filen ska vara orörd och fortfarande läsbar.
    assert read_json(target) == {"cash": 100_000}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fil.json"]


# ------------------------------------------------------------- dry run


def test_dry_run_skriver_ingenting_till_disk(tmp_path):
    store = DryRunStore()
    target = tmp_path / "fil.json"

    store.write(target, {"cash": 1})

    assert not target.exists()
    # Men läsningen ser det som skrevs, så flödet kan köras hela vägen.
    assert store.read(target) == {"cash": 1}


def test_dry_run_laser_befintliga_filer_fran_disk(tmp_path):
    target = tmp_path / "fil.json"
    write_json(target, {"cash": 42})

    store = DryRunStore()
    assert store.read(target) == {"cash": 42}


def test_dry_run_kopiering_ror_inte_malet(tmp_path):
    source = tmp_path / "a.json"
    destination = tmp_path / "b.json"
    write_json(source, {"x": 1})

    DryRunStore().copy(source, destination)

    assert not destination.exists()


def test_store_kopierar_pa_riktigt(tmp_path):
    source = tmp_path / "a.json"
    destination = tmp_path / "docs" / "a.json"
    write_json(source, {"x": 1})

    Store().copy(source, destination)

    assert json.loads(destination.read_text(encoding="utf-8")) == {"x": 1}


def test_kopiering_av_saknad_kalla_ar_tyst(tmp_path):
    Store().copy(tmp_path / "finns-inte.json", tmp_path / "ut.json")
    assert not (tmp_path / "ut.json").exists()


def test_make_store_valjer_ratt_sort():
    assert make_store(True).dry_run is True
    assert make_store(False).dry_run is False
