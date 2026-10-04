#!/usr/bin/env python3
"""Black-box checks for native RESP TLS and mTLS."""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import time

import pipeline_integration as resp


CA_FILE = os.environ.get("PG_LOCAL_CACHE_TLS_CA", "")
CLIENT_CERT = os.environ.get("PG_LOCAL_CACHE_TLS_CERT", "")
CLIENT_KEY = os.environ.get("PG_LOCAL_CACHE_TLS_KEY", "")
WRONG_CERT = os.environ.get("PG_LOCAL_CACHE_TLS_WRONG_CERT", "")
WRONG_KEY = os.environ.get("PG_LOCAL_CACHE_TLS_WRONG_KEY", "")
SERVER_CERT = os.environ.get("PG_LOCAL_CACHE_TLS_SERVER_CERT", "")
SERVER_KEY = os.environ.get("PG_LOCAL_CACHE_TLS_SERVER_KEY", "")


def metric(name: str) -> int:
    return int(resp.sql(f"SELECT {name} FROM local_cache.metrics()"))


def wait_for_metric(name: str, baseline: int, timeout: float = 8) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = metric(name)
        if current > baseline:
            return current
        time.sleep(0.05)
    raise AssertionError(f"{name} did not increase from {baseline}")


def context(*, trust_ca: bool = True, cert: str = "", key: str = "") -> ssl.SSLContext:
    result = ssl.create_default_context(cafile=CA_FILE if trust_ca else None)
    if cert:
        result.load_cert_chain(cert, key)
    return result


def connect_tls(
    *,
    ssl_context: ssl.SSLContext | None = None,
    receive_buffer: int | None = None,
) -> ssl.SSLSocket:
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if receive_buffer is not None:
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
    raw.settimeout(10)
    raw.connect((resp.RESP_HOST, resp.RESP_PORT))
    try:
        if ssl_context is None:
            ssl_context = context(cert=CLIENT_CERT, key=CLIENT_KEY)
        return ssl_context.wrap_socket(raw, server_hostname="127.0.0.1")
    except BaseException:
        raw.close()
        raise


def expect_tls_rejection(ssl_context: ssl.SSLContext) -> None:
    sock: ssl.SSLSocket | None = None
    try:
        sock = connect_tls(ssl_context=ssl_context)
        sock.sendall(resp.RespConnection.encode("PING"))
        reply = sock.recv(64)
        assert reply == b"", f"TLS peer returned an unexpected reply: {reply!r}"
    except (ssl.SSLError, ConnectionResetError, EOFError):
        pass
    else:
        raise AssertionError("TLS peer was not rejected")
    finally:
        if sock is not None:
            sock.close()


def check_plaintext_rejected() -> None:
    raw = socket.create_connection((resp.RESP_HOST, resp.RESP_PORT), timeout=5)
    raw.settimeout(5)
    raw.sendall(resp.RespConnection.encode("PING"))
    try:
        reply = raw.recv(64)
        assert reply == b"", f"plaintext listener returned data: {reply!r}"
    except ConnectionResetError:
        pass
    finally:
        raw.close()


def check_auth_ping_mget(table: str) -> None:
    unauthenticated = resp.RespConnection(authenticate=False)
    try:
        assert unauthenticated.command("AUTH", resp.AUTH_TOKEN) == "OK"
        assert unauthenticated.command("PING") == "PONG"
        assert resp.mget_one(unauthenticated, resp.crud_key(table, 1)) == resp.row_bytes(
            1, "x" * 3900
        )
    finally:
        unauthenticated.close()


def check_buffered_pipeline() -> None:
    client = resp.RespConnection()
    # Three echo payloads just under PGLC_VALUE_MAX (8192) in one write span more
    # than one 16 KiB TLS record, so replies depend on buffered TLS plaintext.
    payload = b"buffered" * 1000
    try:
        client.socket.sendall(
            client.encode("PING")
            + client.encode("PING", payload.decode()) * 3
        )
        assert client.read_response() == "PONG"
        for _ in range(3):
            assert client.read_response() == payload
    finally:
        client.close()


def read_simple_response(sock: ssl.SSLSocket, expected: bytes) -> None:
    response = bytearray()
    while not response.endswith(b"\r\n"):
        chunk = sock.recv(64)
        if not chunk:
            raise EOFError("TLS peer closed before its RESP reply")
        response.extend(chunk)
    assert response == b"+" + expected + b"\r\n", response


