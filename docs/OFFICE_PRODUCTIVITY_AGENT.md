# Universal Office & Productivity Agent — implementation status

This work is being introduced incrementally in Mark-LV. It reuses the existing
`core/action_loader.py` registry and `core/path_policy.py` rather than adding a second dispatch
or filesystem authorization layer.

## Implemented in this phase

- `office_documents`: create editable DOCX documents, create a basic reflowable EPUB, inspect DOCX paragraph text, and open a verified local file directly.
- `office_editor`: advanced editing for existing DOCX, XLSX/XLSM and PPTX files, with source-preserving edited copies by default. DOCX replacement spans differently formatted runs and searches supported body, nested table, header and footer paragraphs; also supports structured content append, Word TOC fields, bibliography sections and cover images. XLSX/XLSM supports literal cell updates, explicit formulas, cell formatting, row append and worksheet create/rename. PPTX supports shape text replacement and slide append. EPUB creation supports chapters, author/language/description metadata, navigation and an optional cover image, with ZIP/XML/manifest/spine/local-link validation.
- `office_spreadsheets`: create editable XLSX workbooks and inspect existing XLSX sheets.
- `office_presentations`: create editable PPTX presentations from slide titles, body text and bullet lists.
- `image_tools`: inspect image metadata, resize, convert and compress images locally.
- `visual_studio`: generate or edit images with the configured Gemini image model, validate and preserve raw outputs, compose exact cover typography, and insert visuals into new DOCX/PPTX copies. Word supports cover-page or inline placement, contextual insertion after a matching paragraph fragment, captions and accessibility alt text. PowerPoint supports a new visual slide, placement on an existing slide, or replacement of a selected picture by zero-based slide/shape indices while retaining the original picture's bounds and z-order. Image generation validates destination files and PowerPoint replacement indices before spending quota. When the authenticated remote dashboard is connected, generated/edited artwork opens in a responsive preview; payloads are capped at 4 MiB and excluded from WebSocket replay history.
- `document_conversion`: export text to PDF with ReportLab, convert DOCX to PDF through LibreOffice or Windows Microsoft Word, extract selectable PDF text with pdfplumber or PyPDF2, and OCR scanned PDFs with PyMuPDF + pytesseract + the separately installed Tesseract engine.
- `office_graph`: delegated Microsoft Graph device-code sign-in for personal or work/school Microsoft accounts (depending on the configured tenant and Entra app registration); manage Outlook events and messages, organize OneDrive items, upload/download files, and list/create OneNote pages where the account/API supports it. Uploads larger than 4 MiB use Microsoft Graph upload sessions with chunked transfer and resumable progress saved in the OS credential vault. Sending mail, creating events and uploads request explicit UI confirmation.

New outputs are created under `~/Documents/MarkLV` by default. The user may provide
another directory inside their home folder or the operating system temporary
directory. Existing output files are not overwritten; a unique filename is chosen.
Path checks reuse the application's shared path policy. These checks are not an
OS sandbox and cannot eliminate all filesystem race conditions.

## Microsoft 365 integration (personal and work/school accounts)

Graph support is implemented as an opt-in action, but it cannot authenticate until the user registers a public-client application and supplies its client ID. In Microsoft Entra, enable public-client/device-code flow and configure supported account types to match the selected tenant audience. Use `common` when the app registration allows both personal and work/school accounts, `consumers` for personal accounts only, or `organizations` for work/school accounts only. The selected tenant audience does not override the account types configured in Entra.

### Local setup on Windows

