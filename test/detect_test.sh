#!/usr/bin/env bash
# Unit tests for scripts/detect.sh using tiny fixture repos.
set -u
D="$(cd "$(dirname "$0")/.." && pwd)/scripts/detect.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
pass=0; fail=0
check() { # name expected_tool expected_java
  local out; out=$(bash "$D" "$T/$1")
  local tool java; tool=$(echo "$out" | sed -n 's/^TOOL=//p'); java=$(echo "$out" | sed -n 's/^JAVA=//p')
  if [ "$tool" = "$2" ] && [ "$java" = "$3" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL $1: got $tool/$java want $2/$3"; echo "$out" | sed 's/^/   /'; fi
}
mk() { mkdir -p "$T/$1/$(dirname "$2")"; printf '%s\n' "$3" > "$T/$1/$2"; }

mk m-release pom.xml '<project><properties><maven.compiler.release>21</maven.compiler.release></properties></project>'
mk m-source18 pom.xml '<project><properties><maven.compiler.source>1.8</maven.compiler.source><maven.compiler.target>1.8</maven.compiler.target></properties></project>'
mk m-spring pom.xml '<project><properties><java.version>17</java.version></properties></project>'
mk m-plugin pom.xml '<project><build><plugins><plugin><artifactId>maven-compiler-plugin</artifactId><configuration><release>11</release></configuration></plugin></plugins></build></project>'
mk m-modules pom.xml '<project><properties><maven.compiler.release>17</maven.compiler.release></properties></project>'
mk m-modules sub/pom.xml '<project><properties><maven.compiler.release>21</maven.compiler.release></properties></project>'
mk m-none pom.xml '<project></project>'
mk m-javaversion pom.xml '<project><properties><maven.compiler.release>11</maven.compiler.release></properties></project>'
mk m-javaversion .java-version '21.0.4'
mk m-sdkman pom.xml '<project/>'; mk m-sdkman .sdkmanrc 'java=25.0.1-tem'
mk g-toolchain build.gradle 'java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }'
mk g-kts build.gradle.kts 'kotlin { jvmToolchain(17) }'
mk g-compat build.gradle "sourceCompatibility = '11'"
mk g-enum build.gradle.kts 'java { sourceCompatibility = JavaVersion.VERSION_17 }'
mk g-enum18 build.gradle 'sourceCompatibility = JavaVersion.VERSION_1_8'
mk g-release build.gradle 'tasks.withType(JavaCompile) { options.release = 21 }'
mk g-settings-only settings.gradle "rootProject.name='x'"
mk both pom.xml '<project><properties><java.version>17</java.version></properties></project>'; mk both build.gradle 'x'

check m-release maven 21; check m-source18 maven 8; check m-spring maven 17; check m-plugin maven 11
check m-modules maven 21; check m-none maven 17; check m-javaversion maven 21; check m-sdkman maven 25
check g-toolchain gradle 21; check g-kts gradle 17; check g-compat gradle 11; check g-enum gradle 17
check g-enum18 gradle 8; check g-release gradle 21; check g-settings-only gradle 17; check both maven 17
out=$(FORCE_TOOL=gradle bash "$D" "$T/both"); echo "$out" | grep -q TOOL=gradle && pass=$((pass+1)) || { fail=$((fail+1)); echo "FAIL force tool"; }
echo "detect tests: $pass passed, $fail failed"; [ $fail -eq 0 ]
