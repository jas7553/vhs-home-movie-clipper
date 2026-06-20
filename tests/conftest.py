import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-slow", action="store_true", default=False, help="run slow integration tests")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-slow"):
        skip = pytest.mark.skip(reason="pass --run-slow to run")
        for item in items:
            if "slow" in item.keywords:
                item.add_marker(skip)
