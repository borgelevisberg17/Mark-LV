# Universal Office & Productivity Agent — implementation status

This work is being introduced incrementally in Mark-LV. It reuses the existing
`core/action_loader.py` registry and `core/path_policy.py` rather than adding a second dispatch
or filesystem authorization layer.

## Implemented in this phase

- `office_documents`: create editable DOCX documents, create a basic reflowable EPUB, inspect DOCX paragraph text, and open a verified local file directly.
- `office_editor`: advanced editing for existing DOCX, XLSX/XLSM and PPTX files, with source-preserving edited copies by default. Supports DOCX run-level text replacement, structured content append, Word TOC field insertion, bibliography sections and cover images; XLSX cell updates, row append and worksheet creation; PPTX shape text replacement and slide append; and structured multi-chapter EPUB creation with navigation and an optional cover image.
- `office_spreadsheets`: create editable XLSX workbooks and inspect existing XLSX sheets.
- `office_presentations`: create editable PPTX presentations from slide titles, body text and bullet lists.
- `image_tools`: inspect image metadata, resize, convert and compress images locally.
- `document_conversion`: export text to PDF with ReportLab, convert DOCX to PDF through LibreOffice or Windows Microsoft Word, extract selectable PDF text with pdfplumber or PyPDF2, and OCR scanned PDFs with PyMuPDF + pytesseract + the separately installed Tesseract engine.
- `office_graph`: delegated Microsoft Graph device-code sign-in for personal Microsoft accounts; read Outlook messages, create calendar events, list OneDrive items, upload/download files, and list OneNote notebooks where the account/API supports it. Uploads larger than 4 MiB use Microsoft Graph upload sessions with chunked transfer and resumable progress saved in the OS credential vault. Sending mail, creating events and uploads request explicit UI confirmation.

New outputs are created under `~/Documents/MarkLV` by default. The user may provide
another directory inside their home folder or the operating system temporary
directory. Existing output files are not overwritten; a unique filename is chosen.
Path checks reuse the application's shared path policy. These checks are not an
OS sandbox and cannot eliminate all filesystem race conditions.

## Microsoft personal account integration

The account choice is Microsoft personal (Outlook.com/Hotmail). Graph support is implemented as an opt-in action, but it cannot authenticate until the user registers a public-client application and supplies its client ID. In Microsoft Entra, configure the app to support personal Microsoft accounts and enable public-client/device-code flow.

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

- The advanced EPUB writer supports multiple chapters, a navigation TOC and optional cover image, but it is not a complete commercial publishing pipeline with EPUBCheck validation, advanced typography or full metadata editing.
- Word TOC insertion creates a field; Microsoft Word (or a compatible editor) must update the field to calculate final page numbers.
- DOCX text replacement intentionally matches within individual runs to avoid flattening rich formatting; it will not replace a phrase split across differently formatted runs.
- The editor validates Office ZIP packages and reopens the generated files, but it does not guarantee preservation of every advanced feature, embedded object, macro or unsupported package extension. For XLSM, VBA is retained where openpyxl supports it; always validate business-critical workbooks in Excel.
- Microsoft Forms general form creation is not represented as a working feature.
- UI automation is not silently substituted for an unsupported API.
- Email sending, event creation, file sharing, deletion and other remote side effects
  require explicit authorization and confirmation for consequential actions.
