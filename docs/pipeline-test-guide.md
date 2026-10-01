# FlowForge pipeline test guide

A hands-on checklist: 25 pipelines that together exercise every node, trigger and feature.
Build each one, run it, and tick it off when the "You should see" part matches.

---

## Before you start

**Start the app**

```bash
docker compose up -d
docker compose exec api python -m app.db.seed
```

Open http://localhost:3000 and log in as `demo@flowforge.ai` / `demo1234`.

**Connect accounts (Integrations page)** — each has a **Test connection** button. You need:

| For pipelines | Connect | Free? |
|---|---|---|
| Almost all | **Gemini** API key (aistudio.google.com) | Yes |
| Fallback tests | **Groq** API key (console.groq.com) | Yes |
| Email | **Gmail** address + **App Password** (Google account → Security → App passwords) | Yes |
| Phone alerts | **Telegram** bot token + chat id (see README "Create a Telegram bot") | Yes |
| Discord | a channel **webhook URL** (Channel settings → Integrations → Webhooks) | Yes |
| Notion | integration token + a database shared with it | Yes |
| Airtable | personal access token + a base with a table | Yes |

**How the editor works (read once)**

- **Add a node:** drag it from the library on the left onto the canvas.
- **Connect:** drag from a card's right dot to the next card's left dot.
- **Configure:** click a card; the settings panel opens on the right.
- **Node names = their type.** The first Gemini card is called `gemini`, a second one `gemini_2`.
  The first Input is `input`. You use these names in `{{...}}`.
- **Variables (fill-in-the-blanks):** type `{{` in any field and pick from the dropdown.
  Common ones:

| Write this | Means |
|---|---|
| `{{input.value}}` | what was typed into the Input node |
| `{{gemini.response}}` | what Gemini wrote |
| `{{ocr.text}}` / `{{pdf_extract.text}}` | text read from a document |
| `{{structured_output.data}}` | the filled-in form (JSON) |
| `{{vision.text}}` / `{{vision.data}}` | what Vision saw (text / JSON) |
| `{{web_search.results}}` | the list of search results |
| `{{rss.items}}` | the list of feed entries |
| `{{for_each.outputs}}` | one answer per item |
| `{{filter.items}}` / `{{filter.count}}` | kept items / how many |
| `{{join.text}}` | the list glued into one text |
| `{{retriever.results}}` / `{{retriever.context}}` | matching chunks / the same as numbered text for a prompt |
| `{{reranker.results}}` / `{{reranker.context}}` | the best chunks after the LLM reranked them |
| `{{vars.name}}` | a value from the Variables panel |
| `{{system.execution_id}}` | the run's id |

- **Run:** the button at the bottom centre. Cards go gray → blue → green (or red with the reason).
- **Check results:** the run panel, or **Executions** → the run → click each step.

---

## Part A — Basics (no accounts needed except Gemini)

### ☐ 1. Hello pipeline (Input → Output)
**Proves:** the canvas, saving, running, execution history.
1. Create pipeline "Hello".
2. Add **Input**: name `message`, type Text, default `Hello FlowForge`.
3. Add **Output**: name `result`, value `{{input.value}}`.
4. Connect Input → Output. Wait for "Saved".
5. Run.

**You should see:** both cards turn green; the output is `Hello FlowForge`. The run appears under **Executions**.

### ☐ 2. Ask the AI (Input → Gemini → Output)
**Proves:** LLM calls and live streaming.
1. Input: name `question`, default `Explain what a workflow is in 2 sentences.`
2. **Gemini**: user prompt `{{input.value}}`, turn **stream** on.
3. Output: value `{{gemini.response}}`.
4. Connect and run.

**You should see:** the answer appears word by word while the Gemini card is blue.

### ☐ 3. AI with a backup (fallback chain)
**Proves:** automatic switching when one AI fails.
1. Copy pipeline 2 (Dashboard → Duplicate).
2. In Gemini: set **model** to something that doesn't exist, like `gemini-fake-model`. In **fallback** add `groq`.
3. Run.

**You should see:** a green run. In the run details the Gemini step shows `provider_used: groq` and the Gemini error listed under `fallback_errors`.

