import json
from contextlib import closing

import pytest

from job_matcher import storage
from job_matcher.sources.base import Board
from job_matcher.sources.http import SourceError
from job_matcher.storage import Store


@pytest.fixture
def cached_board(tmp_path):
    store = Store(tmp_path)
    board, _ = store.save_board(
        Board(provider="greenhouse", token="acme", company="Acme").model_dump(),
        board_url="https://boards.greenhouse.io/acme",
        board_name="Acme",
        verified=True,
        method="careers-url",
    )
    return store, board["id"]


def test_oversized_cache_update_preserves_previous_listing(cached_board, monkeypatch):
    store, board_id = cached_board
    store.save_board_listing(board_id, [], horizon_days=7, complete=True, job_count=0)
    before = store.board_listing(board_id)
    monkeypatch.setattr(storage, "MAX_BOARD_BYTES", 64)
    with pytest.raises(SourceError, match="byte limit"):
        store.save_board_listing(
            board_id,
            [{"description": "x" * 65}],
            horizon_days=7,
            complete=True,
            job_count=1,
        )
    assert store.board_listing(board_id) == before


def test_old_oversized_cache_is_not_decoded(cached_board, monkeypatch):
    store, board_id = cached_board
    store.save_board_listing(
        board_id, [{"description": "x" * 65}], horizon_days=7, complete=True, job_count=1
    )
    monkeypatch.setattr(storage, "MAX_BOARD_BYTES", 64)
    with pytest.raises(SourceError, match="byte limit"):
        store.board_listing(board_id)


def test_listing_count_is_bounded_before_persistence(cached_board, monkeypatch):
    store, board_id = cached_board
    monkeypatch.setattr(storage, "MAX_COLLECTION_ITEMS", 2)
    with pytest.raises(SourceError, match="too many postings"):
        store.save_board_listing(board_id, [{}, {}, {}], horizon_days=7, complete=True, job_count=3)
    assert store.board_listing(board_id) is None


def test_detail_cache_size_is_bounded_and_old_large_rows_are_skipped(cached_board, monkeypatch):
    store, board_id = cached_board
    monkeypatch.setattr(storage, "MAX_RESPONSE_BYTES", 64)
    data = {"description": "x" * 65}
    with pytest.raises(SourceError, match="byte limit"):
        store.save_board_posting(board_id, "1", data)
    assert store.board_postings(board_id, ["1"], "") == {}
    with closing(store.connect()) as db, db:
        db.execute(
            "INSERT INTO board_postings VALUES(?,?,?,?)", (board_id, "1", "old", json.dumps(data))
        )
    assert store.board_postings(board_id, ["1"], "") == {}
