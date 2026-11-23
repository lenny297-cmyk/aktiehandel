"""Läsning och skrivning av JSON-state.

Varje skrivning är atomär: innehållet skrivs till en temporärfil som sedan byter
namn. Ett avbrutet jobb kan därmed aldrig lämna en halvskriven portfölj efter sig.

I dry run skrivs ingenting alls. DryRunStore tar emot samma anrop som Store men
sparar dem i minnet, så att hela flödet kan köras utan att röra repot.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def read_json(path, default=None):
    """Läs en JSON-fil. Saknas den returneras `default`."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise ValueError("{} ar trasig JSON: {}".format(path, exc)) from exc


def write_json(path, data) -> None:
    """Skriv JSON atomärt: temporärfil först, sedan namnbyte."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        prefix=target.name + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, str(target))
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


class Store:
    """Skriver på riktigt."""

    dry_run = False

    def read(self, path, default=None):
        return read_json(path, default)

    def write(self, path, data) -> None:
        write_json(path, data)
        log.info("skrev %s", path)

    def copy(self, source, destination) -> None:
        data = read_json(source)
        if data is None:
            return
        write_json(destination, data)


class DryRunStore(Store):
    """Läser från disk men skriver bara till minnet."""

    dry_run = True

    def __init__(self):
        self.written = {}

    def read(self, path, default=None):
        key = str(path)
        if key in self.written:
            return self.written[key]
        return read_json(path, default)

    def write(self, path, data) -> None:
        self.written[str(path)] = data
        log.info("[dry run] skulle skrivit %s", path)

    def copy(self, source, destination) -> None:
        data = self.read(source)
        if data is None:
            return
        self.write(destination, data)


def make_store(dry_run: bool) -> Store:
    return DryRunStore() if dry_run else Store()
