import language.experimental.safe

val redact: String -> String = s => s.take(4) + "****"
