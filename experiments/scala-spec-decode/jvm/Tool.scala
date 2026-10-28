
// Phase 0b helper on a stock Scala 3 compiler.
//   defs  <file>...   method bodies held out for generation, as JSON lines
//   check <file>...   typecheck each file on its own (to typer) against the classpath; diagnostics as JSON lines
// The classpath (scala3-compiler 3.10.1-RC1 and its dependencies) comes from the CP environment variable.

import dotty.tools.dotc.{Compiler, Driver}
import dotty.tools.dotc.ast.{untpd, Trees}
import dotty.tools.dotc.core.Contexts.*
import dotty.tools.dotc.reporting.{Diagnostic, Reporter}
import dotty.tools.dotc.util.SourceFile
import dotty.tools.dotc.interfaces.Diagnostic.ERROR
import scala.collection.mutable.ListBuffer

class Collect extends Reporter:
  val buf = ListBuffer.empty[Diagnostic]
  def doReport(d: Diagnostic)(using Context): Unit = buf += d

object Tool:
  val scalacOptions = List("-feature", "-deprecation", "-unchecked", "-Yexplicit-nulls", "-encoding", "UTF8", "-Ystop-after:typer")

  def str(s: String): String =
    val sb = StringBuilder("\"")
    s.foreach {
      case '"' => sb ++= "\\\""
      case '\\' => sb ++= "\\\\"
      case '\n' => sb ++= "\\n"
      case c if c < ' ' => sb ++= f"\\u${c.toInt}%04x"
      case c => sb += c
    }
    (sb += '"').toString

  def ctx(stopAfter: String, reporter: Reporter, path: String): Context =
    object driver extends Driver:
      def mk(args: Array[String]) = setup(args, initCtx).get._2
    val args = (path :: "-color:never" :: "-classpath" :: sys.env("CP") :: scalacOptions.map(o => if o.startsWith("-Ystop-after") then s"-Ystop-after:$stopAfter" else o)).toArray
    driver.mk(args).fresh.setReporter(reporter)

  def run(path: String, stopAfter: String): (dotty.tools.dotc.Run, Collect) =
    val rep = Collect()
    given Context = ctx(stopAfter, rep, path)
    val content = String(java.nio.file.Files.readAllBytes(java.nio.file.Path.of(path)), "UTF-8")
    val r = Compiler().newRun
    r.compileSources(List(SourceFile.virtual(path, content)))
    (r, rep)

  def defs(path: String): Unit =
    val rep = Collect()
    given Context = ctx("parser", rep, path)
    val content = String(java.nio.file.Files.readAllBytes(java.nio.file.Path.of(path)), "UTF-8")
    val r = Compiler().newRun
    r.compileSources(List(SourceFile.virtual(path, content)))
    val tree = r.units.head.untpdTree
    // member defs in a template body, with a non-empty rhs
    val acc = new untpd.UntypedTreeTraverser:
      def traverse(t: untpd.Tree)(using Context): Unit = t match
        case tmpl: untpd.Template =>
          tmpl.body.foreach {
            case dd: untpd.DefDef if !dd.rhs.isEmpty && dd.rhs.span.exists && dd.span.exists =>
              val rs = dd.rhs.span
              val eq = content.lastIndexOf('=', rs.start - 1)
              val src = dd.source
              val startLine = src.offsetToLine(dd.span.start)
              val rhsLines = src.offsetToLine(rs.end) - src.offsetToLine(rs.start) + 1
              val line = src.lineContent(dd.span.start)
              val indent = line.takeWhile(c => c == ' ' || c == '\t').length
              println(s"""{"file":${str(path)},"name":${str(dd.name.toString)},"defStart":${dd.span.start},"eq":$eq,"rhsStart":${rs.start},"rhsEnd":${rs.end},"line":$startLine,"rhsLines":$rhsLines,"indent":$indent}""")
            case _ =>
          }
          traverseChildren(t)
        case _ => traverseChildren(t)
    acc.traverse(tree)

  def check(path: String): Unit =
    val t0 = System.nanoTime
    val (_, rep) = run(path, "typer")
    val ms = (System.nanoTime - t0) / 1000000
    val ds = rep.buf.toList.filter(_.level == ERROR).map { d =>
      val id = try d.msg.errorId.toString catch case _: Exception => "?"
      val line = if d.pos.exists then d.pos.line else -1
      val off = if d.pos.exists then d.pos.point else -1
      s"""{"id":${str(id)},"line":$line,"offset":$off,"msg":${str(d.msg.message.linesIterator.take(3).mkString("\n"))}}"""
    }
    println(s"""{"file":${str(path)},"ms":$ms,"errors":[${ds.mkString(",")}]}""")

  // Each source-level selection `qual.name` with its qualifier's (widened) term member names, for the realistic draft ceiling.
  // Member lists are printed once per qualifier type ({"type":k,"members":[...]}), selections refer to them by key.
  def selects(path: String, seen: collection.mutable.Map[String, Int]): Unit =
    import dotty.tools.dotc.ast.tpd
    import dotty.tools.dotc.core.Types.*
    val (r, _) = run(path, "typer")
    given Context = r.runContext
    val unit = r.units.head
    val content = unit.source.content
    val trav = new tpd.TreeTraverser:
      def traverse(t: tpd.Tree)(using Context): Unit =
        t match
          case sel @ tpd.Select(qual, name) if sel.span.exists && !sel.span.isSynthetic && qual.span.exists && !qual.span.isSynthetic && name.isTermName =>
            val s = sel.span.point
            val n = name.toString
            if s + n.length <= content.length && String(content, s, n.length) == n && s > 0 && content(s - 1) == '.' then
              val qt = qual.tpe.widen
              if qt.exists && !qt.isErroneous && !qt.isInstanceOf[MethodicType] then
                val key = qt.show
                val k = seen.getOrElseUpdate(key, {
                  val names = qt.memberNames(takeAllFilter).filter(_.isTermName).map(_.toString).filterNot(_.contains("$")).toList.sorted
                  val id = seen.size
                  println(s"""{"type":$id,"members":[${names.map(str).mkString(",")}]}""")
                  id
                })
                println(s"""{"file":${str(path)},"start":$s,"name":${str(n)},"type":$k}""")
          case _ =>
        traverseChildren(t)
    trav.traverse(unit.tpdTree)

  def main(args: Array[String]): Unit = args.toList match
    case "selects" :: files =>
      val seen = collection.mutable.Map.empty[String, Int]
      files.foreach(f => try selects(f, seen) catch case e: Exception => System.err.println(s"$f: $e"))
    case "defs" :: files => files.foreach(defs)
    case "check" :: files => files.foreach(f => try check(f) catch case e: Exception => println(s"""{"file":${str(f)},"crash":${str(e.toString)}}"""))
    case _ => System.err.println("usage: defs|check <file>...")
