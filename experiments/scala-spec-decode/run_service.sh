#!/usr/bin/env bash
# The compiler service (jvm/Service.scala).
#   run_service.sh [port]            stock 3.10.1-RC1: its jar and dependencies are the classpath
#   run_service.sh [port] 84         retronym/scala3#84 (MultiReaderService): the non-bootstrapped compiler of a #84 worktree,
#                                    built with `sbt scala3-compiler-nonbootstrapped/compile`; SCALA3_84 names the worktree
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
port="${1:-8771}"
jdk=~/.sdkman/candidates/java/21.0.12+1.1-tem
if [[ "${2:-stock}" == 84 ]]; then
  wt="${SCALA3_84:-$HOME/code/.worktrees/scala3/spec-decode-84}"
  cp84=$(cd "$wt" && JAVA_HOME=$jdk timeout 600 sbt -batch -error "export scala3-compiler-nonbootstrapped/Runtime/fullClasspath" | tail -1)
  export CP="$cp84"
  exec timeout "${SERVICE_TIMEOUT:-6h}" scala-cli run -q --server=false --java-home "$jdk" --scala 3.10.1-RC1 \
    --dep com.lihaoyi::ujson:4.4.3 --extra-jars "$cp84" --java-prop service.impl=MultiReaderService --java-opt -Xss8m \
    "$here/jvm/Service.scala" "$here/jvm/Tool.scala" "$here/jvm84/MultiReaderService.scala" --main-class Service -- "$port"
fi
cd "$here/jvm"
export CP=$(coursier fetch org.scala-lang:scala3-compiler_3:3.10.1-RC1 --classpath 2>/dev/null | tail -1)
exec timeout "${SERVICE_TIMEOUT:-6h}" scala-cli run -q --server=false --java-home "$jdk" . --main-class Service -- "$port"
