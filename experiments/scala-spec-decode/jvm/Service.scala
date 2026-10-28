// Phase 1: a long-lived compiler service for the decoding loop, on a Scala 3 InteractiveDriver (stock, or retronym/scala3#84).
//
//   POST /complete {"uri", "text", "offset"}  members selectable at `offset`, which sits right after a `.` in `text`
//   POST /members  {"uri", "text", "offset"}  members of the type of the expression that ends at `offset` (no `.` needed)
//   POST /scope    {"uri", "text", "offset"}  at the end of a bare identifier: does it resolve, and every name in scope there
//   POST /check    {"uri", "text"}            errors after typer: [{id, offset, line, msg}], for the typecheck-and-retry baseline
//   GET  /healthz
//
// Answers carry a status: "ok" (the qualifier typed, the list is the truth), "guessing" (error-typed or Dynamic qualifier: callers
// must neither draft nor mask), "none" (not a selection). Requests are served one at a time on one thread, so the driver's Run is
// never shared. Classpath: CP environment variable; port: first argument (default 8771).

import dotty.tools.dotc.ast.tpd
import dotty.tools.dotc.core.Contexts.*
import dotty.tools.dotc.core.Symbols.*
import dotty.tools.dotc.core.Types.*
import dotty.tools.dotc.core.Flags
import dotty.tools.dotc.interactive.{CachedLogicalPackage, Completion, Interactive, InteractiveDriver}
import dotty.tools.dotc.util.SourcePosition
import dotty.tools.dotc.util.Spans.Span

import com.sun.net.httpserver.{HttpExchange, HttpServer}
import java.net.{InetSocketAddress, URI}
import java.util.concurrent.Executors

/** What the decoder needs from a compiler. The stock implementation types the whole buffer per call; the multi-reader
 *  presentation compiler (retronym/scala3#84) can implement the same interface with targeted typing of the edited body. */
trait CompletionService:
  def complete(uri: String, text: String, offset: Int): Completions
  def members(uri: String, text: String, offset: Int): Completions =
    complete(uri, text.substring(0, offset) + "." + text.substring(offset), offset + 1)
  def check(uri: String, text: String): List[Error]
  def scope(uri: String, text: String, offset: Int): Completions

case class Error(id: String, offset: Int, line: Int, msg: String)

case class Member(name: String, kind: String, paramless: Boolean)
case class Completions(status: String, qualifier: String, members: List[Member], typeMs: Long, totalMs: Long):
  def toJson: ujson.Value = ujson.Obj(
    "status" -> status, "qualifier" -> qualifier, "typeMs" -> typeMs.toDouble, "totalMs" -> totalMs.toDouble,
    "members" -> ujson.Arr.from(members.map(m => ujson.Obj("name" -> m.name, "kind" -> m.kind, "paramless" -> m.paramless))))

/** The completion answer at `offset`, from a typed unit (shared by both back ends). */
object Answer:
  def errors(diags: List[dotty.tools.dotc.reporting.Diagnostic])(using Context): List[Error] =
    diags.filter(_.level == dotty.tools.dotc.interfaces.Diagnostic.ERROR).map { d =>
      val id = try d.msg.errorId.toString catch case _: Exception => "?"
      Error(id, if d.pos.exists then d.pos.point else -1, if d.pos.exists then d.pos.line else -1, d.msg.message.linesIterator.take(3).mkString("\n"))
    }

  def apply(unit: dotty.tools.dotc.CompilationUnit, offset: Int, t0: Long, t1: Long)(using c0: Context): Completions =
    given ctx: Context = c0.fresh.setCompilationUnit(unit)          // Completion reads the unit's tree
    val pos = SourcePosition(unit.source, Span(offset))
    val path = Interactive.pathTo(unit.tpdTree, pos.span)
    val qual = path.collectFirst { case tpd.Select(q, _) if q.span.exists && q.span.end <= offset => q }
    def done(status: String, q: String, ms: List[Member]) =
      Completions(status, q, ms, (t1 - t0) / 1000000, (System.nanoTime - t0) / 1000000)
    qual match
      case None => done("none", "", Nil)
      case Some(q) =>
        val qt = q.tpe.widen
        if !qt.exists || qt.isErroneous || qt.derivesFrom(defn.DynamicClass) then done("guessing", qt.show, Nil)
        else
          val (_, items) = Completion.completions(pos)
          val ms = items.filter(_.symbols.nonEmpty).map { c =>
            val sym = c.symbols.head
            val kind =
              if sym.isType then "type"
              else if sym.is(Flags.Module) then "object"
              else if sym.is(Flags.Method) then (if sym.is(Flags.ExtensionMethod) then "extension" else "method")
              else "value"
            val paramless = !sym.is(Flags.Method) || (sym.info.stripPoly match
              case _: MethodType => false
              case _ => true)
            Member(c.label, kind, paramless)
          }.distinctBy(_.name)
          done("ok", qt.show, ms)

  /** At the end of a bare identifier: "ok" if its name is in scope there, "unresolved" if not (then the members are every
   *  term name in scope, for a mask), "none" if `offset` is not at the end of a term identifier. Decided by name lookup (scope
   *  completions with an empty prefix), not by the typed tree: an identifier that fails its expected type is replaced by an
   *  error tree without a symbol even though the name exists. */
  def scope(unit: dotty.tools.dotc.CompilationUnit, offset: Int, t0: Long, t1: Long)(using c0: Context): Completions =
    given ctx: Context = c0.fresh.setCompilationUnit(unit)
    val pos = SourcePosition(unit.source, Span(offset))
    val tpdPath = Interactive.pathTo(unit.tpdTree, pos.span)
    def done(status: String, q: String, ms: List[Member]) =
      Completions(status, q, ms, (t1 - t0) / 1000000, (System.nanoTime - t0) / 1000000)
    val content = unit.source.content
    var start = offset
    while start > 0 && (Character.isJavaIdentifierPart(content(start - 1))) do start -= 1
    val name = String(content, start, offset - start)
    val typedIdent = tpdPath match
      case (id @ tpd.Ident(n)) :: _ if n.toString == name && id.span.exists && id.span.end == offset => Some(id)
      case _ => None
    val typedOk = typedIdent.exists(_.symbol.exists)
    if name.isEmpty || !Character.isJavaIdentifierStart(name.head) then done("none", "", Nil)
    // the typer never reached it (an erroneous enclosing call drops its arguments): its scope is unknown, so no verdict
    else if typedIdent.isEmpty then done("none", name, Nil)
    else if typedOk then done("ok", name, Nil)                    // the common case, without building the scope
    else
      inContext(Interactive.contextOfPath(tpdPath).withPhase(dotty.tools.dotc.core.Phases.typerPhase)):
        val untpdPath = Interactive.resolveTypedOrUntypedPath(tpdPath, pos)
        val mode = Completion.completionMode(untpdPath, pos)
        if !mode.is(Completion.Mode.Scope) || !mode.is(Completion.Mode.Term) then done("none", name, Nil)
        else
          val raw = Completion.rawCompletions(pos, mode, "", tpdPath, untpdPath)
          val terms = raw.toList.flatMap { (n, ds) =>
            ds.find(_.symbol.isTerm).map(d => Member(n.toString, if d.symbol.is(Flags.Method) then "method" else "value", true))
          }
          val rootPackage = defn.RootClass.info.member(dotty.tools.dotc.core.Names.termName(name)).exists   // `dotty.tools...`
          if rootPackage || terms.exists(_.name == name) then done("ok", name, Nil) else done("unresolved", name, terms)

