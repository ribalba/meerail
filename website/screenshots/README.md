# Website screenshots

The images on the landing page are generated, not taken by hand:

```sh
make screenshots                  # seed the demo mailbox, then shoot everything
make screenshots SHOOT_ARGS="--only markdown age"   # just those two
make test-down                    # when finished — the stack is left up
```

Output goes to `website/public/img/screenshots/` at 2880×1800 (1440×900 at 2×),
which is what `index.html` serves.

## Why it works this way

The shots are of the **real app driven in a real browser** against a real
Postgres, with real Tika extraction and real OCR. Nothing is mocked, so a claim
that survives a screenshot is a claim the software actually makes good on — the
OCR shot in particular fails loudly if Tesseract did not read the scan.

Everything runs on the **test stack**, never production. `seed.py` truncates
every table before it writes and refuses outright to start against a database
whose name does not end in `_test`, the same guard `tests/conftest.py` uses.

The demo mailbox is fictional: invented people at `example.com`, invented
invoices. It ends up on a public page, so nothing real belongs in it.

## The pieces

| File | Does |
|---|---|
| `seed.py` | Builds the demo mailbox through `core.ingest` — the same path the agent uses. Also generates the attachments (PDFs via PyMuPDF, the OCR scan and photos via Pillow) so their contents stay in step with what the captions claim. |
| `shoot.py` | Drives Chromium through Playwright, one browser context per view. `VIEWS` maps each name to its capture function. |
| `meerato_stub.py` | A stand-in Meerato, so the Add Task dialog has buckets and statuses to render. Only used by the `task` view. |

Message dates are relative to *now*, so the age-tint gradient looks the same
whenever the shots are regenerated.

## Things that bite

Each of these cost a debugging round and is now handled in code — worth knowing
before you change anything here.

- **Word boundaries do not work in regex search.** Not a harness quirk, a real
  limitation: `app/routers/search.py` validates the pattern with Python's `re`
  and then matches with Postgres `~*`. Python accepts `\b`, where Postgres reads
  it as a backspace, so `\bword\b` silently returns nothing; Postgres's own `\y`
  is rejected by the Python validator before it ever runs. There is no spelling
  that works, so the screenshots use `terminat(e|ion)` instead.
- **The composer eats pre-formatted lists.** Enter is intercepted for list
  continuation, so typing `- item` line by line yields `- - item`. `cap_markdown`
  goes in through the editor's paste path instead, which is the same
  `insertText()` the editor uses internally.
- **The agent looks dead after 120 seconds.** A full run takes longer than that,
  and a stale `last_agent_seen` puts a red "agent not syncing" banner over the
  sidebar. `boot()` re-stamps both accounts before every view.
- **SSE repaints mid-capture.** Every stream event schedules a 500 ms debounced
  re-render of the sidebar and list. `SETTLE` is set above that.
- **Thumbnails are lazy.** `settle_images()` scrolls them into view and waits for
  `naturalWidth > 0`, or the shot catches empty boxes.
- **The Meerato stub has to be reachable from a container.** The server runs in
  Docker, so `localhost` there is not this host; `meerato_stub.gateway()` asks
  Docker for the compose network's gateway address rather than assuming one.

## Adding a view

Write an `async def cap_<name>(page)`, add it to `VIEWS`, and add a tile to the
screenshots section of `website/public/index.html`. Assert something specific
before calling `shot()` — every capture function checks that the thing it is
photographing is actually on screen, which is what stops a silently broken
fixture from shipping as a blank-looking image.

The site's grid is four tiles across, so the count wants to stay a multiple of
four; it is currently eight.
