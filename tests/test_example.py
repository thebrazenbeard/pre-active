from pathlib import Path
import runpy

from pre_active.store import Store


def test_minimal_example_closes_store(monkeypatch) -> None:
    closed: list[str] = []
    original_close = Store.close

    def tracked_close(self: Store) -> None:
        closed.append(self.path)
        original_close(self)

    monkeypatch.setattr(Store, "close", tracked_close)
    example = Path(__file__).parents[1] / "examples" / "minimal.py"
    runpy.run_path(str(example), run_name="__main__")
    assert closed, "minimal example must explicitly close its Store"
