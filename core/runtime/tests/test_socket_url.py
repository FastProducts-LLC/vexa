"""Docker endpoint URLs must preserve the configured transport."""
import pytest

from runtime_kernel.docker_backend import _socket_url


@pytest.mark.parametrize(
    ("docker_host", "expected"),
    [
        (None, "http+unix://%2Fvar%2Frun%2Fdocker.sock"),
        ("unix:///run/docker.sock", "http+unix://%2Frun%2Fdocker.sock"),
        ("tcp://docker-proxy:2375", "http://docker-proxy:2375"),
        ("http://docker-proxy:2375", "http://docker-proxy:2375"),
        ("https://h:2376", "https://h:2376"),
        ("tcp://docker-proxy:2375/", "http://docker-proxy:2375"),
    ],
    ids=["unset", "unix", "tcp", "http", "https", "tcp-trailing-slash"],
)
def test_socket_url(monkeypatch, docker_host, expected):
    if docker_host is None:
        monkeypatch.delenv("DOCKER_HOST", raising=False)
    else:
        monkeypatch.setenv("DOCKER_HOST", docker_host)
    assert _socket_url() == expected
