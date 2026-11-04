"""The registry client talks to a registry over http or https, and nothing else."""

from __future__ import annotations

import pytest
from emerge.client import RegistryError, register


@pytest.mark.parametrize(
    "url", ["file:///etc", "ftp://127.0.0.1:9", "data:,x", "localhost:8000"]
)
def test_a_registry_url_that_is_not_http_is_refused(url):
    with pytest.raises(RegistryError, match="http or https"):
        register("name: x\n", registry_url=url)
