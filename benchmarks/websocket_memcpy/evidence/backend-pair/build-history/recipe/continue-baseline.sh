#!/usr/bin/env bash
set -euxo pipefail
ROOT=/home/baidu/scrapanium-experiments/wss-memcpy-backend-pair-20260924
UPSTREAM="$ROOT/upstream"
BUILD="$ROOT/baseline-build"
PREFIX="$ROOT/baseline-install"
exec >> "$ROOT/baseline-build.log" 2>&1
cd "$UPSTREAM"
printf "recovery_build_start_utc=%s\n" "$(date -u +%FT%TZ)"
export CC="$ROOT/toolshim/cc"
export CXX="$ROOT/toolshim/cxx"
export AR="$ROOT/toolshim/ar"
export SUBJOBS=4
export PKG_CONFIG_LIBDIR="$BUILD/deps/install/lib/pkgconfig"
CMAKE_ARGS="-G Ninja -DCMAKE_INSTALL_PREFIX=$PREFIX -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=x86_64 -DCURL_CA_PATH=/etc/ssl/certs -DCURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt -DSUBJOBS=4"
make build BUILD_DIR="$BUILD" CMAKE_CONFIGURE_ARGS="$CMAKE_ARGS"
make checkbuild BUILD_DIR="$BUILD" CMAKE_CONFIGURE_ARGS="$CMAKE_ARGS"
make install-strip BUILD_DIR="$BUILD" CMAKE_CONFIGURE_ARGS="$CMAKE_ARGS"
printf "recovery_build_finish_utc=%s\n" "$(date -u +%FT%TZ)"
find "$PREFIX" -type f -printf "%P %s bytes\n" | sort
find "$PREFIX" -type f -print0 | sort -z | xargs -0 sha256sum