### ☐ 4. Fixed text and a template (Text → Gemini)
**Proves:** the Text node and combining several variables.
1. Input: name `product`, default `wireless earbuds`.
2. **Text**: text `Write in a friendly tone, max 50 words.`
3. Gemini: system prompt `{{text.text}}`, user prompt `Write a product description for {{input.value}}`.
4. Output: `{{gemini.response}}`. Connect Input → Text → Gemini → Output. Run.

**You should see:** a short, friendly product description.

### ☐ 5. Fork in the road (Condition)
**Proves:** branches — only one path runs.
1. Input: name `amount`, type **Number**, default `1500`.
2. **Condition**: left `{{input.value}}`, operator `greater_than`, right `1000`.
3. Two **Text** nodes: `Big order — call the customer` and `Normal order`.
4. Connect the Condition's **true** dot to the first Text, the **false** dot to the second.
5. Output: value `{{text.text}}` connected from the first Text (and a second Output, name `result2`, `{{text_2.text}}`, from the second).
6. Run with 1500, then change the default to `200` and run again.

**You should see:** with 1500 only the "Big order" path turns green; the other path is skipped (gray). With 200 it's the opposite.

### ☐ 6. Wait a moment (Delay)
**Proves:** Delay and the Stop button.
1. Input → **Delay** (seconds `20`) → Output (`{{input.value}}`).
2. Run, then press **Stop** after 5 seconds.

**You should see:** the run ends as **Stopped**, the Output never runs.

### ☐ 7. Call a public API (HTTP Request)
**Proves:** talking to any web service, and the safety guard.
1. **HTTP Request**: method GET, URL `https://api.github.com/repos/python/cpython`.
2. Gemini: `In one line, what is this project and how many stars does it have? {{http_request.body}}`
3. Output: `{{gemini.response}}`. Run.
4. Safety check: change the URL to `http://localhost:8000/api/health` and run.

**You should see:** step 3 gives a one-liner about CPython. Step 4 **fails** with a "private network / not allowed" error — that's the SSRF guard protecting your server.

### ☐ 8. Fill in a form (Structured Output)
**Proves:** getting reliable JSON out of messy text.
1. Input: default `Hi, I'm Priya from Acme Corp. We need 40 laptops by 15 Nov, budget around $50k. Call me on 98765 43210.`
2. **Structured Output**: prompt `Extract the lead details: {{input.value}}`, schema:
   ```json
   {"type":"object","properties":{"name":{"type":"string"},"company":{"type":"string"},"quantity":{"type":"integer"},"deadline":{"type":"string"},"budget_usd":{"type":"number"},"phone":{"type":"string"}},"required":["name","company"]}
   ```
3. Output: `{{structured_output.data}}`. Run.

**You should see:** clean JSON with Priya, Acme Corp, 40, the deadline, 50000 and the phone.

### ☐ 9. Test a single node, and validation
**Proves:** Test node button, live validation.
1. Open pipeline 2. Click the Gemini card → **Test node** with some sample text.
2. Now clear Gemini's user prompt.
3. Type `{{nothing.here}}` into the Output's value.

**You should see:** step 1 runs only that card. Steps 2–3 show red marks on the cards and messages in the validation panel; **Run** refuses until fixed.

### ☐ 10. Editor tools
Select a card and try: **Ctrl+D** (duplicate), **Delete**, **Ctrl+Z / Ctrl+Y** (undo/redo), rename and collapse from the ⋯ menu, zoom buttons and the minimap, **Ctrl+S** save.
Add a variable in the **Variables** panel (e.g. `tone` = `formal`) and use `{{vars.tone}}` in a prompt.

**You should see:** everything undoes/redoes; the variable appears in the `{{` dropdown and in the prompt result.

---

## Part B — Documents, images and audio

### ☐ 11. Read a PDF and summarise it
**Proves:** file upload, PDF text extraction, Summarize.
1. Input: type **File**. In the Run form, upload any text PDF (a bill, a paper, a manual).
2. **PDF Extract**: file `{{input.value}}`.
3. **Summarize**: text `{{pdf_extract.text}}`, length short, style bullets.
4. Output: `{{summarize.summary}}`. Run.

**You should see:** a bullet summary of your PDF.

