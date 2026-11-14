#!/usr/bin/env bash
# Detect build tool and required Java version for a checked-out repo.
# Usage: detect.sh <repo-dir>
# Prints KEY=VALUE lines: TOOL, WRAPPER, JAVA, JAVA_SOURCE
set -uo pipefail
dir="${1:?repo dir}"
cd "$dir" || exit 2

TOOL=""; WRAPPER=no
if [ -f pom.xml ]; then TOOL=maven; [ -x mvnw ] || [ -f mvnw ] && WRAPPER=yes; fi
if [ -z "$TOOL" ]; then
  for f in build.gradle build.gradle.kts settings.gradle settings.gradle.kts; do
    [ -f "$f" ] && { TOOL=gradle; [ -f gradlew ] && WRAPPER=yes; break; }
  done
fi
# A repo with both: honour an explicit override, else prefer Maven (pom.xml wins)
[ -n "${FORCE_TOOL:-}" ] && TOOL="$FORCE_TOOL" && WRAPPER=no && {
  [ "$TOOL" = maven ] && [ -f mvnw ] && WRAPPER=yes
  [ "$TOOL" = gradle ] && [ -f gradlew ] && WRAPPER=yes
}

norm() { # "1.8" -> 8, "17.0.2" -> 17, "21-tem" -> 21
  local v="$1"; v="${v#1.}"; v="${v%%[!0-9]*}"; echo "$v"
}
first_num() { grep -oE "$1" "${@:2}" 2>/dev/null | head -1 | grep -oE '[0-9]+(\.[0-9]+)?' | head -1; }

JAVA=""; SRC=""
try() { # try <source-label> <value>
  [ -n "$JAVA" ] && return 0
  local v; v="$(norm "${2:-}")"
  [ -n "$v" ] && [ "$v" -ge 6 ] 2>/dev/null && JAVA="$v" && SRC="$1"
}

# 1. Explicit version files (tool-agnostic)
[ -f .java-version ] && try ".java-version" "$(head -1 .java-version | tr -d ' \r')"
[ -f .sdkmanrc ] && try ".sdkmanrc" "$(grep -E '^java=' .sdkmanrc | head -1 | cut -d= -f2)"
[ -f .tool-versions ] && try ".tool-versions" "$(grep -E '^java ' .tool-versions | head -1 | awk '{print $2}' | grep -oE '[0-9]+(\.[0-9]+)?' | head -1)"
[ -f .mvn/jdk ] && try ".mvn/jdk" "$(head -1 .mvn/jdk | tr -d ' \r')"

if [ "$TOOL" = maven ]; then
  poms=$(find . -name pom.xml -not -path '*/target/*' -not -path '*/node_modules/*' 2>/dev/null | sort)
  # root pom first, then modules: take the max release any module asks for
  max_of() { grep -ohE "$1" $poms 2>/dev/null | grep -oE '>[0-9.]+<' | tr -d '><' | while read -r x; do norm "$x"; done | sort -n | tail -1; }
  try "maven.compiler.release" "$(max_of '<maven.compiler.release>[0-9.]+</maven.compiler.release>')"
  try "compiler plugin <release>" "$(max_of '<release>[0-9.]+</release>')"
  try "java.version property" "$(max_of '<java.version>[0-9.]+</java.version>')"
  try "maven.compiler.target" "$(max_of '<maven.compiler.target>[0-9.]+</maven.compiler.target>')"
  try "maven.compiler.source" "$(max_of '<maven.compiler.source>[0-9.]+</maven.compiler.source>')"
  try "compiler plugin <target>" "$(max_of '<target>[0-9.]+</target>')"
  [ -f .mvn/toolchains.xml ] && try "toolchains.xml" "$(first_num '<version>[0-9.]+</version>' .mvn/toolchains.xml)"
elif [ "$TOOL" = gradle ]; then
  files=$(find . \( -name '*.gradle' -o -name '*.gradle.kts' -o -name 'gradle.properties' -o -name 'libs.versions.toml' \) -not -path '*/build/*' -not -path '*/.gradle/*' 2>/dev/null)
  max_re() { grep -ohE "$1" $files 2>/dev/null | grep -oE '[0-9]+' | sort -n | tail -1; }
  try "toolchain languageVersion" "$(max_re 'JavaLanguageVersion\.of\(\s*[0-9]+')"
  try "jvmToolchain" "$(max_re 'jvmToolchain\(\s*[0-9]+')"
  try "options.release" "$(max_re 'release(\.set\()?\s*=?\s*\(?[0-9]+')"
  try "JavaVersion.VERSION_x" "$(grep -ohE 'JavaVersion\.VERSION_[0-9_]+' $files 2>/dev/null | sed -E 's/.*VERSION_(1_)?([0-9]+).*/\2/' | sort -n | tail -1)"
  try "sourceCompatibility" "$(grep -ohE "(source|target)Compatibility\s*=\s*['\"]?[0-9.]+" $files 2>/dev/null | grep -oE '[0-9.]+$' | while read -r x; do norm "$x"; done | sort -n | tail -1)"
  try "gradle.properties java.version" "$(grep -ohE '^(java\.?[vV]ersion|javaVersion)\s*=\s*[0-9.]+' $files 2>/dev/null | grep -oE '[0-9.]+$' | head -1)"
fi

[ -z "$JAVA" ] && JAVA="${DEFAULT_JAVA:-17}" && SRC="default (nothing declared)"

# shell-quoted so the orchestrator can eval it
printf 'TOOL=%q\nWRAPPER=%q\nJAVA=%q\nJAVA_SOURCE=%q\n' "${TOOL:-unknown}" "$WRAPPER" "$JAVA" "$SRC"
