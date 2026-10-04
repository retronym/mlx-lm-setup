import language.experimental.safe
import api.*

def agent()(using IOCapability) =
  java.nio.file.Files.writeString(java.nio.file.Path.of("exfil.txt"), "key")
