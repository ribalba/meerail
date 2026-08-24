# meerail-tika

Apache Tika, plus the two jars it cannot ship itself, plus the settings meerail
needs it to run with. Built by `docker compose build tika` and published as
`ribalba/meerail-tika` — see the top of `Dockerfile` for what is in the image
and why.

`tika-config.json` carries the settings. It has no comments in it, which is not
a style choice: Tika 4 documents `//` and `/* */` as part of the format, and its
top-level loader does accept them, but the pipes config merger that every parse
goes through re-reads the same file with a stricter parser and throws
`Unexpected character ('/')` before the server finishes starting. So the
reasoning lives here instead.

## `server: {}`

Not optional and not a placeholder: a config file with no `server` section at
all is refused with `Couldn't find 'server' element`. Everything in it stays at
its default. In particular `allowPipes` (the `/pipes` and `/async` endpoints)
and `allowPerRequestConfig` (a caller supplying its own parser config) stay off.
Tika has no authentication of its own, and the compose files put it on a network
the rest of the stack shares; nothing there needs either.

## `parsers`

Two knobs, both about when and how a PDF page is OCR'd. The default answer to
"when" is wrong for us in a way that is expensive rather than merely noisy.

**`ocr.strategyAuto`** — with `ocr.strategy=AUTO` (the default, and what we
want) Tika OCRs a page when its text layer looks untrustworthy. "Untrustworthy"
ships as more than *ten* characters on the page that PDFBox could not map to
Unicode. Any PDF built with a subsetted font that carries no usable ToUnicode
table clears that bar on its first line — those are the "No Unicode mapping for
(189) in font ABCDEF+Foo" warnings — even though the text extracts perfectly
well. The result is a full-document OCR of a document that never needed it:
minutes of Tesseract per attachment, on a file whose text was already there.

`unmappedUnicodeCharsPerPage` is read as a *fraction* of the page's characters
when it is below 1, so `0.5` says: OCR a page only when more than half of its
characters are unmapped, i.e. when the text layer really is garbage rather than
merely imperfect. It is the 3.x `ocrStrategyAuto` string `"50%, 10"` in its new
shape.

**`ocr.dpi`** — the resolution a page is rendered at before it goes to
Tesseract. 300 (the default) is scanner quality; 200 is still comfortably above
what Tesseract needs for body text and costs less than half the pixels, which is
both half the OCR time and half the render heap. The heap half matters: a render
that exhausts it kills the forked parser, and the attachment is burned for it
(see `core/mail/tika.py`).

**`default-parser`** — a `parsers` list loads *only* the parsers it names, so
without this entry the server would be a PDF-only Tika. Configuring a parser
above already replaces its default copy, so there is no duplication. It is the
3.x `<parser-exclude>` dance, done for us.

## `parse-context.timeout-limits`

`totalTaskTimeoutMillis` is how long one document may take end to end. Tika 4
defaults it to an hour — far longer than the 300s `core/mail/tika.py` waits,
which would leave the client abandoning requests the server is still working on
and the attachment judged on silence rather than on an answer. 240s puts the
decision back on the server, inside the client's window.

Running out of it is deliberately not an error: with `throwOnDeadline` at its
default the document comes back as a *success with truncated content*, so a
fifty-page scan is indexed as far as it got rather than being burned whole. The
hard watchdog behind that path does not fire until `totalTaskTimeoutMillis` plus
`progressTimeoutMillis` (a further two minutes by default), which is the slack
the client's 300s is sized against.

## `pipes`

4.x always parses in forked JVMs. The 3.x in-process mode is gone — `-noFork` is
not a flag any more, and a compose file still passing it fails at startup with
`Unrecognized option` — and so is the failure that made us ask for it: a heap
exhaustion used to leave the server answering 503 to *every* file until somebody
restarted the container. Now it ends one fork, that document is answered `503
{"status":"OOM"}`, and the replacement fork serves the next request.

**`numClients: 1`** — one fork. This is a memory decision as much as a
concurrency one: left alone Tika derives the pool size from the host's core
count (up to 4) and hands each fork a percentage of the container, which on a
big CI machine is several JVMs each sized as if it were alone in a 2-3GB
container. That is how a container gets OOM-killed whole instead of one document
failing cleanly. One is also all meerail asks for — extraction is one attachment
at a time on the indexer thread (`agent/sync.py`). A second caller, say an mbox
import running alongside the agent, is answered 429, which `core/mail/tika.py`
reads as "come back later" and leaves the queue intact.

**`forkedJvmArgs`** — a percentage rather than an `-Xmx` in bytes, because this
one image runs in containers with different limits (2g under
`docker-compose.test.yml`, 3g under `docker-compose.yml`) and 60% of each is the
heap the single 3.x process had. The parent JVM is given a much smaller slice
via `JAVA_TOOL_OPTIONS` in the compose files: it routes requests and spools
oversized bodies to disk, it does not parse, and the two heaps have to fit the
same container.

`-XX:-ExitOnOutOfMemoryError` switches *off*, for the fork only, the flag the
compose files set for the JVM in general. On the parent that flag is what makes
an unrecoverable heap failure prompt rather than a limp — the container exits
and `restart: unless-stopped` brings it back. In the fork it destroys
information: the process dies before Tika can attribute the failure, and a
document that exhausted the heap comes back as `UNSPECIFIED_CRASH` (which
`core/mail/tika.py` retries once, burning a second full parse to learn what the
first already knew) instead of `OOM` (which it burns immediately). Letting Tika
catch it costs nothing — the fork is discarded either way.
