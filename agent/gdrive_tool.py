"""Google Drive / Docs source — no consent screen, per-user ACL trim.

Same three-callable contract as github_tool (search / fetch / inventory), so
agent.py's SOURCES table takes it as one more row.

WHY THIS SOURCE LOOKS DIFFERENT FROM GITHUB. GitHub is reached with a token the
USER granted (3-legged consent, vaulted by AgentCore Identity). Drive is reached by
DOMAIN-WIDE DELEGATION: a Google service account impersonates the user, and no
consent screen is ever shown. That is the outcome this project wants — but it means
the callables here take a `subject` (the verified user email) instead of an
already-user-scoped OAuth token, so they do not share github_tool's signature and
agent.py adapts them at the SOURCES boundary.

WHY NOT OKTA TOKEN PROPAGATION. Drive is guarded by Google, permanently. Google's
`jwt-bearer` grant requires the assertion be signed by a Google SERVICE ACCOUNT key
(`iss` = the service account); an Okta-signed assertion is rejected, so the Okta
identity chain cannot cross into Drive no matter how both sides are configured.
Workforce Identity Federation does accept Okta via RFC 8693, but returns a Google
CLOUD token for IAM-governed resources — Workspace APIs are out of scope. AgentCore
agrees: `googleOauth2ProviderConfig` has no `onBehalfOfTokenExchangeConfig` member
(unlike `customOauth2ProviderConfig`), so its built-in Google provider is
consent-only by construction. Identity still propagates end to end, via the
verified `email` claim rather than a brokered token:

    Okta JWT --(signature/iss/aud verified)--> email --(sub of a SA assertion)-->
    Google --> Drive token for THAT user --> Drive's own ACLs trim the results

⚠️ THE SUBJECT IS THE WHOLE SECURITY MODEL. This service account can impersonate ANY
user in the domain for the granted scopes. `subject` must therefore come only from
identity_claims.verified_email() — a signature-verified claim. The assertion in
_credentials() enforces the shape, but shape is not provenance: passing a
caller-supplied string here turns the per-user ACL trim into a domain-wide read while
every log line still looks normal. That is the one bug in this design that is a breach
rather than an error.

SCOPES ARE READ-ONLY, deliberately: an assistant that can search has no reason to be
able to modify, and `drive.readonly` also covers files.export (how a Google Doc's text
is read — a Doc has no bytes to download) and `documents.get`, so the Docs API needs no
second scope added to the delegation grant.

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):

This module is the entire right-hand end of the OBO flow — steps 8a, 9a and 10a, which
exist in flow A alone. It has no tile of its own (it is a module inside the `RT`
container), so its work is drawn as the two arrows leaving the tools gateway. 8a is
`_credentials()`: `with_subject()` puts the verified email in the assertion's `sub`, and
the assertion is signed with the service-account key `_service_account_key()` reads from
`SM` (Secrets Manager). 9a is the `GSTS`->`GDRIVE` hop, which no line of code below
performs explicitly — the google-auth library exchanges the assertion at Google's token
endpoint on the first API call, which is why `SCOPES` and not a `scopes` parameter
decides what the returned token can do. 10a is the ACL trim, and there is deliberately
no code here that implements it: Drive returns only what this subject can open, and
nothing below filters afterwards.

WHY THE DIAGRAM DRAWS TWO ARROWS AND NOT ONE, restated because this file is where it
would be tempting to collapse them: no single credential spans both authorities. Okta is
authoritative over who the user is and Google over what they may read in Drive, and the
"WHY NOT OKTA TOKEN PROPAGATION" section above is the long form of why 8a cannot be an
Okta-signed assertion. The Drive path also touches `VAULT` at no point, which is why the
`TGW`->`GSTS` arrow is bowed around that tile: there is no vaulted token to fetch, and
routing it through the vault would assert the mechanism this source replaces.

WHAT THE DIAGRAM CANNOT SHOW ABOUT 10a, and it matters under STRICT GROUNDING. The
badge's sentence — "only documents this user can already open" — reads as complete, but
`search_drive()` reports Drive's own `incompleteSearch` as a note-bearing hit, and
`_read_text()` reports export caps, first-sheet-only CSV, and formats it cannot read at
all. So 10a returns three distinguishable things the single arrow conflates: what the
user cannot see, what was not searched, and what could not be extracted. Treating the
last two as the first is a confident wrong answer, which is the failure mode this source
is most exposed to.

Nothing here participates in the consent flow at any step, including the ★. A user of
this source is never asked for anything: the Workspace admin granted the delegation once,
domain-wide, and that grant is the `SM`->`TGW` credential hop rather than a numbered step.
"""
import json
import os
import threading

