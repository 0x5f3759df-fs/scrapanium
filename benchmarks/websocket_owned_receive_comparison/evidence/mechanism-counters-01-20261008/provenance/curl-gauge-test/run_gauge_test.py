#!/usr/bin/env python3
"""Correctness-only loopback test for owned WSS payload/pin/chunk gauges."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import shlex


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "gauge_client.c"
RUN_LOG = ROOT / "run-02.log"
SUMMARY = ROOT / "run-02-summary.json"
FRAME_SIZES = (53, 79)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pattern(size: int, mul: int, add: int) -> bytes:
    return bytes((i * mul + add) & 0xFF for i in range(size))


def frame(payload: bytes) -> bytes:
    if len(payload) >= 126:
        raise ValueError("test frames intentionally stay in the one-byte length form")
    return bytes((0x82, len(payload))) + payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True, type=Path,
                    help="the profile agent's single diagnostic Curl prefix")
    ap.add_argument("--cc", default="cc")
    args = ap.parse_args()

    prefix = args.prefix.resolve(strict=True)
    include = prefix / "include"
    library_dir = prefix / "lib"
    dso = library_dir / "libcurl-impersonate.so.4.8.0"
    header = include / "curl" / "websockets.h"
    for required in (SOURCE, dso, header):
        if not required.is_file():
            raise SystemExit(f"required test input is missing: {required}")
    if RUN_LOG.exists() or SUMMARY.exists():
        raise SystemExit("refusing to overwrite a prior gauge-test attempt")
    dso_sha_before = sha(dso)
    header_sha = sha(header)
    source_sha = sha(SOURCE)

    compiler = shutil.which(args.cc)
    if not compiler:
        raise SystemExit(f"C compiler was not found: {args.cc}")
    compiler_path = Path(compiler).resolve(strict=True)
    compiler_sha_before = sha(compiler_path)
    compile_command = [
        str(compiler_path), "-std=c11", "-O0", "-g", "-Wall", "-Wextra", "-Werror",
        f"-I{include}", str(SOURCE), str(dso),
        f"-Wl,-rpath,{library_dir}", "-ldl", "-o",
    ]
    env = os.environ.copy()
    ignored_environment = (
        "LD_PRELOAD", "LD_AUDIT", "LD_DEBUG", "LD_PROFILE", "LD_ORIGIN_PATH",
        "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH",
        "LIBRARY_PATH", "LD_RUN_PATH", "GCC_EXEC_PREFIX", "COMPILER_PATH",
        "CFLAGS", "CPPFLAGS", "LDFLAGS",
    )
    for name in ignored_environment:
        env.pop(name, None)
    env["LD_LIBRARY_PATH"] = str(library_dir)
    tmpdir = tempfile.TemporaryDirectory(prefix="curl-owned-gauge-")
    temp = Path(tmpdir.name)
    binary = temp / "gauge_client"
    cert = temp / "peer-cert.pem"
    key = temp / "peer-key.pem"
    compile_command.append(str(binary))

    openssl = shutil.which("openssl")
    if not openssl:
        raise SystemExit("openssl is required to make a temporary loopback peer certificate")
    cert_command = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=127.0.0.1",
        "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,digitalSignature,keyCertSign,keyEncipherment",
    ]

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(8)
    port = server.getsockname()[1]
    thread_errors: list[str] = []
    peer_state = {"combined_response_and_frame_write_completed": False}

    def serve() -> None:
        tls = None
        frame_write_completed = False
        try:
            raw, _ = server.accept()
            raw.settimeout(8)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(str(cert), str(key))
            tls = context.wrap_socket(raw, server_side=True)
            request = bytearray()
            while b"\r\n\r\n" not in request:
                block = tls.recv(2048)
                if not block:
                    raise RuntimeError("client closed before WebSocket request headers")
                request.extend(block)
                if len(request) > 16384:
                    raise RuntimeError("WebSocket request headers exceeded test bound")
            headers: dict[str, str] = {}
            for line in bytes(request).split(b"\r\n")[1:]:
                if not line:
                    break
                if b":" in line:
                    name, value = line.split(b":", 1)
                    headers[name.decode("ascii").strip().lower()] = value.decode("ascii").strip()
            key_value = headers.get("sec-websocket-key")
            if not key_value:
                raise RuntimeError("client omitted Sec-WebSocket-Key")
            accept = base64.b64encode(hashlib.sha1(
                (key_value + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()).decode("ascii")
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
            frame0 = frame(pattern(FRAME_SIZES[0], 17, 3))
            frame1 = frame(pattern(FRAME_SIZES[1], 29, 11))
            tls.sendall(response + frame0 + frame1)
            frame_write_completed = True
            peer_state["combined_response_and_frame_write_completed"] = True
            while tls.recv(4096):
                pass
        except (ConnectionResetError, ssl.SSLError):
            if not frame_write_completed:
                thread_errors.append("TLS/connection failure before combined response/frame write completed")
        except Exception as exc:
            thread_errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            if tls is not None:
                try:
                    tls.close()
                except OSError:
                    pass
            server.close()

    peer = threading.Thread(target=serve, name="loopback-wss-peer", daemon=True)
    peer.start()

    log: list[str] = []
    log.append(f"prefix={prefix}")
    log.append(f"dso={dso}")
    log.append(f"dso_sha256_before={dso_sha_before}")
    log.append(f"websockets_header={header}")
    log.append(f"websockets_header_sha256={header_sha}")
    log.append(f"gauge_client_source_sha256={source_sha}")
    log.append(f"compiler={compiler_path}")
    log.append(f"compiler_sha256_before={compiler_sha_before}")
    log.append("compiler_include_library_and_loader_overrides=cleared")
    log.append(f"ld_library_path={library_dir}")
    log.append("scope=loopback WSS correctness only; no timing or performance claims")
    log.append("temporary self-signed localhost certificate and private key are deleted at exit")
    log.append("compile=" + shlex.join(compile_command))

    status = "failed"
    client_output = ""
    peer_output = ""
    cert_sha = None
    return_code = 0
    try:
        compiler_version = subprocess.run([str(compiler_path), "--version"],
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          text=True, env=env, check=False, timeout=10)
        log.append(f"compiler_version_exit={compiler_version.returncode}")
        if compiler_version.stdout:
            log.append("compiler_version_begin")
            log.extend(compiler_version.stdout.rstrip().splitlines())
            log.append("compiler_version_end")
        if compiler_version.returncode != 0:
            raise RuntimeError("compiler --version failed")
        cert_result = subprocess.run(cert_command, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=env,
                                     check=False, timeout=20)
        if cert_result.returncode != 0:
            raise RuntimeError("openssl certificate generation failed: " +
                               cert_result.stderr[-1000:])
        cert_sha = sha(cert)
        compile_result = subprocess.run(compile_command, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        env=env, check=False)
        log.append(f"compile_exit={compile_result.returncode}")
        if compile_result.stdout:
            log.append("compile_output_begin")
            log.extend(compile_result.stdout.rstrip().splitlines())
            log.append("compile_output_end")
        if compile_result.returncode != 0:
            raise RuntimeError("test client compilation failed")

        url = f"wss://127.0.0.1:{port}/owned-gauge"
        command = [str(binary), url, str(cert), str(dso)]
        log.append("client=" + shlex.join(command))
        child = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, check=False, timeout=30)
        client_output = child.stdout
        return_code = child.returncode
        log.append(f"client_exit={child.returncode}")
        if client_output:
            log.append("client_output_begin")
            log.extend(client_output.rstrip().splitlines())
            log.append("client_output_end")
        peer.join(timeout=10)
        if peer.is_alive():
            raise RuntimeError("loopback peer did not finish after client cleanup")
        if thread_errors:
            peer_output = "; ".join(thread_errors)
            raise RuntimeError("loopback peer failed: " + peer_output)
        if child.returncode != 0:
            raise RuntimeError("gauge client failed")
        status = "pass"
    except Exception as exc:
        return_code = return_code or 1
        log.append(f"driver_error={type(exc).__name__}: {exc}")
    finally:
        try:
            server.close()
        except OSError:
            pass
        peer.join(timeout=1)
        dso_sha_after = sha(dso) if dso.exists() else "missing"
        compiler_sha_after = sha(compiler_path) if compiler_path.exists() else "missing"
        dso_unchanged = dso_sha_after == dso_sha_before
        compiler_unchanged = compiler_sha_after == compiler_sha_before
        log.append(f"dso_sha256_after={dso_sha_after}")
        log.append(f"dso_unchanged={dso_unchanged}")
        log.append(f"compiler_sha256_after={compiler_sha_after}")
        log.append(f"compiler_unchanged={compiler_unchanged}")
        if not dso_unchanged:
            log.append("driver_error=diagnostic DSO changed during the test")
            status = "failed"
            return_code = 1
        if not compiler_unchanged:
            log.append("driver_error=compiler executable changed during the test")
            status = "failed"
            return_code = 1
        log.append(f"temporary_certificate_sha256={cert_sha or 'unavailable'}")
        log.append(f"status={status}")
        RUN_LOG.write_text("\n".join(log) + "\n", encoding="utf-8")
        summary = {
            "status": status,
            "client_exit": return_code,
            "prefix": str(prefix),
            "dso": str(dso),
            "dso_sha256": dso_sha_before,
            "dso_unchanged_after_run": dso_sha_after == dso_sha_before,
            "websockets_header_sha256": header_sha,
            "gauge_client_source_sha256": source_sha,
            "compiler": str(compiler_path),
            "compiler_sha256_before": compiler_sha_before,
            "compiler_sha256_after": compiler_sha_after,
            "compiler_unchanged_after_run": compiler_unchanged,
            "temporary_certificate_sha256": cert_sha,
            "requested_frames": list(FRAME_SIZES),
            "peer_combined_response_and_frame_write_completed":
                peer_state["combined_response_and_frame_write_completed"],
            "peer_error": peer_output or None,
            "performance_claims": False,
            "acceptance_claims": False,
        }
        SUMMARY.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
        tmpdir.cleanup()

    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