/** Stock 3.10.1-RC1: every request types the whole buffer. */
class StockService(classpath: String, options: List[String]) extends CompletionService:
  private val driver = InteractiveDriver(List("-classpath", classpath, "-color:never") ++ options, CachedLogicalPackage.none)

  def check(uri: String, text: String): List[Error] =
    val diags = driver.run(URI.create(uri), text)
    Answer.errors(diags)(using driver.currentCtx)

  def complete(uri: String, text: String, offset: Int): Completions =
    val t0 = System.nanoTime
    val u = URI.create(uri)
    driver.run(u, text)
    val t1 = System.nanoTime
    Answer(driver.compilationUnits(u), offset, t0, t1)(using driver.currentCtx)

  def scope(uri: String, text: String, offset: Int): Completions =
    val t0 = System.nanoTime
    val u = URI.create(uri)
    driver.run(u, text)
    Answer.scope(driver.compilationUnits(u), offset, t0, System.nanoTime)(using driver.currentCtx)

object Service:
  def main(args: Array[String]): Unit =
    val port = args.headOption.map(_.toInt).getOrElse(8771)
    val options = Tool.scalacOptions.filterNot(_.startsWith("-Ystop-after"))
    // -Dservice.impl=<class> picks another back end, e.g. MultiReaderService (jvm84/, on a retronym/scala3#84 build)
    val svc: CompletionService = sys.props.get("service.impl") match
      case Some(cls) => Class.forName(cls).getConstructor(classOf[String], classOf[List[?]]).newInstance(sys.env("CP"), options).asInstanceOf[CompletionService]
      case None => StockService(sys.env("CP"), options)
    println(s"back end: ${svc.getClass.getSimpleName}")
    val server = HttpServer.create(InetSocketAddress("127.0.0.1", port), 0)
    server.setExecutor(Executors.newSingleThreadExecutor())          // one Run, one reader
    def reply(ex: HttpExchange, code: Int, body: String): Unit =
      val bytes = body.getBytes("UTF-8")
      ex.getResponseHeaders.set("Content-Type", "application/json")
      ex.sendResponseHeaders(code, bytes.length)
      ex.getResponseBody.write(bytes)
      ex.close()
    def endpoint(f: (String, String, Int) => Completions)(ex: HttpExchange): Unit =
      try
        val req = ujson.read(ex.getRequestBody.readAllBytes())
        reply(ex, 200, f(req("uri").str, req("text").str, req("offset").num.toInt).toJson.render())
      catch case e: Throwable =>                                   // an assertion in the compiler must not kill the thread
        e.printStackTrace()
        reply(ex, 500, ujson.Obj("error" -> e.toString).render())
    server.createContext("/complete", ex => endpoint(svc.complete)(ex))
    server.createContext("/members", ex => endpoint(svc.members)(ex))
    server.createContext("/scope", ex => endpoint(svc.scope)(ex))
    server.createContext("/check", ex =>
      try
        val req = ujson.read(ex.getRequestBody.readAllBytes())
        val errs = svc.check(req("uri").str, req("text").str)
        reply(ex, 200, ujson.Obj("errors" -> ujson.Arr.from(errs.map(e => ujson.Obj("id" -> e.id, "offset" -> e.offset, "line" -> e.line, "msg" -> e.msg)))).render())
      catch case e: Throwable =>
        e.printStackTrace()
        reply(ex, 500, ujson.Obj("error" -> e.toString).render()))
    server.createContext("/healthz", ex => reply(ex, 200, """{"status":"ok"}"""))
    server.start()
    println(s"compiler service on 127.0.0.1:$port")
