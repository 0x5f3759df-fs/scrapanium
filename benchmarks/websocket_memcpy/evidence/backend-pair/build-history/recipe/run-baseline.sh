#!/usr/bin/env bash
set -euxo pipefail
ROOT=/home/baidu/scrapanium-experiments/wss-memcpy-backend-pair-20260924
UPSTREAM="$ROOT/upstream"
BUILD="$ROOT/baseline-build"
PREFIX="$ROOT/baseline-install"
mkdir "$BUILD" "$PREFIX"
exec > >(tee -a "$ROOT/baseline-build.log") 2>&1
cd "$UPSTREAM"
printf "start_utc=%s\n" "$(date -u +%FT%TZ)"
printf "upstream_head=%s\n" "$(git rev-parse HEAD)"
printf "upstream_tag=%s\n" "$(git describe --tags --exact-match HEAD)"
printf "upstream_status="; git status --porcelain=v1
sha256sum CMakeLists.txt patches/*.patch .github/workflows/build.yml
/home/baidu/scrapanium-experiments/wss-memcpy-tools/zig-x86_64-linux-0.15.2/zig version
sha256sum /home/baidu/scrapanium-experiments/wss-memcpy-tools/zig-x86_64-linux-0.15.2/zig
cmake --version
ninja --version
make --version | head -n 1
export CC="$ROOT/toolshim/cc"
export CXX="$ROOT/toolshim/cxx"
export AR="$ROOT/toolshim/ar"
export SUBJOBS=4
CMAKE_ARGS="-G Ninja -DCMAKE_INSTALL_PREFIX=$PREFIX -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=x86_64 -DCURL_CA_PATH=/etc/ssl/certs -DCURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt -DSUBJOBS=4"
make prepare-libidn2 BUILD_DIR="$BUILD" JOBS=4
make configure BUILD_DIR="$BUILD" CMAKE_CONFIGURE_ARGS="$CMAKE_ARGS"
make build BUILD_DIR="$BUILD" CMAKE_CONFIGURE_ARGS="$CMAKE_ARGS"
make checkbuild BUILD_DIR="$BUILD" CMAKE_CONFIGURE_ARGS="$CMAKE_ARGS"
make install-strip BUILD_DIR="$BUILD" CMAKE_CONFIGURE_ARGS="$CMAKE_ARGS"
printf "finish_utc=%s\n" "$(date -u +%FT%TZ)"
find "$PREFIX" -type f -printf "%P %s bytes\n" | sort
find "$PREFIX" -type f -print0 | sort -z | xargs -0 sha256sum