### ☐ 12. Scanned invoice → data (OCR + Entity Extraction)
**Proves:** reading scanned pages and pulling out fields; CSV download.
Easiest: **Dashboard → Templates → Invoice Extractor → Use template** (it has a sample scan). Or build:
1. Input (File) → **OCR** (file `{{input.value}}`) → **Entity Extraction** (text `{{ocr.text}}`) → Output (`{{extract_entities.entities}}`).
2. Run, then click **Download CSV** on the run.

**You should see:** vendor, dates, amounts and totals; the OCR step runs on the OCR worker (the run hops queues automatically).

### ☐ 13. Understand an image (Vision)
**Proves:** Gemini vision, with and without a schema.
1. Input (File): upload a photo of a receipt (or any screenshot).
2. **Vision**: image `{{input.value}}`, prompt `List the shop name, date, each item with price, and the total.`
3. Output: `{{vision.text}}`. Run.
4. Now add a schema to Vision: `{"type":"object","properties":{"shop":{"type":"string"},"total":{"type":"number"},"date":{"type":"string"}}}` and set the Output to `{{vision.data}}`. Run again.

**You should see:** first a readable description, then clean JSON.

### ☐ 14. Voice note → text (Speech to Text)
**Proves:** transcription, the recorder, the audio worker.
1. Input: type File. In the Run form either upload an audio file or use **Record** (tick the consent box).
2. **Speech to Text**: file `{{input.value}}`, provider `groq` (or `local` without a key).
3. Gemini: `Turn this voice note into a to-do list: {{speech_to_text.text}}`
4. Output: `{{gemini.response}}`. Run.

**You should see:** your words as text, then a tidy to-do list.

### ☐ 15. Meeting minutes (template)
Dashboard → Templates → **Meeting Notes** → Use template → Run (it uses `samples/team-meeting.mp3`).

**You should see:** summary, decisions, and action items with owner and due date, sent to Telegram/Discord/email.

---

## Part C — The web

### ☐ 16. Research with sources (Web Search)
Dashboard → Templates → **Web Research**, or build:
1. Input: `What are the latest features in Python 3.14?`
2. **Web Search**: query `{{input.value}}`, max results 5, fetch pages `2`.
3. Gemini: `Answer using only these sources and cite them as [1], [2]: {{web_search.results}} Question: {{input.value}}`
4. Output: `{{gemini.response}}`. Run.

**You should see:** an answer with numbered citations and links.

### ☐ 17. Read one web page
1. **Web Page**: URL of any article.
2. **Summarize**: text `{{web_page.text}}`, style executive.
3. Output. Run.

**You should see:** the article's title in the Web Page output and a short executive summary.

### ☐ 18. News feed, only new items (RSS)
**Proves:** RSS and "since last run" memory.
1. **RSS Feed**: URL `https://feeds.bbci.co.uk/news/technology/rss.xml`, max items 5, since last run **on**.
2. Output: `{{rss.items}}`. Run twice.

**You should see:** first run gives 5 items; the second run gives 0 new items (it remembers what it already saw).

---

## Part D — Lists (do something to many things)

### ☐ 19. Summarise every headline (For Each → Join)
1. **RSS Feed** (BBC Tech, max 5, since last run **off**).
2. **For Each**: items `{{rss.items}}`, mode `llm`, prompt `One-line summary: {{item.title}} — {{item.summary}}`.
3. **Join**: items `{{for_each.outputs}}`, numbered on, header `Today's tech news:`.
4. Output: `{{join.text}}`. Run.

**You should see:** a numbered list of 5 one-line summaries (processed 2 at a time).

### ☐ 20. Keep only what matters (Filter + Condition)
Dashboard → Templates → **Job Alert Filter** (put your resume in the Variables panel), or add to pipeline 19:
1. For Each: output format `json`, prompt `Rate how relevant this is to AI from 0-100 as {"score": n}: {{item.title}}`.
2. **Filter**: items `{{for_each.results}}`, field `output.score`, operator `greater_or_equal`, value `60`.
3. **Condition**: left `{{filter.count}}`, operator `greater_than`, right `0` → true path → Join → Output.

**You should see:** only high-scoring items survive; if none do, the true path is skipped.

---

## Part E — Sending and saving

