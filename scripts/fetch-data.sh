#!/bin/sh
# Fetches (or updates) EDOPro's rules engine source, card scripts, card databases and strings.
# Usage: scripts/fetch-data.sh [dir]
set -eu
DIR=${1:-vendor}
mkdir -p "$DIR"

get() {  # get <github repo> <folder> [extra clone flags]
	if [ -d "$DIR/$2/.git" ]; then
		git -C "$DIR/$2" pull -q --ff-only
		git -C "$DIR/$2" submodule -q update --init --depth 1
	else
		git clone -q --depth 1 ${3:-} "https://github.com/$1.git" "$DIR/$2"
	fi
}
get edo9300/ygopro-core ygopro-core "--recurse-submodules --shallow-submodules"
get ProjectIgnis/CardScripts CardScripts
get ProjectIgnis/BabelCDB BabelCDB
curl -fsSL -o "$DIR/strings.conf" https://raw.githubusercontent.com/ProjectIgnis/Distribution/master/config/strings.conf
echo "data in $DIR"
