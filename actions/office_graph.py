"""Microsoft Graph integration for personal Microsoft accounts.

Uses the OAuth 2.0 device-code flow and OS credential storage. A client ID must
be supplied through MARKLV_MICROSOFT_CLIENT_ID or config/microsoft_graph.json.
No client secret is used or accepted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote


from core.confirm import request as request_confirmation
from core.path_policy import PathPolicyError, resolve_user_path, user_accessible_roots
from actions._office_common import safe_filename, safe_output_directory, unique_output_path

_AUTHORITY = "https://login.microsoftonline.com/consumers"
_DEVICE_ENDPOINT = _AUTHORITY + "/oauth2/v2.0/devicecode"
_TOKEN_ENDPOINT = _AUTHORITY + "/oauth2/v2.0/token"
_GRAPH = "https://graph.microsoft.com/v1.0"
_CREDENTIAL_SERVICE = "MarkLV-MicrosoftGraph-Personal"
_CREDENTIAL_USERNAME = "delegated-token-cache"
_PENDING_FLOW: dict | None = None
_MAX_BODY = 12000
_MAX_UPLOAD_BYTES = 4 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 5 * 1024 * 1024  # 16 x 320 KiB, as required by Graph

_ALL_SCOPES = (
    "User.Read", "Calendars.ReadWrite", "Mail.Read", "Mail.Send",
    "Files.ReadWrite", "Notes.ReadWrite", "offline_access",
)
_SCOPES_BY_SET = {
    "all": _ALL_SCOPES,
    "profile": ("User.Read", "offline_access"),
    "calendar": ("Calendars.ReadWrite", "offline_access"),
    "mail_read": ("Mail.Read", "offline_access"),
    "mail_send": ("Mail.Send", "offline_access"),
    "drive": ("Files.ReadWrite", "offline_access"),
    "onenote": ("Notes.ReadWrite", "offline_access"),
}
_REQUIRED = {
    "profile": {"User.Read"},
    "calendar_list": {"Calendars.ReadWrite"},
    "calendar_create": {"Calendars.ReadWrite"},
    "mail_list": {"Mail.Read"},
    "mail_send": {"Mail.Send"},
    "drive_list": {"Files.ReadWrite"},
    "drive_upload": {"Files.ReadWrite"},
    "drive_upload_resume": {"Files.ReadWrite"},
    "drive_download": {"Files.ReadWrite"},
    "onenote_list": {"Notes.ReadWrite"},
}


def _requests():
    try:
        import requests
        return requests
    except ImportError as exc:
        raise RuntimeError("Microsoft Graph requires requests. Install it with: pip install requests") from exc


def _client_id() -> str:
    value = os.environ.get("MARKLV_MICROSOFT_CLIENT_ID", "").strip()
    if value:
        return value
    config_path = Path(__file__).resolve().parent.parent / "config" / "microsoft_graph.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        value = str(data.get("client_id", "")).strip()
        return value
    except (OSError, ValueError, TypeError):
        return ""


def _keyring():
    try:
        import keyring
        backend = keyring.get_keyring()
        if getattr(backend, "priority", 0) <= 0:
            raise RuntimeError("No secure operating-system credential vault is available.")
        return keyring
    except ImportError as exc:
        raise RuntimeError("Secure token storage requires keyring. Install it with: pip install keyring") from exc


# Windows Credential Manager limits each credential blob to 2,560 bytes.
# Microsoft access/refresh tokens and resumable-upload metadata can exceed that
# limit when serialized together. Store bounded chunks under separate entries,
# then atomically switch a small manifest to the new generation.
# keyring's Windows Credential Manager backend stores secret strings as UTF-16LE.
# The native credential blob limit is 2,560 bytes, so 800 ASCII characters use
# at most 1,600 bytes and leave headroom for backend/API implementation details.
_CREDENTIAL_CHUNK_CHARS = 800
_CREDENTIAL_CHUNK_MAX_BYTES = 1600
_CREDENTIAL_MAX_CHUNKS = 10000
_CREDENTIAL_CHUNK_PREFIX = _CREDENTIAL_USERNAME + ":chunk:"


def _load_cache() -> dict:
    keyring = _keyring()
    raw = keyring.get_password(_CREDENTIAL_SERVICE, _CREDENTIAL_USERNAME)
    if not raw:
        return {}
    try:
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            return {}
        if manifest.get("_marklv_chunked") == 1:
            generation = str(manifest.get("generation", ""))
            count = int(manifest.get("chunks", 0))
            if not generation or count < 1 or count > _CREDENTIAL_MAX_CHUNKS:
                return {}
            pieces = []
            for index in range(count):
                piece = keyring.get_password(
                    _CREDENTIAL_SERVICE,
                    f"{_CREDENTIAL_CHUNK_PREFIX}{generation}:{index}",
                )
                if piece is None:
                    return {}
                pieces.append(piece)
            data = json.loads("".join(pieces))
            return data if isinstance(data, dict) else {}
        # Backward compatibility with cache entries written by older versions.
        return manifest
    except (ValueError, TypeError, OverflowError):
        return {}


def _save_cache(cache: dict) -> None:
    keyring = _keyring()
    serialized = json.dumps(cache, separators=(",", ":"), ensure_ascii=True)
    generation = secrets.token_hex(8)
    chunks = [
        serialized[i:i + _CREDENTIAL_CHUNK_CHARS]
        for i in range(0, len(serialized), _CREDENTIAL_CHUNK_CHARS)
    ] or ["{}"]
    if len(chunks) > _CREDENTIAL_MAX_CHUNKS:
        raise RuntimeError(
            "The Microsoft credential cache is too large for safe Windows Credential Manager storage. "
            "Remove completed resumable-upload sessions and try again."
        )
    if any(len(chunk.encode("utf-16-le")) > _CREDENTIAL_CHUNK_MAX_BYTES for chunk in chunks):
        raise RuntimeError("Internal credential chunk exceeds the safe Windows Credential Manager byte limit.")

    previous_manifest = None
    previous_raw = keyring.get_password(_CREDENTIAL_SERVICE, _CREDENTIAL_USERNAME)
    if previous_raw:
        try:
            parsed = json.loads(previous_raw)
            if isinstance(parsed, dict) and parsed.get("_marklv_chunked") == 1:
                previous_manifest = parsed
        except (ValueError, TypeError):
            pass

    # Write all new pieces first. Every UTF-16LE credential blob is explicitly
    # checked against the conservative 1,600-byte ceiling before reaching WinCred.
    # The old manifest remains authoritative until all chunks are safely written.
    try:
        for index, chunk in enumerate(chunks):
            keyring.set_password(
                _CREDENTIAL_SERVICE,
                f"{_CREDENTIAL_CHUNK_PREFIX}{generation}:{index}",
                chunk,
            )

        keyring.set_password(
            _CREDENTIAL_SERVICE,
            _CREDENTIAL_USERNAME,
            json.dumps({
                "_marklv_chunked": 1,
                "generation": generation,
                "chunks": len(chunks),
            }, separators=(",", ":")),
        )
    except Exception as exc:
        # If this is a new generation, remove any partial chunks. Never remove
        # chunks belonging to the currently committed manifest.
        if not previous_manifest or str(previous_manifest.get("generation", "")) != generation:
            for index in range(len(chunks)):
                try:
                    keyring.delete_password(
                        _CREDENTIAL_SERVICE,
                        f"{_CREDENTIAL_CHUNK_PREFIX}{generation}:{index}",
                    )
                except Exception:
                    pass
        raise RuntimeError(
            "Windows could not securely save the Microsoft sign-in credentials. "
            "No token was confirmed as stored; verify Windows Credential Manager "
            "is available and retry sign-in. Original error: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # Only remove the previous generation after the new manifest is committed.
    if previous_manifest:
        old_generation = str(previous_manifest.get("generation", ""))
        old_count = int(previous_manifest.get("chunks", 0))
        if old_generation and old_generation != generation and 0 <= old_count <= _CREDENTIAL_MAX_CHUNKS:
            for index in range(old_count):
                try:
                    keyring.delete_password(
                        _CREDENTIAL_SERVICE,
                        f"{_CREDENTIAL_CHUNK_PREFIX}{old_generation}:{index}",
                    )
                except Exception:
                    pass


def _clear_cache() -> None:
    """Remove the persisted Microsoft session from the OS credential vault."""
    global _PENDING_FLOW
    keyring = _keyring()
    raw = keyring.get_password(_CREDENTIAL_SERVICE, _CREDENTIAL_USERNAME)
    if raw:
        try:
            manifest = json.loads(raw)
        except (ValueError, TypeError):
            manifest = {}
        if isinstance(manifest, dict) and manifest.get("_marklv_chunked") == 1:
            generation = str(manifest.get("generation", ""))
            try:
                count = int(manifest.get("chunks", 0))
            except (TypeError, ValueError, OverflowError):
                count = 0
            if generation and 0 < count <= _CREDENTIAL_MAX_CHUNKS:
                for index in range(count):
                    try:
                        keyring.delete_password(
                            _CREDENTIAL_SERVICE,
                            f"{_CREDENTIAL_CHUNK_PREFIX}{generation}:{index}",
                        )
                    except KeyError:
                        # An already-missing chunk contains nothing to retain.
                        pass
    try:
        keyring.delete_password(_CREDENTIAL_SERVICE, _CREDENTIAL_USERNAME)
    except Exception as exc:
        # A missing entry is already logged out; distinguish it from a vault error.
        if raw:
            raise RuntimeError(f"Could not remove the Microsoft session from the OS credential vault: {exc}") from exc
    _PENDING_FLOW = None


def _start_login(scope_set: str) -> str:
    global _PENDING_FLOW
    # Never create a second OAuth flow for an account that is already connected.
    try:
        session = _account_status()
        if session.get("connected"):
            account = session.get("display_name") or session.get("email") or "Microsoft account"
            return f"Already connected to {account}. Use operation=logout before connecting another account."
    except Exception:
        # A transient status check must not prevent the user from attempting sign-in.
        pass
    client_id = _client_id()
    if not client_id:
        return (
            "Microsoft Graph is not configured. Register a public-client app in Microsoft Entra, "
            "enable device-code/public-client flow, then set MARKLV_MICROSOFT_CLIENT_ID or create "
            "config/microsoft_graph.json from the example file. No account was connected."
        )
    scopes = _SCOPES_BY_SET.get(scope_set, _SCOPES_BY_SET["all"])
    try:
        response = _requests().post(
            _DEVICE_ENDPOINT,
            data={"client_id": client_id, "scope": " ".join(scopes)},
            timeout=15,
        )
        payload = response.json()
        if response.status_code >= 400:
            return f"Microsoft sign-in could not start: {payload.get('error_description', payload.get('error', 'request failed'))}"
        if not payload.get("device_code") or not payload.get("user_code") or not payload.get("verification_uri"):
            return "Microsoft sign-in returned an incomplete device-code response."
        _PENDING_FLOW = {
            "device_code": payload["device_code"],
            "interval": max(2, int(payload.get("interval", 5))),
            "expires_at": time.time() + int(payload.get("expires_in", 900)),
            "scopes": list(scopes),
        }
        return (
            "Microsoft device sign-in started. Open " + str(payload["verification_uri"])
            + " and enter code " + str(payload["user_code"])
            + ". After you finish signing in, call office_graph with operation=poll_login. "
            "The code expires; no token has been stored yet."
        )
    except Exception as exc:
        return f"Microsoft sign-in could not start: {exc}"


def _poll_login() -> str:
    global _PENDING_FLOW
    flow = _PENDING_FLOW
    if not flow:
        return "No device sign-in is pending. Start with operation=start_login."
    if time.time() >= flow["expires_at"]:
        _PENDING_FLOW = None
        return "The device code expired. Start a new login."
    client_id = _client_id()
    try:
        response = _requests().post(
            _TOKEN_ENDPOINT,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": flow["device_code"],
            },
            timeout=15,
        )
        payload = response.json()
        if response.status_code < 400 and payload.get("access_token"):
            granted_scope_text = str(payload.get("scope") or " ".join(flow["scopes"]))
            granted_scopes = sorted({scope.casefold() for scope in granted_scope_text.split()})
            cache = {
                "access_token": payload["access_token"],
                "refresh_token": payload.get("refresh_token", ""),
                "expires_at": time.time() + max(0, int(payload.get("expires_in", 3600)) - 60),
                "scope": granted_scope_text,
                "scopes": granted_scopes,
            }
            try:
                _save_cache(cache)
            except Exception as exc:
                # The device code has already been redeemed. Do not keep polling
                # it or imply that authentication completed successfully.
                # Never fall back to plaintext token storage.
                _PENDING_FLOW = None
                return (
                    "Microsoft authorization succeeded, but Mark-LV could not securely save the tokens "
                    "in the operating-system credential vault. No plaintext fallback was used. "
                    "Correct the credential-storage problem and start a new sign-in. "
                    f"Storage error: {type(exc).__name__}: {exc}"
                )
            _PENDING_FLOW = None
            return "Microsoft personal account connected. Delegated tokens were stored in the operating system credential vault."
        error = payload.get("error", "")
        if error in {"authorization_pending", "slow_down"}:
            return "Sign-in is still pending. Complete the browser step, then call poll_login again."
        if error == "authorization_declined":
            _PENDING_FLOW = None
            return "Sign-in was declined; no account was connected."
        return f"Microsoft sign-in failed: {payload.get('error_description', error or 'unknown error')}"
    except Exception as exc:
        return f"Microsoft sign-in poll failed: {exc}"


def _access_token(required: set[str]) -> tuple[str | None, str]:
    cache = _load_cache()
    scopes = {str(scope).casefold() for scope in cache.get("scopes", [])}
    required_normalized = {scope.casefold() for scope in required}
    if not cache.get("refresh_token") or not required_normalized.issubset(scopes):
        missing = ", ".join(sorted(required_normalized - scopes)) or "Microsoft account sign-in"
        return None, f"Authorization is missing: {missing}. Call office_graph with operation=start_login and scope_set=all."
    if cache.get("access_token") and time.time() < float(cache.get("expires_at", 0)):
        return str(cache["access_token"]), ""
    response = _requests().post(
        _TOKEN_ENDPOINT,
        data={
            "client_id": _client_id(),
            "grant_type": "refresh_token",
            "refresh_token": cache["refresh_token"],
            "scope": " ".join(cache.get("scopes", [])),
        },
        timeout=15,
    )
    payload = response.json()
    if response.status_code >= 400 or not payload.get("access_token"):
        return None, f"Microsoft token refresh failed: {payload.get('error_description', payload.get('error', 'unknown error'))}. Reconnect with start_login."
    cache["access_token"] = payload["access_token"]
    cache["refresh_token"] = payload.get("refresh_token", cache["refresh_token"])
    cache["expires_at"] = time.time() + max(0, int(payload.get("expires_in", 3600)) - 60)
    cache["scope"] = payload.get("scope", cache.get("scope", ""))
    _save_cache(cache)
    return str(cache["access_token"]), ""


def _account_status() -> dict:
    """Validate the saved delegated session and resolve the Microsoft account identity."""
    cache = _load_cache()
    if not cache or not cache.get("refresh_token"):
        return {"connected": False, "display_name": "", "email": "", "message": "No Microsoft account is connected."}
    scopes = {str(scope).casefold() for scope in cache.get("scopes", [])}
    if "user.read" not in scopes:
        return {"connected": False, "display_name": "", "email": "", "message": "The saved session lacks User.Read permission. Sign in again with scope_set=all."}

    token, error = _access_token({"User.Read"})
    if not token:
        return {"connected": False, "display_name": "", "email": "", "message": error}
    profile = _graph("GET", "/me", token)
    name = str(profile.get("displayName") or "Microsoft account").strip()
    email = str(profile.get("mail") or profile.get("userPrincipalName") or "").strip()
    # _access_token may have refreshed and persisted credentials. Reload here so
    # writing profile metadata never overwrites the newly rotated token.
    cache = _load_cache()
    account_id = str(profile.get("id") or "")
    if (
        cache.get("account_name") != name
        or cache.get("account_email") != email
        or cache.get("account_id") != account_id
    ):
        cache["account_name"] = name
        cache["account_email"] = email
        cache["account_id"] = account_id
        _save_cache(cache)
    return {"connected": True, "display_name": name, "email": email, "message": "Microsoft account connected."}


def _graph(method: str, endpoint: str, token: str, *, params=None, json_body=None, data=None, headers=None):
    response = _requests().request(
        method, _GRAPH + endpoint, params=params,
        json=json_body, data=data,
        headers={"Authorization": "Bearer " + token, **(headers or {})},
        timeout=20,
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"Microsoft Graph returned HTTP {response.status_code}: {str(detail)[:500]}")
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _safe_remote_path(raw: str) -> str:
    from pathlib import PurePosixPath, PureWindowsPath
    original = str(raw or "").strip()
    if not original or "\x00" in original:
        raise ValueError("A relative OneDrive path is required.")
    win = PureWindowsPath(original)
    posix = PurePosixPath(original)
    if win.drive or win.is_absolute() or posix.is_absolute() or original.startswith(("\\", "/")):
        raise ValueError("A relative OneDrive path is required.")
    components = original.replace("\\", "/").split("/")
    if original in {".", ".."} or any(part in {"..", "."} for part in components):
        raise ValueError("OneDrive path cannot contain traversal.")
    value = original.replace("\\", "/").rstrip("/")
    if not value:
        raise ValueError("A relative OneDrive path is required.")
    return value



def _upload_sessions() -> dict:
    cache = _load_cache()
    sessions = cache.get("upload_sessions", {})
    return sessions if isinstance(sessions, dict) else {}


def _store_upload_session(upload_id: str, session: dict) -> None:
    cache = _load_cache()
    sessions = cache.get("upload_sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
    sessions[upload_id] = session
    cache["upload_sessions"] = sessions
    _save_cache(cache)


def _delete_upload_session(upload_id: str) -> None:
    cache = _load_cache()
    sessions = cache.get("upload_sessions", {})
    if isinstance(sessions, dict):
        sessions.pop(upload_id, None)
        cache["upload_sessions"] = sessions
        _save_cache(cache)


def _session_offset(session: dict) -> int:
    ranges = session.get("nextExpectedRanges") or ["0-"]
    try:
        return max(0, int(str(ranges[0]).split("-", 1)[0]))
    except (ValueError, TypeError, IndexError):
        return 0


def _upload_session_transfer(upload_id: str, token: str) -> str:
    session = _upload_sessions().get(upload_id)
    if not isinstance(session, dict):
        return "No resumable upload session was found in the OS credential vault. Start drive_upload again."

    source_raw = str(session.get("source_path", ""))
    remote = str(session.get("remote_path", ""))
    try:
        source = resolve_user_path(source_raw, roots=user_accessible_roots(), must_exist=True)
        remote = _safe_remote_path(remote)
    except (OSError, ValueError, PathPolicyError) as exc:
        return f"Cannot resume this upload safely: {exc}"
    if not source.is_file():
        return "The original upload file is no longer available; the upload session was retained."
    stat = source.stat()
    if stat.st_size != int(session.get("size", -1)) or stat.st_mtime_ns != int(session.get("mtime_ns", -1)):
        return "The local file changed since this upload session started. Start a new drive_upload with the current file."
    upload_url = str(session.get("upload_url", ""))
    if not upload_url.startswith("https://"):
        _delete_upload_session(upload_id)
        return "The saved upload session is invalid and was removed. Start drive_upload again."

    requests = _requests()
    try:
        # The upload URL is a pre-authorized capability URL. Do not attach the
        # Microsoft bearer token, log this URL, or return it to the model.
        status_response = requests.get(upload_url, timeout=20)
        if status_response.status_code in (404, 410):
            _delete_upload_session(upload_id)
            return "The OneDrive upload session expired. Start drive_upload again."
        if status_response.status_code >= 400:
            return f"Could not check resumable upload status (HTTP {status_response.status_code}); session retained for retry."
        status_data = status_response.json()
        session["nextExpectedRanges"] = status_data.get("nextExpectedRanges", session.get("nextExpectedRanges", ["0-"]))
        _store_upload_session(upload_id, session)
        offset = _session_offset(session)
        total = source.stat().st_size
        if offset >= total:
            return f"Upload session {upload_id} reports no remaining bytes, but OneDrive did not confirm a completed item. Check OneDrive before starting a new upload."

        with source.open("rb") as stream:
            stream.seek(offset)
            while offset < total:
                chunk = stream.read(min(_UPLOAD_CHUNK_BYTES, total - offset))
                if not chunk:
                    return f"Upload paused unexpectedly at byte {offset}/{total}. Resume with operation=drive_upload_resume and upload_id={upload_id}."
                end = offset + len(chunk) - 1
                try:
                    response = requests.put(
                        upload_url,
                        data=chunk,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end}/{total}",
                        },
                        timeout=(15, 120),
                    )
                except Exception as exc:
                    return f"Upload paused at byte {offset}/{total} ({type(exc).__name__}). Session {upload_id} is retained; resume with operation=drive_upload_resume and upload_id={upload_id}."

                if response.status_code in (200, 201):
                    item = {}
                    try:
                        item = response.json()
                    except ValueError:
                        pass
                    _delete_upload_session(upload_id)
                    return f"Resumable upload completed: {item.get('name', Path(remote).name)} ({total} bytes) to OneDrive path '{remote}'."

                if response.status_code == 202:
                    try:
                        response_data = response.json()
                    except ValueError:
                        response_data = {}
                    session["nextExpectedRanges"] = response_data.get("nextExpectedRanges", [f"{end + 1}-"])
                    _store_upload_session(upload_id, session)
                    offset = _session_offset(session)
                    stream.seek(offset)
                    continue

                if response.status_code in (404, 410):
                    _delete_upload_session(upload_id)
                    return "The OneDrive upload session expired or was cancelled. Start drive_upload again."
                return (
                    f"Upload paused at byte {offset}/{total}; OneDrive returned HTTP {response.status_code}. "
                    f"Session {upload_id} is retained; resume with operation=drive_upload_resume and upload_id={upload_id}."
                )
    except Exception as exc:
        return f"Resumable upload paused safely: {type(exc).__name__}: {str(exc)[:300]}. Session {upload_id} is retained; resume with operation=drive_upload_resume and upload_id={upload_id}."
    return f"Upload did not reach a confirmed completion. Session {upload_id} is retained for resume."


def _start_resumable_upload(source: Path, remote: str, token: str) -> str:
    stat = source.stat()
    upload_id = _digest(str(source.resolve()) + ":" + remote + ":" + str(stat.st_size) + ":" + str(stat.st_mtime_ns))
    existing = _upload_sessions().get(upload_id)
    if isinstance(existing, dict):
        return _upload_session_transfer(upload_id, token)

    endpoint = "/me/drive/root:/" + quote(remote, safe="/") + ":/createUploadSession"
    data = _graph(
        "POST", endpoint, token,
        json_body={"item": {"@microsoft.graph.conflictBehavior": "fail"}},
    )
    upload_url = str(data.get("uploadUrl", ""))
    if not upload_url.startswith("https://"):
        return "Microsoft Graph did not return a valid resumable upload URL."
    session = {
        "source_path": str(source.resolve()),
        "remote_path": remote,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "upload_url": upload_url,
        "expirationDateTime": data.get("expirationDateTime", ""),
        "nextExpectedRanges": ["0-"],
    }
    _store_upload_session(upload_id, session)
    return _upload_session_transfer(upload_id, token)


def office_graph(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    operation = str(params.get("operation", "")).strip().lower()
    if operation == "start_login":
        return _start_login(str(params.get("scope_set", "all")).strip().lower())
    if operation == "poll_login":
        try:
            return _poll_login()
        except Exception as exc:
            return f"Could not complete sign-in: {exc}"
    if operation == "status":
        try:
            cache = _load_cache()
            if not cache:
                return "No Microsoft personal account is connected."
            return "Microsoft personal account token cache is present. Access token expiry: " + time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(float(cache.get("expires_at", 0)))
            )
        except Exception as exc:
            return f"Secure credential vault is unavailable: {exc}"
    if operation == "session_status":
        try:
            return json.dumps(_account_status(), ensure_ascii=False, separators=(",", ":"))
        except Exception as exc:
            return json.dumps({"connected": None, "error": f"Could not verify the Microsoft session: {type(exc).__name__}: {str(exc)[:300]}"}, ensure_ascii=False)
    if operation == "logout":
        try:
            _clear_cache()
            return "Microsoft account disconnected. Saved Microsoft tokens were removed from the operating-system credential vault."
        except Exception as exc:
            return f"Microsoft logout failed: {type(exc).__name__}: {str(exc)[:400]}"

    required = _REQUIRED.get(operation)
    if required is None:
        if operation in {"forms_create", "forms_edit"}:
            return "General Microsoft Forms creation/editing is not exposed by the supported public Microsoft Graph API; this action is intentionally unavailable."
        return "Unsupported operation. Available: status, start_login, poll_login, profile, calendar_list, calendar_create, mail_list, mail_send, drive_list, drive_upload, drive_download, onenote_list."

    try:
        token, error = _access_token(required)
        if not token:
            return error

        if operation == "profile":
            data = _graph("GET", "/me", token)
            return "Microsoft profile: " + json.dumps({k: data.get(k) for k in ("displayName", "mail", "userPrincipalName", "id")}, ensure_ascii=False)

        if operation == "calendar_list":
            start = str(params.get("start", "")).strip()
            end = str(params.get("end", "")).strip()
            if not start or not end or end <= start:
                return "calendar_list requires ISO 8601 start and end values, and end must be later than start."
            data = _graph("GET", "/me/calendarView", token, params={"startDateTime": start, "endDateTime": end, "$top": "50", "$select": "id,subject,start,end,location,isAllDay"})
            events = data.get("value", [])
            return "Calendar events:\n" + "\n".join(
                f"- {e.get('subject') or '(no subject)'} | {e.get('start', {}).get('dateTime')} → {e.get('end', {}).get('dateTime')} | id={e.get('id')}"
                for e in events[:50]
            ) if events else "No calendar events found in that range."

        if operation == "calendar_create":
            subject = str(params.get("subject", "")).strip()
            start = str(params.get("start", "")).strip()
            end = str(params.get("end", "")).strip()
            if not subject or not start or not end or end <= start:
                return "calendar_create requires subject, ISO 8601 start and end values, with end later than start."
            timezone = str(params.get("time_zone", "Africa/Luanda")).strip() or "Africa/Luanda"
            event = {
                "subject": subject,
                "start": {"dateTime": start, "timeZone": timezone},
                "end": {"dateTime": end, "timeZone": timezone},
                "body": {"contentType": "text", "content": str(params.get("body", "") or "")[:4000]},
            }
            key = "office_graph:calendar_create:" + _digest(json.dumps(event, sort_keys=True))
            return request_confirmation(
                key, "Create Outlook calendar event",
                f"Create event '{subject}' from {start} to {end} ({timezone}) in the signed-in personal Microsoft calendar?",
                lambda: "Created Outlook event: " + json.dumps(_graph("POST", "/me/events", token, json_body=event), ensure_ascii=False)[:1500],
                category="network", permissions=("network",),
            )

        if operation == "mail_list":
            top = max(1, min(25, int(params.get("limit", 10))))
            data = _graph("GET", "/me/messages", token, params={"$top": str(top), "$orderby": "receivedDateTime desc", "$select": "id,subject,from,receivedDateTime,isRead,bodyPreview"})
            messages = data.get("value", [])
            return "Recent Outlook messages:\n" + "\n".join(
                f"- {m.get('receivedDateTime')} | {m.get('from', {}).get('emailAddress', {}).get('address', '')} | {m.get('subject') or '(no subject)'} | id={m.get('id')} | preview={str(m.get('bodyPreview') or '')[:160]}"
                for m in messages[:top]
            ) if messages else "No messages returned."

        if operation == "mail_send":
            recipient = str(params.get("to", "")).strip()
            subject = str(params.get("subject", "")).strip()
            body = str(params.get("body", "")).strip()
            if not recipient or "@" not in recipient or not subject or not body:
                return "mail_send requires a valid recipient email, subject and non-empty body."
            if len(body) > 20000:
                return "Email body exceeds the 20,000 character limit."
            message = {"message": {"subject": subject, "body": {"contentType": "Text", "content": body}, "toRecipients": [{"emailAddress": {"address": recipient}}]}, "saveToSentItems": True}
            key = "office_graph:mail_send:" + _digest(json.dumps(message, sort_keys=True))
            return request_confirmation(
                key, "Send Outlook email",
                f"Send email to {recipient} with subject '{subject}'? The message will be sent from the connected personal Microsoft account.",
                lambda: "Microsoft Graph accepted the email for sending." if _graph("POST", "/me/sendMail", token, json_body=message) == {} else "Microsoft Graph accepted the email for sending.",
                category="network", permissions=("network",),
            )

        if operation == "drive_list":
            folder = str(params.get("folder", "") or "").strip().strip("/")
            endpoint = "/me/drive/root/children"
            if folder:
                folder = _safe_remote_path(folder)
                endpoint = "/me/drive/root:/" + quote(folder, safe="/") + ":/children"
            data = _graph("GET", endpoint, token, params={"$top": "50", "$select": "id,name,size,file,folder,lastModifiedDateTime,webUrl"})
            items = data.get("value", [])
            return "OneDrive items:\n" + "\n".join(
                f"- {i.get('name')} | {'folder' if i.get('folder') is not None else 'file'} | {i.get('size', 0)} bytes | id={i.get('id')}"
                for i in items[:50]
            ) if items else "No OneDrive items found."

        if operation == "drive_upload":
            raw = str(params.get("file_path", "") or "").strip()
            remote = str(params.get("remote_path", "") or "").strip().strip("/")
            if not raw or not remote:
                return "drive_upload requires file_path and a safe remote_path relative to the OneDrive root."
            remote = _safe_remote_path(remote)
            source = resolve_user_path(raw, roots=user_accessible_roots(), must_exist=True)
            if not source.is_file():
                return "file_path must be an existing file."
            token_path = "/me/drive/root:/" + quote(remote, safe="/")
            try:
                _graph("GET", token_path + ":", token)
                return "A OneDrive item already exists at that path; upload refused to avoid overwriting it."
            except RuntimeError as exc:
                if "HTTP 404" not in str(exc):
                    raise
            if source.stat().st_size > _MAX_UPLOAD_BYTES:
                stat = source.stat()
                upload_id = _digest(str(source.resolve()) + ":" + remote + ":" + str(stat.st_size) + ":" + str(stat.st_mtime_ns))
                return request_confirmation(
                    "office_graph:drive_upload_resumable:" + upload_id,
                    "Upload large file to OneDrive",
                    f"Create a resumable upload for {source.name} ({stat.st_size} bytes) at OneDrive path '{remote}'? Existing files are not overwritten. If interrupted, the session can be resumed.",
                    lambda: _start_resumable_upload(source, remote, token),
                    category="network", permissions=("network", "filesystem.read"),
                )
            return request_confirmation(
                "office_graph:drive_upload:" + _digest(str(source.resolve()) + ":" + remote),
                "Upload file to OneDrive",
                f"Upload {source.name} ({source.stat().st_size} bytes) to OneDrive path '{remote}'? Existing files are not overwritten.",
                lambda: "Uploaded to OneDrive: " + str(_graph("PUT", token_path + ":/content", token, data=source.read_bytes(), headers={"Content-Type": "application/octet-stream"}).get("name", remote)),
                category="network", permissions=("network", "filesystem.read"),
            )

        if operation == "drive_upload_resume":
            upload_id = str(params.get("upload_id", "") or "").strip()
            if not upload_id or not re.fullmatch(r"[a-f0-9]{24}", upload_id):
                return "drive_upload_resume requires the upload_id returned by an interrupted drive_upload."
            session = _upload_sessions().get(upload_id)
            if not isinstance(session, dict):
                return "No resumable upload session was found. It may have expired or already completed."
            return request_confirmation(
                "office_graph:drive_upload_resume:" + upload_id,
                "Resume OneDrive upload",
                f"Resume uploading {Path(str(session.get('source_path', 'file'))).name} to OneDrive path '{session.get('remote_path', '')}' using saved upload session {upload_id}?",
                lambda: _upload_session_transfer(upload_id, token),
                category="network", permissions=("network", "filesystem.read"),
            )

        if operation == "drive_download":
            remote = str(params.get("remote_path", "") or "").strip().strip("/")
            if not remote:
                return "drive_download requires a safe remote_path relative to the OneDrive root."
            remote = _safe_remote_path(remote)
            endpoint = "/me/drive/root:/" + quote(remote, safe="/") + ":/content"
            response = _requests().get(_GRAPH + endpoint, headers={"Authorization": "Bearer " + token}, timeout=30, stream=True)
            if response.status_code >= 400:
                return f"OneDrive download failed with HTTP {response.status_code}."
            directory = safe_output_directory(str(params.get("output_dir", "") or ""))
            output = unique_output_path(directory, safe_filename(str(params.get("filename", "") or ""), Path(remote).name, Path(remote).suffix or ".bin"))
            total = 0
            with output.open("wb") as stream:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > 25 * 1024 * 1024:
                        stream.close()
                        output.unlink(missing_ok=True)
                        return "Download exceeds the 25 MiB limit; no file was retained."
                    stream.write(chunk)
            return f"Downloaded OneDrive file to {output} ({total} bytes)."

        if operation == "onenote_list":
            data = _graph("GET", "/me/onenote/notebooks", token, params={"$top": "50"})
            notebooks = data.get("value", [])
            return "OneNote notebooks:\n" + "\n".join(f"- {n.get('displayName')} | id={n.get('id')}" for n in notebooks[:50]) if notebooks else "No OneNote notebooks returned."

        return "Unsupported operation."
    except Exception as exc:
        return f"Microsoft Graph operation failed: {type(exc).__name__}: {str(exc)[:700]}"


TOOL = {
    "name": "office_graph",
    "description": "Connect Mark-LV to a personal Microsoft account using OAuth device-code sign-in, then read Outlook messages, create Outlook calendar events, inspect OneDrive, upload/download files (large uploads support resumable sessions), and list OneNote notebooks where Microsoft Graph supports the account. Email sending, event creation and uploads require explicit UI confirmation. Configure a public-client app ID first. General Microsoft Forms creation is not supported.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "description": "status | session_status | start_login | poll_login | logout | profile | calendar_list | calendar_create | mail_list | mail_send | drive_list | drive_upload | drive_upload_resume | drive_download | onenote_list | forms_create"},
            "scope_set": {"type": "STRING", "description": "For start_login: all | profile | calendar | mail_read | mail_send | drive | onenote; defaults to all"},
            "start": {"type": "STRING", "description": "ISO 8601 start datetime for calendar_list/calendar_create"},
            "end": {"type": "STRING", "description": "ISO 8601 end datetime for calendar_list/calendar_create"},
            "time_zone": {"type": "STRING", "description": "Time zone for calendar events; defaults to Africa/Luanda"},
            "subject": {"type": "STRING", "description": "Email subject or calendar event subject"},
            "body": {"type": "STRING", "description": "Email body or event description"},
            "to": {"type": "STRING", "description": "Recipient email address for mail_send"},
            "limit": {"type": "INTEGER", "description": "Maximum number of messages to list, 1-25"},
            "folder": {"type": "STRING", "description": "Optional OneDrive folder path relative to drive root"},
            "file_path": {"type": "STRING", "description": "Existing local file path for drive_upload"},
            "upload_id": {"type": "STRING", "description": "Resume identifier returned by an interrupted large OneDrive upload"},
            "remote_path": {"type": "STRING", "description": "OneDrive path relative to root for upload/download"},
            "output_dir": {"type": "STRING", "description": "Optional local download folder inside home or OS temporary directory"},
            "filename": {"type": "STRING", "description": "Optional local download filename"}
        },
        "required": ["operation"]
    },
    "handler": office_graph
}