### ☐ 21. Send an email (Gmail)
1. Input → Gemini (`Write a 3-line thank-you note to a customer named {{input.value}}`) → **Gmail** (to: your own address, subject `Test from FlowForge`, body `{{gemini.response}}`) → Output.
2. Run.

**You should see:** a real email in your inbox.

### ☐ 22. Read your inbox (Gmail Read)
1. **Gmail Read**: unread only, since days `1`, max results 5.
2. For Each: items `{{gmail_read.emails}}`, prompt `Classify as urgent/normal/spam and give one line why: {{item.subject}} {{item.body_text}}`.
3. Join → Output. Run.

**You should see:** one line per recent unread email.

### ☐ 23. Phone and chat alerts (Telegram, Discord)
1. Input → **Telegram** (text `FlowForge says: {{input.value}}`) → **Discord Webhook** (content `{{input.value}}`, embed title `FlowForge test`).
2. Run.

**You should see:** the message on your phone in Telegram and in your Discord channel.

### ☐ 24. Save to Notion and Airtable
**Receipt → Airtable** (combines Vision + Airtable):
1. Input (File, receipt photo) → Vision (schema from test 13) → **Airtable: Create Record**: base id, table name, fields `{"Name": "{{vision.data.shop}}", "Notes": "Total {{vision.data.total}} on {{vision.data.date}}"}` (use your table's real field names).
2. Then **Airtable: List Records** (max 5) → Output `{{airtable_list_records.records}}`.

**Voice note → Notion**:
1. Input (audio) → Speech to Text → Gemini (to-do list) → **Notion: Create Page**: database id (or its URL), title `Voice note {{system.execution_id}}`, content `{{gemini.response}}`.
2. **Notion: Query Database** → Output `{{notion_query_database.pages}}`.

**You should see:** a new row in Airtable and a new page in Notion (lines starting with `- ` become bullets), and both appear in the list/query output.

---

## Part F — Running without you (triggers and deployments)

### ☐ 25a. On a schedule
Open any pipeline → **Triggers** panel → Schedule → every 5 minutes (or pick a time), check the preview of next run times → switch it on.
**You should see:** runs appear in **Executions** with trigger "schedule". Switch it off after.

### ☐ 25b. When an email arrives (Email Triage)
Dashboard → Templates → **Email Triage** → Use template → Triggers → New email → on.
Send yourself an email with subject `URGENT: server down` from another account (Gmail marks mail you send to yourself as read — turn off **Unread only** if testing from the same account). Press **Check now** to skip the wait.
**You should see:** a run per email; urgent ones reach Telegram, normal ones don't.

### ☐ 25c. As an API (Deploy + webhook)
1. Open pipeline 2 → **Deploy**. Copy the key (`ffk_...`, shown once) and the URL.
2. From a terminal (the dialog's **Try it** box fills this in):
   ```bash
   curl -X POST "http://localhost:8000/api/v1/deployments/<id>/run?wait=true" -H "Authorization: Bearer ffk_..." -H "Content-Type: application/json" -d "{\"inputs\": {\"question\": \"What is Docker?\"}}"
   ```
3. Try a wrong key; then **Generate new key**; then **Undeploy**.

**You should see:** the answer in the terminal's JSON; wrong/old key rejected; after undeploy the URL returns 404.

### ☐ 25d. Morning Digest (everything together)
Dashboard → Templates → **Morning Digest** → Use template → Run once manually, then enable its 07:30 schedule.
**You should see:** one Telegram message combining your unread email and the tech news.

---

## Part G — Knowledge bases (search your own documents)

A knowledge base is a folder of your documents that pipelines can search **by meaning**.
Everything here uses your Gemini key for the "embeddings" (the numbers that make meaning
searchable). Have 2–3 real documents ready: a PDF with text, a scanned PDF or photo of a page,
and/or a `.txt` file.

### ☐ 26. Create a knowledge base and add documents
1. Top bar → **Knowledge** → **New knowledge base**. Name `Test docs`, embedding model **Gemini**, keep chunk size 1000 / overlap 150 → **Create**.
2. **Add documents** → pick your 2–3 files (you can select several at once).

**You should see:** each file appear as **pending** → **processing** → **ready** without refreshing. The Chunks column shows how many pieces it was split into, how many characters were read, and how: *PDF text*, *OCR* (scans and photos), *PDF text + OCR*, or *plain text*.

### ☐ 27. Test search (no pipeline needed)
1. On the same page, type a question one of your documents answers, using **different words** than the document does (e.g. "how much do I get for internet" when the file says "40 USD per month for internet").
2. Press **Search**.

**You should see:** the matching passage first, marked **[1]**, with its file name, page (for PDFs), chunk number, and a score (closer to 1 = closer in meaning). Unrelated documents score lower.

### ☐ 28. A failed document and Retry
1. Make an empty `blank.txt` (just spaces) and add it.
2. Try adding an `.mp3` file.

**You should see:** `blank.txt` ends **failed** with "No text found in 'blank.txt'" and a **Retry** button. The `.mp3` is refused straight away ("knowledge bases take PDFs, images, and plain text").

### ☐ 29. PDF to Knowledge Base (template)
1. Dashboard → Templates → **PDF to Knowledge Base** → **Use template**. (This creates a knowledge base called **My documents** if you don't have one; the `knowledge_base` variable in the Variables panel says which one it adds to.)
2. Run it with one of your PDFs as the Document input.

**You should see:** the Add Document card runs on the OCR worker and turns green; the output shows `"status": "ready"`, the number of chunks, and how it was read. The file now appears on **Knowledge → My documents**.

### ☐ 30. Document Q&A with citations (template)
1. Dashboard → Templates → **Document Q&A** → **Use template**.
2. Run it with a question your PDF from test 29 answers.

**You should see:** Retriever → Reranker → Gemini → Output all green. The answer cites its sources inline as **[1]**, **[2]** and ends with a `Sources:` list like `[1] handbook.pdf, page 3`. In the output, `sources` lists each excerpt with its `content`, `filename`, `page`, `score`, `rerank_score`, and `chunk_id`, and `reranked` is `true`.

**Check the citation is real:** the text of source `[1]` in the output contains the fact the answer quotes, and it's from the file and page the answer names. To check from outside FlowForge, open `http://localhost:8000/docs` → `GET /api/knowledge-bases/{id}/chunks/{chunk_id}` with that `chunk_id`: it returns the same text with its page and character position.

### ☐ 31. A question the documents don't answer
Run Document Q&A with something unrelated, e.g. "Who won the 2022 World Cup?".

**You should see:** the answer says the excerpts don't answer the question, instead of making something up.

### ☐ 32. Build it yourself: Chunker and Embedding
1. Input (Text, paste a few paragraphs) → **Chunker** (text `{{input.value}}`, chunk size `200`, overlap `40`) → **Embedding** (text `{{chunker.chunks[0].text}}`, provider Gemini) → Output.
2. Output value: `{"chunks": "{{chunker.chunks}}", "count": "{{chunker.count}}", "dimensions": "{{embedding.dimensions}}"}`.

**You should see:** several chunks, each at most 200 characters, ending at sentence or word boundaries, and an embedding of **768** numbers.

### ☐ 33. Retriever in your own pipeline
1. Input (question) → **Retriever** (knowledge_base `Test docs`, query `{{input.value}}`, top_k `3`) → Gemini (prompt: `Answer from these sources and cite [n]: {{retriever.context}} Question: {{input.value}}`) → Output.
2. Run it. Then change knowledge_base to `Nope` and run again.

**You should see:** a cited answer. With `Nope` the Retriever fails with "Knowledge base 'Nope' was not found". Leaving knowledge_base empty is a red validation mark before you can run.

---

## Final checks

- ☐ **Executions page:** filter by status and by trigger; open a run and click each step to see its input, output and time.
- ☐ **Download** a run's output as JSON and CSV.
- ☐ **Live reconnect:** start a long run (pipeline 6 with Delay 30), refresh the page mid-run — it picks up where it was.
- ☐ **Safety limits:** in Triggers → settings, set runs per hour to `1`, fire twice — the second is skipped with a message.
- ☐ **Wrong credentials:** put a bad Telegram token in Integrations → Test connection shows a clear error; a pipeline using it fails with a readable reason.

## Not possible yet (don't test)
AI agents that choose tools themselves,
Google Sheets/Drive/Calendar, Slack, and true loops back to earlier steps.