def tls_auth_ping(sock: ssl.SSLSocket) -> None:
    if resp.AUTH_TOKEN:
        sock.sendall(resp.RespConnection.encode("AUTH", resp.AUTH_TOKEN))
        read_simple_response(sock, b"OK")
    sock.sendall(resp.RespConnection.encode("PING"))
    read_simple_response(sock, b"PONG")


def check_session_resumption(*, mtls: bool) -> None:
    ssl_context = context(
        cert=CLIENT_CERT if mtls else "",
        key=CLIENT_KEY if mtls else "",
    )
    # TLS 1.2 exercises session-ID resumption and the server session context.
    ssl_context.maximum_version = ssl.TLSVersion.TLSv1_2
    previous = connect_tls(ssl_context=ssl_context)
    try:
        tls_auth_ping(previous)
        previous_session = previous.session
        assert previous_session is not None
    finally:
        previous.close()

    raw = socket.create_connection((resp.RESP_HOST, resp.RESP_PORT), timeout=10)
    raw.settimeout(10)
    try:
        resumed = ssl_context.wrap_socket(
            raw,
            server_hostname="127.0.0.1",
            session=previous_session,
        )
    except BaseException:
        raw.close()
        raise
    try:
        assert resumed.session_reused
        tls_auth_ping(resumed)
    finally:
        resumed.close()


def check_large_roundtrips() -> None:
    client = resp.RespConnection(receive_buffer=4096)
    client.socket.settimeout(20)
    payload_size = 8192
    commands_per_round = 7
    try:
        for round_number in range(4):
            payloads = [
                bytes((65 + round_number * commands_per_round + index,))
                * payload_size
                for index in range(commands_per_round)
            ]
            batch = b"".join(
                client.encode("PING", payload) for payload in payloads
            )
            assert len(batch) < 65536
            client.socket.sendall(batch)
            for payload in payloads:
                assert client.read_response() == payload
    finally:
        client.close()


def check_backpressure(table: str) -> None:
    before = metric("output_backpressure_events_total")
    client = resp.RespConnection(receive_buffer=4096)
    count = 800
    # Send the whole pipeline without reading; its replies exceed both the
    # bounded server output buffer and the client's restricted receive window.
    row_ids = [index % 4 + 1 for index in range(count)]
    request = b"".join(
        client.encode("MGET", resp.crud_key(table, row_id)) for row_id in row_ids
    )
    assert len(request) < 65536
    try:
        client.socket.settimeout(30)
        client.socket.sendall(request)
        wait_for_metric("output_backpressure_events_total", before, timeout=20)
        values = {1: "x", 2: "y", 3: "z", 4: "w"}
        expected = [
            [resp.row_bytes(row_id, values[row_id] * 3900)] for row_id in row_ids
        ]
        for response in expected:
            assert client.read_response() == response
    finally:
        client.close()


def check_close_notify() -> None:
    client = connect_tls()
    raw = client.unwrap()
    raw.close()


def check_truncated_close() -> None:
    before = int(resp.sql("SELECT (local_cache.stats()->>'client_disconnect')::bigint"))
    client = resp.RespConnection()
    assert client.command("PING") == "PONG"
    fd = client.socket.detach()
    socket.socket(fileno=fd).close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        after = int(
            resp.sql("SELECT (local_cache.stats()->>'client_disconnect')::bigint")
        )
        if after > before:
            return
        time.sleep(0.05)
    raise AssertionError("truncated TLS client was not disconnected")


