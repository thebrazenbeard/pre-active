import argparse

from pre_active.cli import build_parser


def test_cli_exposes_observer_management_surface() -> None:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert "observer" in subparsers.choices
