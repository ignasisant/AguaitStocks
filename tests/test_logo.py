"""Logo domain parsing — pure, no network."""

import pytest

from stocks.data.logo import domain_from_website


@pytest.mark.parametrize(
    ("website", "expected"),
    [
        ("https://www.apple.com", "apple.com"),
        ("http://microsoft.com/", "microsoft.com"),
        ("https://investor.nvidia.com/home", "investor.nvidia.com"),
        ("nvidia.com", "nvidia.com"),
        ("www.tesla.com", "tesla.com"),
        ("", None),
        (None, None),
    ],
)
def test_domain_from_website(website, expected):
    assert domain_from_website(website) == expected