def check_stalled_handshake() -> None:
    baseline = metric("tls_handshake_failures_total")
    raw = socket.create_connection((resp.RESP_HOST, resp.RESP_PORT), timeout=5)
    raw.settimeout(5)
    incoming = ssl.MemoryBIO()
    outgoing = ssl.MemoryBIO()
    client = context().wrap_bio(
        incoming, outgoing, server_side=False, server_hostname="127.0.0.1"
    )
    try:
        try:
            client.do_handshake()
        except ssl.SSLWantReadError:
            pass
        hello = outgoing.read()
        assert len(hello) > 2
        raw.sendall(hello[: len(hello) // 2])
        started = time.monotonic()
        try:
            reply = raw.recv(1)
        except ConnectionResetError:
            reply = b""
        assert reply == b"", "stalled TLS handshake was not closed"
        assert time.monotonic() - started >= 0.5, "partial ClientHello closed early"
    except TimeoutError as error:
        raise AssertionError("partial ClientHello exceeded handshake deadline") from error
    finally:
        raw.close()
    wait_for_metric("tls_handshake_failures_total", baseline, timeout=5)


def check_mtls_rejects_missing_and_wrong() -> None:
    if not WRONG_CERT or not WRONG_KEY:
        raise RuntimeError("wrong client certificate fixture is required for --mtls")
    baseline = metric("tls_handshake_failures_total")
    expect_tls_rejection(context())
    expect_tls_rejection(context(cert=WRONG_CERT, key=WRONG_KEY))
    wait_for_metric("tls_handshake_failures_total", baseline)


def check_tls13_floor() -> None:
    ctx = context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    baseline = metric("tls_handshake_failures_total")
    expect_tls_rejection(ctx)
    wait_for_metric("tls_handshake_failures_total", baseline)


def check_bind_health() -> None:
    health = json.loads(resp.sql("SELECT local_cache.health()"))
    assert health["tls_enabled"] is True, health
    assert health["workers_running"] == health["workers_configured"], health
    assert health["workers_running"] > 0, health


def run_full(*, mtls: bool) -> None:
    if not CA_FILE:
        raise RuntimeError("PG_LOCAL_CACHE_TLS_CA is required")
    if mtls and (not CLIENT_CERT or not CLIENT_KEY):
        raise RuntimeError("client certificate and key are required for --mtls")

    failure_baseline = metric("tls_handshake_failures_total")
    handshake_baseline = metric("tls_handshakes_total")
    table = f"tls_{os.getpid()}"
    role = resp.sql("SELECT current_setting('pg_local_cache.role')")
    grants = (
        f"GRANT USAGE ON SCHEMA public TO {resp.sql_identifier(role)};"
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} "
        f"TO {resp.sql_identifier(role)};"
    )
    resp.sql(
        f"CREATE TABLE public.{table} (id bigint PRIMARY KEY, value text NOT NULL);"
        f"INSERT INTO public.{table} VALUES "
        "(1, repeat('x', 3900)), (2, repeat('y', 3900)), "
        "(3, repeat('z', 3900)), (4, repeat('w', 3900));"
        f"{grants}"
        f"SELECT local_cache.attach_table('public.{table}'::regclass, false, '{table}')"
    )
    try:
        client = resp.RespConnection()
        try:
            assert resp.wait_for_mapping(
                client, resp.crud_key(table, 1)
            ) == resp.row_bytes(1, "x" * 3900)
        finally:
            client.close()

        check_plaintext_rejected()
        expect_tls_rejection(context(trust_ca=False))
        wait_for_metric("tls_handshake_failures_total", failure_baseline)
        check_auth_ping_mget(table)
        check_buffered_pipeline()
        check_backpressure(table)
        check_large_roundtrips()
        check_session_resumption(mtls=mtls)
        check_close_notify()
        check_truncated_close()

        if mtls:
            check_mtls_rejects_missing_and_wrong()
            valid = resp.RespConnection()
            try:
                assert valid.command("PING") == "PONG"
                assert resp.mget_one(valid, resp.crud_key(table, 1)) == resp.row_bytes(
                    1, "x" * 3900
                )
            finally:
                valid.close()
        assert metric("tls_handshakes_total") > handshake_baseline
        print("TLS integration passed: auth, RESP, retry, close-notify, truncation, and backpressure")
    finally:
        resp.sql(
            f"SELECT local_cache.detach_table('public.{table}'::regclass);"
            f"DROP TABLE IF EXISTS public.{table}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mtls", action="store_true")
    parser.add_argument("--handshake-timeout", action="store_true")
    parser.add_argument("--protocol-floor", action="store_true")
    parser.add_argument("--bind-health", action="store_true")
    args = parser.parse_args()
    if sum((args.mtls, args.handshake_timeout, args.protocol_floor,
            args.bind_health)) > 1:
        parser.error("select at most one specialized check")
    if args.handshake_timeout:
        check_stalled_handshake()
    elif args.protocol_floor:
        check_tls13_floor()
    elif args.bind_health:
        check_bind_health()
    else:
        run_full(mtls=args.mtls)


if __name__ == "__main__":
    main()