From the Mark-LV repository root, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_microsoft_graph.ps1
```

When prompted, enter the Application (client) ID. The script validates its GUID format,
checks that `config/microsoft_graph.json` is ignored by Git, and writes the local config
without a UTF-8 BOM. It does not write a client secret or access token. Do not commit this
file. Alternatively, set the `MARKLV_MICROSOFT_CLIENT_ID` environment variable.

The required `requests` and `keyring` dependencies are declared in `requirements.txt`.
Install missing dependencies in the project's active Python environment if needed:

```powershell
python -m pip install requests keyring
```

Ensure Windows Credential Manager or another secure OS credential vault is available.
Launch Mark-LV and use **Setup → Microsoft 365 Sign In**, or call `office_graph` with
`operation=start_login`. Open the verification URL shown by the action, enter the device
code in the Microsoft page, and then check the login in the UI or call `office_graph` with
`operation=poll_login`. A real sign-in is not considered validated until the action reports
success and a read-only `profile` request succeeds.

The implementation requests delegated Graph scopes, stores tokens in the OS credential
vault, refreshes access tokens when possible, and never writes tokens to source or audit
logs. Some account/API features may be unavailable; errors are surfaced instead of
simulated. General Microsoft Forms creation is intentionally unsupported.

## Conversion, OCR and large-upload setup

### DOCX to PDF

Call document_conversion with operation=docx_to_pdf and file_path set to an existing
.docx file. The action first looks for soffice/libreoffice on PATH. On Windows it then
falls back to installed Microsoft Word via COM (using the pywin32 dependency). If neither
converter is installed, it reports the missing prerequisite instead of producing a fake
PDF. Outputs use a unique filename and do not overwrite an existing document.

### OCR for scanned PDFs

Call document_conversion with operation=ocr_pdf, file_path, and optionally
language=eng+por, max_pages=20, save=true, and output_dir. Python packages are listed in
requirements.txt; install the Tesseract OCR application separately and make sure its
executable is on PATH. The action rejects PDFs above 50 MiB, defaults to at most 20 pages,
caps requests at 100 pages, and uses a per-page OCR timeout. OCR quality and supported
languages depend on the installed Tesseract language data. For PDFs that already contain
selectable text, prefer operation=extract_pdf_text.

### Resumable OneDrive uploads

office_graph keeps the simple upload route for files up to 4 MiB. Larger files use Microsoft
Graph upload sessions and 5 MiB chunks. The session URL (a bearer-capability URL) and transfer
metadata are kept in the operating-system credential vault, not source code, logs, or the
assistant response. If transfer pauses, use the returned upload_id with
operation=drive_upload_resume; resuming also requires explicit confirmation. The original
local file must still exist and retain the same size and modification timestamp. Upload
sessions expire on Microsoft's side, so an expired session must be restarted. The destination
is not intentionally overwritten.

## Remaining limitations

- EPUB output receives built-in package and internal-reference validation. If the official [EPUBCheck](https://www.w3.org/publishing/epubcheck/) CLI is installed and available on PATH, the action runs it automatically and rejects a non-zero result or timeout. If it is unavailable, the result explicitly reports structural validation only; built-in checks are not full standards conformance validation and do not provide advanced typesetting or all publishing-platform metadata.
- Word TOC insertion creates a field; Microsoft Word (or a compatible editor) must update the field to calculate final page numbers.
- DOCX replacement spans run boundaries while preserving unaffected run formatting. The replacement text inherits the first matched run's formatting; editing tracked changes, text boxes, footnotes, equations and all other uncommon Word story parts is not guaranteed.
- XLSX/XLSM edits fail closed when chart/drawing/pivot/external-link/embedded package parts are detected, unless the user explicitly authorizes possible feature loss. Formula results are calculated by Excel, not by openpyxl. For XLSM, VBA preservation is supported where openpyxl can retain it, but business-critical workbooks still require Excel validation.
- PPTX shape replacement retains the first paragraph's formatting and removes later paragraphs in that selected shape. Complex chart editing, SmartArt, embedded OLE objects, animations and all theme-level layout changes are not implemented by this action.
- `visual_studio` supports generating and editing visual assets, composing readable cover typography locally, and inserting artwork into source-preserving copies of DOCX, PPTX and EPUB files. EPUB cover insertion adds an image manifest item, an accessible XHTML cover page, EPUB 3 `cover-image` metadata and EPUB 2-compatible cover metadata. This is structural packaging support, not a replacement for EPUBCheck or full commercial publishing QA; validate the final EPUB with EPUBCheck and target-reader apps before release.
- `visual_studio` image generation requires a working Gemini API key in the local configuration and a model that supports the requested image options. Aspect ratio and output size availability vary by model. Cover typography is composed locally and saved as a separate designed image. Contextual Word insertion uses a text fragment to locate the paragraph; ambiguous repeated fragments use the first match. PowerPoint picture replacement targets a zero-based shape index and should be reviewed in PowerPoint, especially on slides with overlapping elements. Word and PowerPoint insertions create new copies; final print/presentation quality still needs human review.
- Microsoft Forms general form creation is not represented as a working feature.
- UI automation is not silently substituted for an unsupported API.
- Email sending, event creation, file sharing, deletion and other remote side effects
  require explicit authorization and confirmation for consequential actions.
