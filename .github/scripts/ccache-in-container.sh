#!/usr/bin/env bash
# Set up ccache INSIDE a Linux build container so llama.cpp's GGML_CCACHE (ON by default) auto-detects it.
# On Linux the Ninja generator prefixes every compile — including nvcc — via RULE_LAUNCH_COMPILE, so a warm
# ccache turns the multi-arch CUDA rebuild from ~45 min into a few minutes. CCACHE_DIR is a host-mounted,
# actions/cache-persisted directory, so the cache survives across CI runs.
#
# SOURCE this (don't exec) from the build container's script, AFTER apt has installed curl + xz-utils:
#     source /bob/.github/scripts/ccache-in-container.sh
# It is deliberately best-effort: any failure just means "build without a cache" (correct, only slower), never
# a failed build — GGML_CCACHE simply warns when ccache is absent.
#
# The Ubuntu 20.04 apt ccache is 3.7.x with weak nvcc support, so we fetch a modern static 4.x binary (full
# CUDA support, runs on any glibc). Pin the version for reproducibility.

_CCACHE_VER=4.10.2

_setup_ccache() {
  local arch url
  arch="$(uname -m)"   # x86_64 on the CI runners
  export CCACHE_DIR="${CCACHE_DIR:-/ccache}"
  export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-5G}"
  export CCACHE_COMPILERCHECK=content   # hash the compiler by content — robust across toolkit/container bumps
  mkdir -p "$CCACHE_DIR" 2>/dev/null || true

  if ! command -v ccache >/dev/null 2>&1; then
    url="https://github.com/ccache/ccache/releases/download/v${_CCACHE_VER}/ccache-${_CCACHE_VER}-linux-${arch}.tar.xz"
    if curl -fsSL -o /tmp/ccache.tar.xz "$url" && tar -C /tmp -xf /tmp/ccache.tar.xz; then
      cp "/tmp/ccache-${_CCACHE_VER}-linux-${arch}/ccache" /usr/local/bin/ccache && chmod +x /usr/local/bin/ccache
    fi
  fi

  if command -v ccache >/dev/null 2>&1; then
    ccache -z >/dev/null 2>&1 || true   # zero this-run stats so the post-build `ccache -sv` shows the hit rate
    echo "ccache ready: $(ccache --version | head -1)  dir=$CCACHE_DIR maxsize=$CCACHE_MAXSIZE"
  else
    echo "ccache unavailable (download failed?) — building without a compile cache (slower, still correct)."
  fi
}

_setup_ccache
