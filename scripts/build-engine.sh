#!/bin/sh
# Builds libocgcore, EDOPro's rules engine, as a shared library.
# Usage: scripts/build-engine.sh [core-dir] [out-dir]
set -eu
CORE=${1:-vendor/ygopro-core}
OUT=${2:-build}
OBJ="$OUT/obj"
mkdir -p "$OBJ"

case "$(uname)" in
	# Pin the SDK to the selected Xcode's: a bare clang may pick a newer Command Line Tools SDK
	# that the linker can't read ("tapi error: malformed file").
	Darwin) LIB=libocgcore.dylib; LINK="-dynamiclib"; export SDKROOT="${SDKROOT:-$(xcrun --sdk macosx --show-sdk-path)}" ;;
	*) LIB=libocgcore.so; LINK="-shared -static-libstdc++ -static-libgcc -Wl,--no-undefined" ;;
esac

# Lua is compiled as C++ like the core does (Lua errors become C++ exceptions),
# minus the Lua libraries the core leaves out.
LUA=$(ls "$CORE"/lua/src/*.c | grep -vE '/(lbitlib|lcorolib|ldblib|linit|loadlib|loslib|ltests|lua|luac|lutf8lib|onelua)\.c$')

# One compiler per source file, in parallel.
export CORE OBJ
ls $LUA "$CORE"/*.cpp | xargs -P "$(getconf _NPROCESSORS_ONLN)" -I{} sh -c '
	${CXX:-c++} -x c++ -std=c++17 -O2 -fPIC -fno-rtti -fvisibility=hidden -w \
		-DOCGCORE_EXPORT_FUNCTIONS -DNDEBUG -I"$CORE/lua/src" -c "{}" -o "$OBJ/$(basename {}).o"'
${CXX:-c++} $LINK -o "$OUT/$LIB" "$OBJ"/*.o
echo "built $OUT/$LIB"
