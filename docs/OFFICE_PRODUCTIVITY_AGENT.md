# Universal Office & Productivity Agent — implementation status

This work is being introduced incrementally in Mark-LV. It reuses the existing
`core/action_loader.py` registry and `core/path_policy.py` rather than adding a second dispatch
or filesystem authorization layer.

## Implemented in this phase

- `office_documents`: create editable DOCX documents, create a basic reflowable EPUB, and inspect DOCX paragraph text.
- `office_spreadsheets`: create editable XLSX workbooks and inspect existing XLSX sheets.
- `office_presentations`: create editable PPTX presentations from slide titles, body text and bullet lists.
- `image_tools`: inspect image metadata, resize, convert and compress images locally.
- `document_conversion`: export text to PDF with ReportLab and extract selectable PDF text with pdfplumber or PyPDF2.
- `office_graph`: delegated Microsoft Graph device-code sign-in for personal Microsoft accounts; read Outlook messages, create calendar events, list OneDrive items, upload/download small files, and list OneNote notebooks where the account/API supports it. Sending mail, creating events and uploading files request explicit UI confirmation.

New outputs are created under `~/Documents/MarkLV` by default. The user may provide
another directory inside their home folder or the operating system temporary
directory. Existing output files are not overwritten; a unique filename is chosen.
Path checks reuse the application's shared path policy. These checks are not an
OS sandbox and cannot eliminate all filesystem race conditions.

## Microsoft personal account integration

The account choice is Microsoft personal (Outlook.com/Hotmail). Graph support is implemented as an opt-in action, but it cannot authenticate until the user registers a public-client application and supplies its client ID. In Microsoft Entra, configure the app to support personal Microsoft accounts and enable public-client/device-code flow. Copy config/microsoft_graph.example.json to config/microsoft_graph.json and replace the placeholder, or set MARKLV_MICROSOFT_CLIENT_ID. Install the declared keyring dependency and ensure a secure OS credential vault is available. Call office_graph with start_login, complete the displayed browser code, then call poll_login.

The implementation requests delegated Graph scopes, stores tokens in the OS credential vault, refreshes access tokens when possible, and never writes tokens to source or audit logs. Test against a real personal Microsoft account is still required. Some account/API features may be unavailable; errors are surfaced instead of simulated. General Microsoft Forms creation is intentionally unsupported.

## Explicit limitations

- DOCX-to-PDF conversion is not implemented by this phase.
- PDF OCR for scanned/image-only PDFs is not implemented.
- EPUB generation creates a basic single-chapter ebook; it is not a full publishing
  workflow with advanced typography, cover generation, metadata editor or TOC builder.
- Microsoft Forms general form creation is not represented as a working feature.
- UI automation is not silently substituted for an unsupported API.
- Email sending, event creation, file sharing, deletion and other remote side effects
  are not implemented here; when added, they must use explicit authorization and
  confirmation for consequential actions.