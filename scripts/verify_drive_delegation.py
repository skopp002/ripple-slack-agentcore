#!/usr/bin/env python3
"""Prove Google domain-wide delegation works — BEFORE any agent code depends on it.

    export GOOGLE_SA_SECRET_ARN='arn:aws:secretsmanager:...:secret:ripple/google-drive-sa-xxxx'
    python3 scripts/verify_drive_delegation.py user-one@yourdomain.com
    python3 scripts/verify_drive_delegation.py user-two@yourdomain.com
    python3 scripts/verify_drive_delegation.py user@dom.com --export <fileId>

WHY THIS EXISTS AS A SEPARATE SCRIPT. Hop 2 of the Drive design (service-account
impersonation) involves a Workspace super-admin, a numeric client ID that is easy to
confuse with an email, and a scope list that must match exactly. Every one of those
fails with an opaque OAuth error. Debugging them *through* the agent — behind a
runtime, a JWT authorizer and a Strands loop — wastes hours, so this reproduces the
one exchange that matters with nothing else in the path: no Okta, no AgentCore, no
Ripple. Running it for TWO users and getting DIFFERENT file lists is the ACL trim,
demonstrated end to end.

WHAT IT DELIBERATELY DOES NOT DO: it takes the impersonation subject from ARGV,
because at this stage there is no token to read it from. In the agent the subject
comes ONLY from the validated Okta `email` claim — see the assertion in the Drive
tool. A caller-supplied subject here is fine (you are the admin, at a shell); a
caller-supplied subject in the agent would be a domain-wide read dressed up as an
ACL trim.

Reads no AWS resource except the one secret ARN passed in, and creates nothing.

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png): it
reproduces the right-hand end of the delegation flow — steps 8a, 9a and 10a — with every
other tile on the canvas removed. It reads the service-account key from the Secrets
Manager tile, signs the assertion that step 8a sends to the Google OAuth2 token
endpoint, receives back the token step 9a describes (scoped to one named user, with no
consent screen anywhere), and lists what Google Drive returns for it, which is step
10a's ACL trim. Two runs for two users returning different files is that trim
demonstrated rather than asserted.

It creates no tile and is on no deployed path — it runs from a laptop, so on the
diagram's trust boundary it sits where the CLI client does, outside the AWS account
entirely. What it deliberately SKIPS is the left half of the flow: no Okta, no
runtime, no gateway, no vault, and therefore no step 6a. That absence is the reason
the subject comes from argv here. Step 6a is the runtime re-verifying the JWT's
signature before naming an impersonation subject, and it is what makes 8a safe given
that the key below holds domain-wide delegation; with 6a out of the picture there is
no verified claim to take a subject from. A caller-supplied subject is correct in this
script and would be a domain-wide read dressed up as an ACL trim in the agent.
"""
import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

# Least privilege, and read-only on purpose: this script exists to verify access, so
# it must never be capable of modifying a customer's Drive. `drive.readonly` also
# covers files.export, which is how a Google Doc's text is read (a Doc has no bytes
# to download — see docs/GOOGLE-DRIVE-OKTA-SETUP.md § "Google Docs specifically").
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def load_service_account(secret_arn: str, region: str) -> dict:
    """Fetch the service-account JSON key from Secrets Manager.

    The key is never written to disk by this script. It is a DOMAIN-WIDE credential:
    whoever holds it can impersonate any user in the domain for the granted scopes.
    """
    sm = boto3.client("secretsmanager", region_name=region)
    try:
        raw = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
    except ClientError as e:
        raise SystemExit(
            f"Cannot read {secret_arn}: {e}\n"
            "  Check the ARN, the region, and that your caller has "
            "secretsmanager:GetSecretValue on it.")
    try:
        key = json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(
            "The secret is not JSON. It should hold the service-account key FILE "
            "verbatim:\n  aws secretsmanager create-secret --name ripple/google-drive-sa "
            "--secret-string file://<key>.json")
    missing = [f for f in ("client_email", "private_key", "token_uri") if f not in key]
    if missing:
        raise SystemExit(
            f"The secret is JSON but not a service-account key (missing {missing}).\n"
            "  Expected the file downloaded from Service Accounts -> Keys -> Add key.")
    return key


def credentials_for(key: dict, subject: str):
    """Build credentials that impersonate `subject` via domain-wide delegation.

    `with_subject` is what puts the user's address in the assertion's `sub` claim —
    the mechanism by which Google issues a token scoped to THAT user, so Drive's own
    ACLs do the filtering. Without it the token represents the service account, which
    can see nothing in anyone's Drive and makes the whole exercise pointless.
    """
    try:
        from google.oauth2 import service_account
    except ImportError:
        raise SystemExit(
            "Missing dependency. Install into the dev venv:\n"
            "  ./.venv/bin/pip install google-auth google-api-python-client")
    return service_account.Credentials.from_service_account_info(
        key, scopes=SCOPES).with_subject(subject)


