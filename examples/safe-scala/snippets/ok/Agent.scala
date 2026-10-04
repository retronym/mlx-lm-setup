import language.experimental.safe
import api.*

def agent()(using IOCapability) =
  requestFileSystem("/project") { fs ?=>
    val secret = readClassified("secrets/api.key")
    val masked = secret.map(s => s.take(4) + "****")
    access("notes.md").read()
  }