# Import lazily-ish: these are only needed when the source is enabled, but a missing
# dependency should fail loudly at import rather than mid-answer.
from google.oauth2 import service_account          # type: ignore[import-untyped]
from googleapiclient.discovery import build        # type: ignore[import-untyped]
from googleapiclient.errors import HttpError       # type: ignore[import-untyped]
import boto3

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Google Docs / Sheets / Slides are not files with bytes — they must be EXPORTED.
# Mapping the native type to the text format we want keeps that decision in one place.
_EXPORT_AS = {
    "application/vnd.google-apps.document": "text/plain",
    # CSV is the only text export Sheets offers, and it is only the FIRST sheet.
    # Reported as truncated when used, so a grounded answer cannot claim the
    # spreadsheet is silent on something that lives on sheet 2.
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
# Plain-text-ish files that CAN be downloaded directly.
_DOWNLOADABLE_PREFIXES = ("text/",)
_DOWNLOADABLE = {"application/json", "application/xml"}

_sa_lock = threading.Lock()
_sa_key: dict | None = None


class DriveConfigError(RuntimeError):
    """Misconfiguration, not a per-user failure. Worth surfacing to an operator."""


def _service_account_key() -> dict:
    """Fetch and cache the service-account JSON key from Secrets Manager.

    Cached per process: this is on the path of every Drive call, and re-reading a
    secret per invocation adds latency and cost for a value that changes ~never.
    Never written to disk — it is a domain-wide credential.
    """
    global _sa_key
    with _sa_lock:
        if _sa_key is None:
            arn = os.environ.get("GOOGLE_SA_SECRET_ARN") or ""
            if not arn:
                raise DriveConfigError(
                    "GOOGLE_SA_SECRET_ARN is not set on this runtime")
            region = (os.environ.get("AWS_REGION")
                      or os.environ.get("AWS_DEFAULT_REGION") or "")
            sm = boto3.client("secretsmanager", region_name=region or None)
            raw = sm.get_secret_value(SecretId=arn)["SecretString"]
            try:
                key = json.loads(raw)
            except json.JSONDecodeError as e:
                raise DriveConfigError(
                    "the Google secret is not JSON; it must hold the service-account "
                    "key file verbatim") from e
            missing = [f for f in ("client_email", "private_key", "token_uri")
                       if f not in key]
            if missing:
                raise DriveConfigError(
                    f"the Google secret is not a service-account key (missing {missing})")
            _sa_key = key
        return _sa_key


def _credentials(subject: str):
    """Credentials that impersonate `subject` via domain-wide delegation.

    `with_subject` is what puts the user's address in the assertion's `sub` claim,
    which is why Google issues a token scoped to THAT user and Drive's own ACLs do
    the filtering. Without it the token represents the service account, which can
    see nothing in anyone's Drive.
    """
    # Defence in depth, not the primary control. The primary control is that every
    # caller passes identity_claims.verified_email(). This catches a future refactor
    # that wires something else in — an empty or malformed subject must fail loudly
    # rather than fall back to the service account's own (much wider) view.
    if not subject or "@" not in subject:
        raise DriveConfigError(
            f"refusing to impersonate {subject!r}: the subject must be a verified "
            "user email (see agent/identity_claims.py). An unverified or empty "
            "subject here would be a domain-wide read.")
    return service_account.Credentials.from_service_account_info(
        _service_account_key(), scopes=SCOPES).with_subject(subject)


def _drive(subject: str):
    # cache_discovery=False: the default file cache is unwritable in the container
    # and emits a noisy warning per call.
    return build("drive", "v3", credentials=_credentials(subject),
                 cache_discovery=False)


def _explain(e: Exception) -> str:
    """Turn Google's opaque OAuth/API errors into the actual misconfiguration.

    These are the same mappings as scripts/verify_drive_delegation.py, kept here so a
    failure inside a live answer is as diagnosable as one at setup time.
    """
    s = str(e)
    if "unauthorized_client" in s:
        return ("Google rejected the delegation (unauthorized_client). Either the "
                "Admin console grant holds the service account EMAIL instead of its "
                "NUMERIC client id, or the requested scope is not in the authorized "
                f"list ({' '.join(SCOPES)}). Grants are per-scope, exact match.")
    if "invalid_grant" in s and ("Invalid email" in s or "User ID" in s):
        # Google documents only "the user doesn't exist" for this. Whether an ALIAS
        # resolves to its primary is undocumented either way, so name it as a thing to
        # rule out rather than as the cause.
        return ("Google does not recognise this user. The email claim must be a real "
                "Workspace user's PRIMARY address; if it is an alias, alias handling "
                "here is undocumented — retry with the primary before looking further.")
    if "accessNotConfigured" in s or "has not been used in project" in s:
        return "The Drive API is not enabled in the service account's Cloud project."
    # insufficientFilePermissions is Drive's DOCUMENTED 403 reason; the shorter
    # insufficientPermissions is matched too because other Google surfaces emit it.
    if "insufficientFilePermissions" in s or "insufficientPermissions" in s:
        return f"Scope too narrow for this call. Authorized: {' '.join(SCOPES)}"
    return f"{type(e).__name__}: {s[:300]}"


def _escape(term: str) -> str:
    """Escape a term for a Drive `q` fullText clause.

    Backslash first, then the quote — reversing that order double-escapes. An
    unescaped quote makes Drive reject the query as malformed, which for a
    question containing an apostrophe would look like "Drive found nothing".
    """
    return term.replace("\\", "\\\\").replace("'", "\\'")


def search_drive(subject: str, query: str, top: int = 8) -> list[dict]:
    """Search this user's Drive for content matching `query`.

    Returns hits with a `snippet` and a `ref` handle for read_drive_document. Drive
    has no text-match API — `fullText contains` matches but returns no excerpt — so
    the snippet is a short EXPORTED prefix of each hit. That matters under STRICT
    GROUNDING: metadata-only results let the model see that a relevant file exists
    while having no text to quote, which correctly produces LOW confidence and looks
    like a broken agent. Same lesson as github_tool's text-match Accept header.
    """
    if not query.strip():
        return []
    drive = _drive(subject)
    # trashed=false: a deleted file is still visible to fullText search otherwise,
    # and quoting a trashed document as current is a grounding error.
    q = f"fullText contains '{_escape(query.strip())}' and trashed = false"
    resp = drive.files().list(
        q=q, pageSize=top,
        # incompleteSearch must be REQUESTED — `fields` is a partial response mask, so
        # asking only for files() drops it silently. See the note below for why it is
        # load-bearing rather than diagnostic.
        fields=("incompleteSearch,"
                "files(id,name,mimeType,webViewLink,modifiedTime,owners(emailAddress))"),
        # Without these, shared drives are invisible and the result set looks
        # narrower than the user's real access — a misleading "trim".
        includeItemsFromAllDrives=True, supportsAllDrives=True).execute()

    hits: list[dict] = []
    # Searching allDrives spans several corpora, and Drive returns
    # incompleteSearch=true when it could not search all of them. Unreported, a
    # partial search is indistinguishable from a complete one, so the model says
    # "not found" when the truth is "not searched" — a confident wrong answer, which
    # under STRICT GROUNDING is the one failure mode worse than no answer. Carried as
    # a note-bearing entry in the hit list because that is the only channel back to
    # the model here: agent.py stamps `source` on it, and it has no `ref` so nothing
    # tries to fetch it.
    if resp.get("incompleteSearch"):
        hits.append({
            "title": "(partial search)",
            "incomplete_search": True,
            "note": ("Drive reported incompleteSearch: some drives could not be "
                     "searched, so these results may be missing matches. Do not "
                     "treat absence here as evidence that nothing exists."),
        })
    for f in resp.get("files", []):
        hit = {
            "title": f.get("name", "?"),
            "url": f.get("webViewLink", ""),
            "modified": f.get("modifiedTime", ""),
            "mime": f.get("mimeType", ""),
            "ref": f["id"],
            "snippet": "",
        }
        # Best-effort prefix. One file failing to export must not lose the rest of
        # the result set, so this is wrapped per file rather than around the loop.
        try:
            text = _read_text(drive, f["id"], f.get("mimeType", ""), max_chars=600)
            hit["snippet"] = text["text"][:600]
            # An extraction limit (oversize export, unreadable format) yields empty
            # text. Carrying the reason forward stops a silent "" from reading as "this
            # file matched but says nothing" — the same absence-vs-unavailable
            # confusion incompleteSearch causes above.
            if not hit["snippet"] and text.get("note"):
                hit["note"] = text["note"]
        except Exception:
            hit["snippet"] = f"(no preview; call read_company_document on {f['id']})"
        hits.append(hit)
    return hits


def _read_text(drive, file_id: str, mime: str, max_chars: int) -> dict:
    """Read one file's text, choosing export vs download by MIME type."""
    export_as = _EXPORT_AS.get(mime)
    if export_as:
        # export_media, NOT get_media: a Google Doc has no bytes to download, and
        # get_media on one returns a "only files with binary content" error.
        try:
            data = drive.files().export_media(
                fileId=file_id, mimeType=export_as).execute()
        except HttpError as e:
            # files.export caps exported content at 10 MB and has no range/partial
            # mode, so max_chars cannot rescue a big Doc: the WHOLE document is
            # fetched before we truncate, and an oversize one fails the call outright
            # rather than returning a prefix. Degrading to a `note` keeps that a
            # reported extraction limit (which SYSTEM_PROMPT tells the model to
            # respect) instead of an exception that voids the containing search.
            #
            # 401/403/404 are re-raised: those are the permission answers the ACL trim
            # is built on, and turning "you may not read this" into "we could not
            # extract text" would hide a real access failure behind a format excuse.
            status = getattr(getattr(e, "resp", None), "status", None)
            if status in (401, 403, 404):
                raise
            # Google documents no reason string for exceeding the cap, so this keys on
            # the failure itself and names the cap as the likely one, without claiming
            # to have identified it.
            return {"text": "", "truncated": True,
                    "note": (f"could not export {mime} as text (HTTP {status or '?'}); "
                             "files.export is capped at 10 MB, so a very large "
                             "document cannot be read at all — not even partially")}
    elif mime.startswith(_DOWNLOADABLE_PREFIXES) or mime in _DOWNLOADABLE:
        data = drive.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
    else:
        # PDFs, images and Office binaries would need OCR or a converter. Saying so
        # is better than returning bytes the model will hallucinate over.
        return {"text": "", "truncated": False,
                "note": f"cannot extract text from {mime}"}
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    return {"text": text[:max_chars], "truncated": len(text) > max_chars}


def fetch_drive_document(subject: str, ref: str, max_chars: int = 20000) -> dict:
    """Fetch one document's full text by the `ref` a search hit carried.

    STILL PERMISSION-TRIMMED: the credentials impersonate this user, so Google 404s
    anything they cannot read. `ref` is a Drive file id, so a caller cannot widen
    access by constructing one — an id for a file the user cannot see fails.
    """
    drive = _drive(subject)
    meta = drive.files().get(
        fileId=ref, supportsAllDrives=True,
        fields="id,name,mimeType,webViewLink,modifiedTime").execute()
    body = _read_text(drive, ref, meta.get("mimeType", ""), max_chars)
    out = {
        "title": meta.get("name", "?"),
        "url": meta.get("webViewLink", ""),
        "modified": meta.get("modifiedTime", ""),
        "mime": meta.get("mimeType", ""),
        "ref": ref,
        **body,
    }
    if meta.get("mimeType") == "application/vnd.google-apps.spreadsheet":
        # Report the limitation rather than letting the model treat sheet 1 as the
        # whole workbook. Appended, not assigned: if the export failed outright,
        # _read_text's note is the more important of the two and must survive.
        out["truncated"] = True
        first_sheet = "CSV export covers the FIRST sheet only"
        out["note"] = (f"{out['note']}; {first_sheet}" if out.get("note")
                       else first_sheet)
    return out


def list_drive_sources(subject: str, top: int = 100) -> list[dict]:
    """What this user can see: their shared drives, plus recent documents.

    Answers "what do I have access to" — an inventory question that content search
    cannot. Shared drives come first because they are the closest Drive equivalent to
    a repository; `owned_by_user` marks files this user owns.
    """
    drive = _drive(subject)
    items: list[dict] = []

    # Shared drives. Not fatal if unavailable: a domain may use none, in which case
    # the recent-files list below is the whole answer.
    try:
        for d in drive.drives().list(pageSize=min(top, 100),
                                     fields="drives(id,name)").execute().get("drives", []):
            items.append({"name": d.get("name", "?"), "kind": "shared drive",
                          "ref": d.get("id", ""), "owned_by_user": False})
    except HttpError:
        pass

    resp = drive.files().list(
        pageSize=min(top, 100), orderBy="modifiedTime desc",
        q="trashed = false",
        fields="files(id,name,mimeType,webViewLink,modifiedTime,owners(emailAddress))",
        includeItemsFromAllDrives=True, supportsAllDrives=True).execute()
    for f in resp.get("files", []):
        owners = [o.get("emailAddress", "") for o in (f.get("owners") or [])]
        items.append({
            "name": f.get("name", "?"),
            "kind": ("google doc" if f.get("mimeType", "").endswith("apps.document")
                     else f.get("mimeType", "file")),
            "url": f.get("webViewLink", ""),
            "modified": f.get("modifiedTime", ""),
            "ref": f["id"],
            # Ownership, not authorship — same honesty constraint as github_tool.
            "owned_by_user": subject in owners,
        })
    return items
