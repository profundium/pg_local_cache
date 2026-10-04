#!/usr/bin/env python3
"""Copy pg_local_cache RESP values into Valkey in batches."""
import argparse
import os
import socket


def conn(address, port):
    sock = socket.create_connection((address, port))
    return sock, sock.makefile("rb")


def enc(*args):
    out = b"*%d\r\n" % len(args)
    for value in args:
        value = value if isinstance(value, bytes) else str(value).encode()
        out += b"$%d\r\n%s\r\n" % (len(value), value)
    return out


def read(stream):
    line = stream.readline()
    if not line.endswith(b"\r\n"):
        raise RuntimeError(f"invalid RESP line: {line!r}")
    kind, rest = line[:1], line[1:-2]
    if kind in (b"+", b"-", b":"):
        if kind == b"-":
            raise RuntimeError(rest.decode(errors="replace"))
        return rest
    if kind == b"$":
        size = int(rest)
        return None if size < 0 else stream.read(size + 2)[:-2]
    if kind == b"*":
        count = int(rest)
        return None if count < 0 else [read(stream) for _ in range(count)]
    raise RuntimeError(f"invalid RESP response: {line!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", nargs="?", default=os.environ.get("PGLC_DB_ADDR", "192.168.0.4"))
    parser.add_argument("token_file", nargs="?", default=os.environ.get("PGLC_TOKEN_FILE", "/root/pglc.token"))
    args = parser.parse_args()
    key_space = int(os.environ.get("KEY_SPACE", "100000"))
    if key_space < 0:
        parser.error("KEY_SPACE must be nonnegative")
    with open(args.token_file, encoding="utf-8") as token_file:
        token = token_file.read().strip()

    pg, pg_stream = conn(args.address, 6380)
    vk, vk_stream = conn(args.address, 6379)
    try:
        pg.sendall(enc("AUTH", token))
        read(pg_stream)
        vk.sendall(enc("AUTH", token))
        read(vk_stream)
        key = lambda i: 'CRUD:pglc_demo.public.items:{"id":%d}' % i
        copied = 0
        for start in range(1, key_space + 1, 64):
            ids = range(start, min(start + 64, key_space + 1))
            pg.sendall(enc("MGET", *[key(i) for i in ids]))
            values = read(pg_stream)
            commands = []
            for index, value in zip(ids, values):
                if value is not None:
                    commands.append(enc("SET", key(index), value))
            if commands:
                vk.sendall(b"".join(commands))
                for _ in commands:
                    read(vk_stream)
                copied += len(commands)
        vk.sendall(enc("DBSIZE"))
        print("valkey keys:", read(vk_stream).decode(), "copied:", copied)
        vk.sendall(enc("GET", key(min(42, key_space))))
        sample = read(vk_stream)
        print("sample:", sample[:80] if sample else None)
    finally:
        pg_stream.close()
        vk_stream.close()
        pg.close()
        vk.close()


if __name__ == "__main__":
    main()
