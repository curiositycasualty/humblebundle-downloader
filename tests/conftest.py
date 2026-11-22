"""Shared by every test.

No test may reach the network. Everything that would talk to Humble is
faked (see FakeHumble and FakeSite), and a test that slips past the
fakes should fail loudly rather than quietly depend on Humble being up,
or on whatever account the machine running it is signed in to.
Connections to this machine itself are fine: asyncio and Textual use
them internally.
"""
import socket

import pytest

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


class NetworkUsed(RuntimeError):
    pass


def _local(address):
    if not isinstance(address, tuple):
        return True                   # a unix socket path
    return str(address[0]) in LOCAL_HOSTS


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address):
        if self.family == getattr(socket, "AF_UNIX", None) or _local(address):
            return connect(self, address)
        raise NetworkUsed("a test tried to connect to {0}".format(address))

    def guarded_connect_ex(self, address):
        if self.family == getattr(socket, "AF_UNIX", None) or _local(address):
            return connect_ex(self, address)
        raise NetworkUsed("a test tried to connect to {0}".format(address))

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host is None or str(host) in LOCAL_HOSTS:
            return getaddrinfo(host, *args, **kwargs)
        raise NetworkUsed("a test tried to look up {0}".format(host))

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
