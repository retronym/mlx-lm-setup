import language.experimental.safe
import api.*

def agent()(using IOCapability) =
  val notes = requestFileSystem("/project") { fs ?=>
    access("notes.md")
  }
  notes.write("later")
