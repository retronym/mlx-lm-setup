import language.experimental.safe
import api.*

def agent()(using IOCapability) =
  requestFileSystem("/project") { fs ?=>
    val forgotten = fs.asInstanceOf[Any]
  }
