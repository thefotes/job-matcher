import pytest

from job_matcher.sources.base import Board
from job_matcher.storage import Store


@pytest.mark.parametrize("provider", ["smartrecruiters", "recruitee"])
def test_legacy_self_name_verification_requires_confirmation_without_losing_records(
    tmp_path, provider
):
    store = Store(tmp_path)

    def save(token, method):
        return store.save_board(
            Board(provider=provider, token=token, company="Dummy Company").model_dump(),
            board_url=f"https://example.invalid/{token}",
            board_name="Dummy Company",
            verified=True,
            method=method,
        )[0]

    legacy = save("legacy", "board-name")
    explicit = save("explicit", "careers-url")
    imported = save("imported", "import:board-name")
    store.save_board_listing(legacy["id"], [], horizon_days=7, complete=True, job_count=0)
    listing = store.board_listing(legacy["id"])

    migrated = Store(tmp_path)
    rows = {b["id"]: b for b in migrated.boards()}
    assert len(rows) == 3
    assert rows[legacy["id"]]["active"] is False
    assert rows[legacy["id"]]["verified"] is False
    assert "careers URL" in rows[legacy["id"]]["last_error"]
    assert {b["id"] for b in migrated.boards(active_only=True)} == {explicit["id"], imported["id"]}
    assert migrated.board_listing(legacy["id"]) == listing
    assert Store(tmp_path).boards() == migrated.boards()  # Idempotent across restarts.

    confirmed = save("legacy", "careers-url")
    assert confirmed["id"] == legacy["id"]
    assert confirmed["active"] and confirmed["verified"]
    assert confirmed["last_error"] is None
    assert all(b["active"] and b["verified"] for b in Store(tmp_path).boards())
