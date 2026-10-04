//> using scala 3.nightly
//> using options -language:experimental.captureChecking
// Stubs with the signatures of TACIT's capability API (lampepfl/tacit, library/Interface.scala), reduced to
// what the film shows. Like TACIT's library, this file is not compiled in safe mode and is marked @assumeSafe.
import caps.*

@assumeSafe abstract class Classified[+T]:
  def map[B](op: T ->{any.rd} B): Classified[B]

@assumeSafe abstract class FileEntry:
  def read(): String
  def write(content: String): Unit

@assumeSafe abstract class FileSystem extends SharedCapability
@assumeSafe class IOCapability extends SharedCapability

@assumeSafe given IOCapability = IOCapability()     // TACIT's REPL preamble provides this given

@assumeSafe object api:
  def requestFileSystem[T](root: String)(op: FileSystem^ ?=> T)(using IOCapability): T = ???
  def access(path: String)(using fs: FileSystem): FileEntry^{fs} = ???
  def readClassified(path: String)(using FileSystem): Classified[String] = ???
