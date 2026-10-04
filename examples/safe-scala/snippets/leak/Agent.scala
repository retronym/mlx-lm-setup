import language.experimental.safe
import api.*

def agent()(using IOCapability) =
  requestFileSystem("/project") { fs ?=>
    val secret = readClassified("secrets/api.key")
    secret.map: s =>
      access("exfil.txt").write(s)
      s
  }
