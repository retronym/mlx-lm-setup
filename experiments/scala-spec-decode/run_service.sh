#!/usr/bin/env bash
# The compiler service (jvm/Service.scala) on a stock 3.10.1-RC1 compiler, its jar and dependencies as the classpath.
set -euo pipefail
cd "$(dirname "$0")/jvm"
export CP=$(coursier fetch org.scala-lang:scala3-compiler_3:3.10.1-RC1 --classpath 2>/dev/null | tail -1)
exec timeout "${SERVICE_TIMEOUT:-6h}" scala-cli run -q --server=false --java-home ~/.sdkman/candidates/java/21.0.12+1.1-tem . \
  --main-class Service -- "${1:-8771}"
