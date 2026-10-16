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

New outputs are created under `~/Documents/MarkLV` by default. The user may provide
another directory inside their home folder or the operating system temporary
directory. Existing output files are not overwritten; a unique filename is chosen.
Path checks reuse the application's shared path policy. These checks are not an
OS sandbox and cannot eliminate all filesystem race conditions.

## Microsoft personal account integration

The account choice is Microsoft personal (Outlook.com/Hotmail). This first phase
does **not** claim that Outlook mail, Microsoft Calendar, OneDrive or OneNote APIs
are connected. Those require a Microsoft Entra app registration, delegated
permissions compatible with personal Microsoft accounts, a sign-in/token flow,
and tests against the actual Graph endpoints. They should be implemented as a
separate follow-up phase after local file actions pass CI. Never place a client
secret or access token in source code or audit logs.

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