// The compiler service on retronym/scala3#84: a session keeps one warm Run per file, a request types only the path to its
// target (readUpTo), and with -Yinteractive-body-edits an edit inside the body of a def with a declared type keeps the run
// (only that def is indexed and typed again). Compiled and run against a #84 build only (run_service.sh 84).

import dotty.tools.dotc.core.Contexts.*
import dotty.tools.dotc.interactive.{CachedLogicalPackage, InteractiveDriver}
import dotty.tools.dotc.util.SourceFile
import dotty.tools.dotc.util.Spans.Span
import java.net.URI

class MultiReaderService(classpath: String, options: List[?]) extends CompletionService:
  private val driver = InteractiveDriver(
    List("-classpath", classpath, "-color:never", "-Yinteractive-body-edits") ++ options.map(_.toString)
      ++ sys.env.get("SERVICE_EXTRA_OPTIONS").toList.flatMap(_.split(" ").filter(_.nonEmpty)),   // e.g. -Yinteractive-log
    CachedLogicalPackage.none)

  def check(uri: String, text: String): List[Error] =
    val u = URI.create(uri)
    driver.readAll(u, SourceFile.virtual(u, text))((ctx, _, diags) => Answer.errors(diags)(using ctx))

  def complete(uri: String, text: String, offset: Int): Completions =
    val t0 = System.nanoTime
    val u = URI.create(uri)
    driver.readUpTo(u, SourceFile.virtual(u, text), Span(offset)) { (ctx, unit, _) =>
      Answer(unit, offset, t0, System.nanoTime)(using ctx)
    }

  def scope(uri: String, text: String, offset: Int): Completions =
    val t0 = System.nanoTime
    val u = URI.create(uri)
    driver.readUpTo(u, SourceFile.virtual(u, text), Span(offset)) { (ctx, unit, _) =>
      Answer.scope(unit, offset, t0, System.nanoTime)(using ctx)
    }
