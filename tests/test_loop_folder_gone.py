"""The loop stops when its data folder disappears, rather than retrying forever.

Moving the install folder while `sellbird run` ran left it failing every cycle
with "unable to open database file" for days, holding approved email.
"""

from sellbird.main import database_folder_gone


def test_a_database_in_an_existing_folder_is_fine(tmp_path):
    assert database_folder_gone(str(tmp_path / "sellbird.db")) is None


def test_a_moved_folder_is_reported(tmp_path):
    gone = tmp_path / "moved" / "data"
    assert database_folder_gone(str(gone / "sellbird.db")) == str(gone)


def test_memory_databases_are_never_gone():
    assert database_folder_gone(":memory:") is None