def explain(err: Exception, subject: str, client_email: str) -> str:
    """Translate Google's opaque OAuth errors into the actual misconfiguration.

    Each of these cost real time to diagnose the first time; the mapping is the
    whole value of this script.
    """
    s = str(err)
    if "unauthorized_client" in s:
        return (
            "unauthorized_client — the delegation grant does not match this request.\n"
            "  Two causes, both common:\n"
            "   1. The Admin console 'Client ID' field holds the service account EMAIL\n"
            f"      ({client_email}) instead of its NUMERIC client id. Fix: Service\n"
            "      Accounts -> the account -> Show advanced settings -> Client ID.\n"
            f"   2. The scope is not in the authorized list. Grants are per-scope and\n"
            f"      exact-match; this script asks for: {' '.join(SCOPES)}")
    if "invalid_grant" in s and ("Invalid email" in s or "User ID" in s):
        # Google documents exactly one cause for this — "the user doesn't exist" — and
        # says nothing about aliases either way. So: name the fix, not a mechanism.
        return (
            f"invalid_grant — Google does not recognise '{subject}'.\n"
            "  It must be a real Workspace user. Use the PRIMARY address: whether an\n"
            "  alias resolves to its primary is undocumented and not something to rely\n"
            "  on, so an alias is the first thing to rule out. See\n"
            "  docs/GOOGLE-DRIVE-OKTA-SETUP.md § 'The claim that actually matters'.")
    if "accessNotConfigured" in s or "has not been used in project" in s:
        return ("The Drive API is not enabled in the Cloud project that owns this\n"
                "  service account. APIs & Services -> Library -> Google Drive API.")
    # insufficientFilePermissions is Drive's documented 403 reason; the broader match
    # catches the other spellings Google's OAuth surfaces use.
    if "insufficientFilePermissions" in s or "insufficient" in s.lower():
        return ("Delegation works but the scope is too narrow for this call.\n"
                f"  Authorized here: {' '.join(SCOPES)}")
    return f"Unrecognised error: {s}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("subject", help="Workspace user to impersonate (PRIMARY address)")
    ap.add_argument("--secret-arn", default=os.environ.get("GOOGLE_SA_SECRET_ARN", ""),
                    help="defaults to $GOOGLE_SA_SECRET_ARN")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", ""),
                    help="defaults to $AWS_REGION")
    ap.add_argument("--top", type=int, default=10, help="how many files to list")
    ap.add_argument("--export", metavar="FILE_ID",
                    help="also export this file as text — the Google DOCS path, which "
                         "a plain download cannot do")
    args = ap.parse_args()

    if not args.secret_arn:
        raise SystemExit("Set GOOGLE_SA_SECRET_ARN or pass --secret-arn.")
    if not args.region:
        raise SystemExit("Set AWS_REGION or pass --region.")
    if "@" not in args.subject:
        raise SystemExit(f"'{args.subject}' is not an email address.")

    key = load_service_account(args.secret_arn, args.region)
    print(f"service account : {key['client_email']}")
    print(f"impersonating   : {args.subject}")
    print(f"scopes          : {' '.join(SCOPES)}\n")

    try:
        from googleapiclient.discovery import build
    except ImportError:
        raise SystemExit(
            "Missing dependency. Install into the dev venv:\n"
            "  ./.venv/bin/pip install google-auth google-api-python-client")

    try:
        creds = credentials_for(key, args.subject)
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        # Not `q=`-filtered: the point is to show what this USER can see at all.
        resp = drive.files().list(
            pageSize=args.top,
            # incompleteSearch has to be asked for — `fields` is a response mask, so
            # requesting only files() silently drops it.
            fields="incompleteSearch,files(id,name,mimeType,owners(emailAddress))",
            # Without these two, shared drives are invisible and the list looks
            # emptier than the user's real access — a misleading "trim".
            includeItemsFromAllDrives=True, supportsAllDrives=True).execute()
    except Exception as e:                                   # noqa: BLE001
        print("FAILED\n")
        print(explain(e, args.subject, key["client_email"]))
        return 1

    if resp.get("incompleteSearch"):
        # Matters most here, where an empty list is read as "delegation works but the
        # user sees nothing" — a conclusion this flag invalidates.
        print("NOTE — Drive returned incompleteSearch: some drives could not be\n"
              "  searched, so the list below is partial. Do not read a short list as\n"
              "  the user's full access.\n")

    files = resp.get("files", [])
    if not files:
        print("OK — the exchange SUCCEEDED, but this user can see no files.")
        print("  Delegation is correct (an auth failure would have raised above).")
        print("  Share a Doc with this user, then re-run. See the doc's "
              "'Seed test content'.")
    else:
        print(f"OK — {len(files)} file(s) visible to {args.subject}:\n")
        for f in files:
            owner = (f.get("owners") or [{}])[0].get("emailAddress", "?")
            kind = "DOC " if f["mimeType"].endswith("apps.document") else "file"
            print(f"  [{kind}] {f['name']}")
            print(f"         id={f['id']}  owner={owner}")

    if args.export:
        print(f"\nexporting {args.export} as text/plain ...")
        try:
            # export_media, NOT get_media: a Google Doc has no bytes to download.
            # This is the call the agent's Drive tool uses to get quotable text.
            data = drive.files().export_media(
                fileId=args.export, mimeType="text/plain").execute()
        except Exception as e:                               # noqa: BLE001
            print("EXPORT FAILED\n")
            print(explain(e, args.subject, key["client_email"]))
            print("  If the delegation checks above passed, note that files.export is\n"
                  "  capped at 10 MB of exported content — a very large Doc fails\n"
                  "  outright rather than returning a prefix.")
            return 1
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
        print(f"OK — {len(text)} chars. First 400:\n")
        print(text[:400])

    print("\nRun this for a SECOND user. Different file lists = the ACL trim, proven\n"
          "with no Okta, no AgentCore and no consent screen in the path.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
