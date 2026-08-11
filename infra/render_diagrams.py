#!/usr/bin/env python3
"""Render the ARCHITECTURE.md flows to PNG — no node/npm/mmdc required.

Why not mermaid-cli: it needs node plus a headless Chromium download, and this
box has neither (nor docker). The diagrams are drawn with matplotlib instead.

ONE DIAGRAM PER FLOW, AND THE FILENAME SAYS WHICH FLOW. There are three:

    architecture-deploy-flow.png      how the system gets built and deployed
    architecture-delegation-flow.png  answering from Google Drive — no consent screen
    architecture-consent-flow.png     answering from GitHub — one consent click

⚠️ THE MIDDLE ONE USED TO BE CALLED architecture-obo-flow.png, AND THE RENAME IS THE
POINT, not tidying. "OBO" is overloaded: agent.py reserves it for
ON_BEHALF_OF_TOKEN_EXCHANGE, which is M3 — RFC 8693, no third-party source uses it, and
Google Drive CANNOT use it because Google requires a service-account assertion signed
with a key we hold. The flow drawn here is M2, DELEGATION. So the old filename named the
one mechanism the diagram does not draw, and asserted it in the one place a reader cannot
miss. The word does not appear as a mechanism name anywhere in this file any more; where
M3 is genuinely meant, the full ON_BEHALF_OF_TOKEN_EXCHANGE is spelled out. See
render_components_v3.py for the authoritative M1/M2/M3 vocabulary.

Three is a deliberate reduction from four. An earlier "request time" diagram drew the
consent path only, under a name that promised both, so a reader looking for the
delegation flow found the consent flow with no label saying so; a second diagram then
drew both paths on one canvas, which meant neither could be read without disentangling it
from the other. Splitting by flow means every numbered step on a canvas belongs to the
flow the filename names. The one diagram that still draws both together is
architecture-components.png (render_components.py), which is a component map rather
than a sequence and says so in its title.

EVERY FLOW HAS ONE START AND ONE END, MARKED AS SUCH. Edge 1 begins at the user and
carries a ▶ START badge; the last edge returns to the user and carries a ■ END badge.
Both request flows therefore close the loop back to the person who asked — which is
the claim each of them is making — and `check_endpoints()` asserts it rather than
leaving it to the eye. Numbering within a flow is contiguous 1..N with no a/b suffix,
because a canvas that shows one flow needs no way to tell two apart.

ARROWS BEGIN AND END ON A LIFELINE. An earlier version offset BOTH endpoints outward
by half a participant box, so an arrow between adjacent columns became a stub floating
in the gutter, touching neither lifeline: the diagram drew motion between two things
without showing which two. Endpoints are now the lifeline x-coordinates exactly, with
a departure dot at the tail so tail and head are distinguishable without an activation
box. `check_geometry()` asserts this from the drawn coordinates.

Why a SEQUENCE layout rather than a boxes-and-arrows graph: each flow is strictly
ordered 1..N, so one row per numbered edge is guaranteed to have no crossing arrows
and no label collisions — the free-form graph layout was unreadable at this node
count.

Why the layout is MEASURED rather than hand-tuned: every box width, row height and
label position is derived from the real rendered text extent, so nothing can overlap
regardless of how the labels below are edited. Data coordinates are INCHES (the axes
fills the figure and 1 data unit == 1 inch), which makes "width of this string in
points / 72" directly comparable to layout geometry.

Usage:
    python3 infra/render_diagrams.py            # write all three PNGs
    python3 infra/render_diagrams.py --check    # verify, no re-render

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):

This file is not on any flow — it DRAWS the three sequence diagrams above, and none of
them is the components diagram. Numbering here is per-flow and contiguous: the delegation
flow runs 1-17, the consent flow 1-26, the deploy flow 1-21. The components diagram numbers
the same two request stories differently (1-5 shared, then 6a-14a and 6b-14b) because it
draws them on one canvas and needs the a/b suffix to tell them apart. Neither scheme is
derived from the other, so a step number is meaningless without naming the diagram it
came from. Every edge number here is parity-checked against ARCHITECTURE.md and against
nothing else.

The components diagram is rendered by render_components.py. Editing this file cannot
change it, and renumbering an edge here does not renumber a badge there.
"""
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                      # noqa: E402
from matplotlib.patches import (                     # noqa: E402
    Circle, FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle)

HERE = os.path.dirname(os.path.abspath(__file__))
DOC = os.path.join(HERE, "ARCHITECTURE.md")

# Ownership kinds — same legend as ARCHITECTURE.md.
FILL = {"CFN": "#d3e5f5", "SVC": "#fde2c4", "EXT": "#e6e6e6",
        "OPS": "#d9f0d3", "ENG": "#e8dff3"}
LINE = {"CFN": "#2d6ca2", "SVC": "#c8791d", "EXT": "#6b6b6b",
        "OPS": "#3f8f43", "ENG": "#7a5aa6"}
LEGEND = [
    ("CFN", "created and owned by CloudFormation"),
    ("SVC", "created by the service at first use, tagged by apply_tags.py"),
    ("EXT", "external to AWS / to CloudFormation"),
    ("OPS", "manual operator step"),
    ("ENG", "the CloudFormation service itself (deploy engine)"),
]

# Per-flow accent, used for the flow banner and the START / END badges. Same
# convention as render_components.py: green = delegation (M2), orange = consent (M1).
ACCENT = {"deploy": "#2d6ca2", "delegation": "#3f8f43", "consent": "#c8791d"}

# ===========================================================================
# DEPLOY FLOW — architecture-deploy-flow.png
#
# The only one of the three that does not involve an end user, so the only one whose
# END is not a person. It starts at an operator shell and ends with a tagged, running
# runtime endpoint that the other two diagrams then use.
# ===========================================================================
DEPLOY_COLS = [
    ("OP",     "Operator\nshell", "OPS"),
    ("CFN",    "Cloud-\nFormation", "ENG"),
    ("BUILD",  "scripts/\ncfn_build.py", "OPS"),
    ("TAGS",   "scripts/\napply_tags.py", "OPS"),
    ("SM",     "Secrets Mgr\nripple/github-oauth", "EXT"),
    ("GH",     "GitHub\nOAuth App", "EXT"),
    ("S3",     "S3 build\nsource", "CFN"),
    ("CB",     "CodeBuild\nARM64", "CFN"),
    ("CBROLE", "IAM Ripple\nCodeBuildRole", "CFN"),
    ("CBLOG",  "CodeBuild\nlog group", "CFN"),
    ("ECR",    "ECR repo\nimmutable tags", "CFN"),
    ("MEM",    "AgentCore\nMemory", "CFN"),
    ("PROV",   "OAuth2 provider\nripple-github", "CFN"),
    ("RT",     "AgentCore Runtime\nCUSTOM_JWT", "CFN"),
    ("RTROLE", "IAM Ripple\nRuntimeRole", "CFN"),
    ("EP",     "Runtime endpoint\nlive", "CFN"),
    ("RTLOG",  "Runtime\nlog group", "SVC"),
]
DEPLOY_BANDS = [
    ("OP", "TAGS", "operator + deploy engine", "#3f8f43"),
    ("SM", "GH", "outside CFN", "#6b6b6b"),
    ("S3", "ECR", "STACK 1  ripple-foundation  (01-foundation.yaml)", "#2d6ca2"),
    ("MEM", "EP", "STACK 2  ripple-runtime  (02-runtime.yaml)", "#2d6ca2"),
    ("RTLOG", "RTLOG", "service-made", "#c8791d"),
]
# (src, dst, number, label, style)   style: '-' solid, ':' dashed
DEPLOY_EDGES = [
    ("OP", "SM", 1, "START — one-time: create ripple/github-oauth holding {\"client_secret\": ...}", "-"),
    ("OP", "CFN", 2, "deploy stack 1  ->  S3, ECR, CodeBuild, build role, log group", "-"),
    ("CB", "CBROLE", 3, "assumes the build role (ECR push + S3 read only)", ":"),
    ("OP", "BUILD", 4, "run the image build — the one step CloudFormation cannot do", "-"),
    ("BUILD", "S3", 5, "zip Dockerfile + requirements.txt + agent/  ->  ripple/source.zip", "-"),
    ("BUILD", "CB", 6, "start_build with IMAGE_TAG = UTC timestamp", "-"),
    ("S3", "CB", 7, "CodeBuild fetches source.zip", "-"),
    ("CB", "ECR", 8, "build linux/arm64 image, push under the timestamp tag", "-"),
    ("CB", "CBLOG", 9, "build logs (30-day retention)", "-"),
    ("BUILD", "OP", 10, "prints IMAGE_TAG=<tag> as the last line", "-"),
    ("OP", "CFN", 11, "deploy stack 2 with ImageTag + IdP / GitHub parameters", "-"),
    ("PROV", "SM", 12, "read the client secret via ClientSecretSource=EXTERNAL", "-"),
    ("RT", "RTROLE", 13, "assumes the project-scoped execution role", ":"),
    ("ECR", "RT", 14, "runtime pulls the container image by ContainerUri", "-"),
    ("MEM", "RT", 15, "!GetAtt Memory.MemoryId injected as an env var", ":"),
    ("RT", "OP", 16, "GithubCallbackUrl stack output", "-"),
    ("OP", "GH", 17, "MANUAL: paste that callback URL — without it the consent flow fails at its last hop", "-"),
    ("RT", "RTLOG", 18, "service creates the log group on first invoke", "-"),
    ("OP", "TAGS", 19, "run the tagger — covers what CloudFormation cannot declare", "-"),
    ("TAGS", "RTLOG", 20, "apply project_name=ripple_slack_assistant to the service-made log group", "-"),
    ("TAGS", "SM", 21, "END — the same tag on the Identity-managed secret; the runtime is now live and tagged", "-"),
]
DEPLOY_PHASES = [
    (1, "▶  START — ONE-TIME SECRET"),
    (2, "STACK 1"),
    (4, "BUILD THE IMAGE  (outside CloudFormation)"),
    (11, "STACK 2"),
    (19, "POST-DEPLOY TAGGING   ->   ■  END"),
]
DEPLOY_NOTE = (
    "START is the operator shell; END is a live, tagged runtime endpoint. This is the one "
    "flow of the three that does not return to an end user, because no end user takes part "
    "in it.\n"
    "The runtime endpoint 'live' is created by edge 11 but sends no deploy-time message — "
    "it is where the delegation and consent flows both begin their authenticated invoke.\n"
    "Edges 2 and 11 target the CloudFormation service, which then creates the [CFN] "
    "lifelines bracketed as STACK 1 / STACK 2.\n"
    "Edge 17 is the only step that can block a cutover, and only because replacing the "
    "credential provider issues a new callback URL.")

# ===========================================================================
# DELEGATION FLOW (M2) — architecture-delegation-flow.png
#
# NOT a token exchange, and the old name for this file said it was. M2 is domain-wide
# delegation: we hold a Google service-account key and sign an assertion naming the user.
# M3 (ON_BEHALF_OF_TOKEN_EXCHANGE) is RFC 8693, brokered by AgentCore Identity, and Drive
# can never move to it — see the note at edge 11.
#
# The flow the demo exercises. Google Drive is reached with a token minted FOR the
# calling user by domain-wide delegation, so no consent screen exists anywhere on this
# canvas. The only interactive step is the sign-in at edge 3, and it is ONE sign-in
# rather than one per source — which is the whole difference from the consent flow and
# the reason the two are drawn apart.
#
# THE LOAD-BEARING STEPS ARE 8 AND 11. Edge 8 is the agent re-verifying the user's JWT
# itself, three fail-closed gates; edge 11 is the assertion whose `sub` is the email
# that verification produced. Impersonation is safe only because 8 precedes 11.
# ===========================================================================
DELEG_COLS = [
    ("USER",   "User", "OPS"),
    ("CLI",    "Caller\nclient/login.py + ask.py", "OPS"),
    ("IDP",    "IdP\nauthorization server", "EXT"),
    ("EP",     "Runtime endpoint live\nCUSTOM_JWT", "CFN"),
    ("AGENT",  "agent/agent.py\nStrands harness", "CFN"),
    ("BR",     "Amazon Bedrock\nClaude model", "CFN"),
    ("SM",     "Secrets Manager\nservice-account key", "EXT"),
    ("GSTS",   "Google STS\noauth2.googleapis.com", "EXT"),
    ("GDRIVE", "Google Drive + Docs\nAPI", "EXT"),
]
DELEG_BANDS = [
    ("USER", "CLI", "caller", "#3f8f43"),
    ("IDP", "IDP", "the ONE login", "#6b6b6b"),
    ("EP", "BR", "inside AWS — CloudFormation-owned", "#2d6ca2"),
    ("SM", "GDRIVE", "the delegation hop — no consent screen exists here", "#3f8f43"),
]
DELEG_EDGES = [
    ("USER", "CLI", 1, "START — the user runs client/login.py, then asks a question", "-"),
    ("CLI", "IDP", 2, "device-code grant; no per-source prompt is requested anywhere in this flow", "-"),
    ("USER", "IDP", 3, "approves the device code in a browser — ONE sign-in, not one per source", ":"),
    ("IDP", "CLI", 4, "the user's JWT, aud = the runtime's API identifier; cached in client/.token.json", "-"),
    ("CLI", "EP", 5, "client/ask.py POSTs the question; that JWT rides the Authorization header, and the "
                     "body carries the question ALONE — one identity channel per request", "-"),
    ("EP", "IDP", 6, "CUSTOM_JWT authorizer validates signature, iss and aud against JWKS", ":"),
    ("EP", "AGENT", 7, "invokes the container entrypoint; the SDK exposes that same header on `context`", "-"),
    ("AGENT", "IDP", 8, "the agent reads the JWT from the HEADER edge 6 authenticated and RE-verifies it "
                        "itself: signature/iss/aud, then email_verified, then the domain allowlist — three "
                        "fail-closed gates, and the last two have no counterpart at edge 6", ":"),
    ("AGENT", "BR", 9, "Strands reasons over the question and selects search_drive", "-"),
    ("AGENT", "SM", 10, "gdrive_tool reads the service-account key from GOOGLE_SA_SECRET_ARN, first use only", "-"),
    ("AGENT", "GSTS", 11, "jwt-bearer assertion signed with that key, sub = the email edge 8 verified (RFC 7523)", "-"),
    ("GSTS", "AGENT", 12, "an access token scoped to THAT ONE USER — no consent screen at 10, 11 or 12", "-"),
    ("AGENT", "GDRIVE", 13, "search_drive: files.list with fullText contains, under that user-scoped token", "-"),
    ("GDRIVE", "AGENT", 14, "only files this user can open — Drive's OWN ACLs did the trim; nothing here filters", "-"),
    ("AGENT", "EP", 15, "answer + [n] citations + HIGH / MEDIUM / LOW confidence", "-"),
    ("EP", "CLI", 16, "HTTP response", "-"),
    ("CLI", "USER", 17, "END — the answer is printed with citations. One invocation, no consent screen.", "-"),
]
DELEG_PHASES = [
    (1, "▶  START — SIGN IN ONCE  (the only interactive step here)"),
    (5, "AUTHENTICATED INVOKE"),
    (8, "VERIFY THE USER, THEN IMPERSONATE THEM  (8 must precede 11)"),
    (13, "RETRIEVE — Drive trims to this user"),
    (15, "RESPONSE   ->   ■  END"),
]
DELEG_NOTE = (
    "START and END are both the user, and exactly ONE invocation sits between them: the "
    "question asked at edge 1 is answered at edge 17 without interruption.\n"
    "NO CONSENT SCREEN EXISTS ON THIS CANVAS. The Workspace admin granted domain-wide "
    "delegation once, for drive.readonly alone; every user afterwards is impersonated "
    "without being asked. The sign-in at edge 3 is authentication, not consent.\n"
    "TWO AUTHORITIES, TWO HOPS, AND THEY CANNOT BE COLLAPSED INTO ONE. The IdP is "
    "authoritative over who the user is (edges 6, 8); Google is authoritative over what "
    "they may read in Drive (edges 11-14). Google's jwt-bearer grant requires the "
    "assertion be signed by a Google SERVICE ACCOUNT key, so no IdP-issued token is "
    "accepted at edge 11 however both sides are configured. IdP SSO into Workspace is "
    "convenience; it is not what makes edge 11 work.\n"
    "EDGE 8 IS WHY EDGE 11 IS SAFE. The service account can impersonate any user in the "
    "domain, so `sub` may come only from a signature-verified claim. A caller-supplied "
    "email there turns the per-user trim into a domain-wide read with every log line "
    "still looking normal.\n"
    "ONE IDENTITY CHANNEL, WHICH IS WHY 6 AND 8 CANNOT DISAGREE. Edge 5 carries the JWT "
    "on the Authorization header only; the request body holds the question. Edge 8 reads "
    "that same header, so the token the authorizer authenticated at 6 and the token that "
    "names the impersonated user at 11 are one value. Accepting an identity from the body "
    "as well would let a caller pair a valid header for themselves with a body token "
    "naming a colleague — both verify, and 11 would follow the body.\n"
    "EDGE 8 IS NOT REDUNDANT WITH EDGE 6, though it re-checks the same signature. Edge 6 "
    "proves the token is AUTHENTIC; it does not prove its holder owns the mailbox in "
    "`email`. An issuer permitting self-signup signs a colleague's address quite "
    "genuinely, so `email_verified` and the domain allowlist exist only at 8.")

# ===========================================================================
# CONSENT FLOW — architecture-consent-flow.png
#
# GitHub, which cannot do the M3 token exchange: its token endpoint answers the RFC 8693
# grant with unsupported_grant_type (verified empirically), so the user's own OAuth
# token has to be brokered and vaulted instead.
#
# WHY THIS DIAGRAM IS HALF AGAIN AS LONG AS THE DELEGATION ONE. It draws what happens rather
# than what is convenient to draw: TWO invocations. The runtime does not hold a request
# open waiting for a human (CONSENT_WAIT_SECONDS = 0, NoWaitTokenPoller), so the first
# ask returns a URL instead of content and the client re-asks afterwards. An earlier
# diagram showed the consent and the vaulting as consecutive edges inside one request,
# which made the first-ask-is-ungrounded behaviour invisible — the most expensive
# misreading this drawing can cause.
#
# EDGE 15 IS THE ONLY HUMAN ACTION ON EITHER REQUEST FLOW'S CANVAS, and edge 17 is what
# actually vaults the token. Approving at 15 vaults nothing on its own; only the client
# calling CompleteResourceTokenAuth with the session_id does.
# ===========================================================================
CONSENT_COLS = [
    ("USER",  "User", "OPS"),
    ("CLI",   "Caller\nclient/ask.py + consent.py", "OPS"),
    ("IDP",   "IdP\nauthorization server", "EXT"),
    ("EP",    "Runtime endpoint live\nCUSTOM_JWT", "CFN"),
    ("AGENT", "agent/agent.py\nStrands harness", "CFN"),
    ("BR",    "Amazon Bedrock\nClaude model", "CFN"),
    ("WI",    "Workload identity\nWorkloadAccessToken", "CFN"),
    ("VAULT", "AgentCore Identity\ntoken vault ripple-github", "CFN"),
    ("GHAPP", "GitHub OAuth App\nconsent screen", "EXT"),
    ("GH",    "GitHub API", "EXT"),
]
CONSENT_BANDS = [
    ("USER", "CLI", "caller", "#3f8f43"),
    ("IDP", "IDP", "the ONE login", "#6b6b6b"),
    ("EP", "VAULT", "inside AWS — CloudFormation-owned", "#2d6ca2"),
    ("GHAPP", "GH", "GitHub — one consent click, then user-scoped for good", "#c8791d"),
]
CONSENT_EDGES = [
    ("USER", "CLI", 1, "START — the user runs client/login.py, then asks a question", "-"),
    ("CLI", "IDP", 2, "device-code grant", "-"),
    ("USER", "IDP", 3, "approves the device code in a browser — this is authentication, not consent", ":"),
    ("IDP", "CLI", 4, "the user's JWT, cached in client/.token.json", "-"),
    ("CLI", "EP", 5, "client/ask.py POSTs the question, that JWT on the Authorization header     "
                     "[invocation 1 of 2]", "-"),
    ("EP", "IDP", 6, "CUSTOM_JWT authorizer validates signature, iss and aud against JWKS", ":"),
    ("EP", "AGENT", 7, "invokes the container entrypoint", "-"),
    ("AGENT", "BR", 8, "Strands reasons over the question and selects search_github", "-"),
    ("AGENT", "WI", 9, "the PLATFORM injected a WorkloadAccessToken and the SDK forwards it from a "
                       "contextvar — agent code never sees the user JWT and cannot assert a user", ":"),
    ("WI", "VAULT", 10, "workloadIdentityToken: the PROOF of which user is asking", "-"),
    ("VAULT", "AGENT", 11, "no token vaulted for this user yet, so it returns an authorization URL and does "
                           "NOT block (CONSENT_WAIT_SECONDS = 0, NoWaitTokenPoller)", "-"),
    ("AGENT", "EP", 12, "the answer carries auth_required with that URL instead of content — this first "
                        "ask is deliberately UNGROUNDED", "-"),
    ("EP", "CLI", 13, "HTTP response", "-"),
    ("CLI", "USER", 14, "prints 'authorize here' and opens the browser", "-"),
    ("USER", "GHAPP", 15, "★  THE ONE HUMAN ACTION ON EITHER REQUEST FLOW: approves on GitHub's consent "
                          "screen — once per user, per source, then never again", ":"),
    ("GHAPP", "CLI", 16, "GitHub redirects to OUR return URL carrying ?session_id=...", "-"),
    ("CLI", "VAULT", 17, "CompleteResourceTokenAuth(session_id) — THE step that actually vaults the token; "
                         "approving at 15 vaults nothing on its own", "-"),
    ("CLI", "EP", 18, "client/ask.py re-asks the SAME question automatically     [invocation 2 of 2]", "-"),
    ("EP", "AGENT", 19, "invokes the container entrypoint again", "-"),
    ("AGENT", "VAULT", 20, "GetResourceOauth2Token — the SAME call site and the SAME code as edge 10", "-"),
    ("VAULT", "AGENT", 21, "the user's own GitHub token, from the vault; edges 11-17 never recur", "-"),
    ("AGENT", "GH", 22, "search_github: code, issues and PRs under that user's own OAuth token", "-"),
    ("GH", "AGENT", 23, "only repos this user can access — GitHub's OWN ACLs did the trim", "-"),
    ("AGENT", "EP", 24, "answer + [n] citations + HIGH / MEDIUM / LOW confidence", "-"),
    ("EP", "CLI", 25, "HTTP response", "-"),
    ("CLI", "USER", 26, "END — the answer is printed with citations. Every later question starts here.", "-"),
]
CONSENT_PHASES = [
    (1, "▶  START — SIGN IN ONCE"),
    (5, "FIRST ASK — no token yet, so no content comes back"),
    (15, "★  THE HUMAN STEP  (once per user, per source)"),
    (18, "SECOND ASK — same question, now grounded"),
    (24, "RESPONSE   ->   ■  END"),
]
CONSENT_NOTE = (
    "START and END are both the user, but TWO invocations sit between them (edges 5 and "
    "18), because a human acts in the middle and the runtime will not hold a request open "
    "for one. Edges 1-14 answer nothing; edges 18-26 answer the same question.\n"
    "APPROVING IS NOT VAULTING. Edge 15 is the click; edge 17 is what stores the token. "
    "If the return URL is not registered, or the client never completes the session, the "
    "user will have approved and the vault will still be empty — and the first ask then "
    "repeats forever with no error naming the cause.\n"
    "GITHUB IS ON THIS FLOW BECAUSE IT HAS TO BE, not by choice: its token endpoint "
    "answers the M3 token-exchange grant with unsupported_grant_type. A brokered exchange "
    "needs the SOURCE to accept a brokered assertion, and SSO into a vendor is not that.\n"
    "THE GUARANTEE IS THE SAME ONE THE DELEGATION FLOW MAKES. Edge 22 calls GitHub with a "
    "token representing the USER, so GitHub itself trims the results. The two flows differ "
    "in who consents and who holds a long-lived credential — never in who the source "
    "thinks is asking.\n"
    "WHICH FLOW A SOURCE USES IS CONFIGURATION, NOT CODE — <SOURCE>_AUTH_FLOW, resolved "
    "per source by agent.py _flow_for(). That variable selects M1 (USER_FEDERATION) or M3 "
    "(ON_BEHALF_OF_TOKEN_EXCHANGE); it cannot select the delegation flow, which is chosen "
    "by a source's `credential` axis instead. Edge 20 is the call site that does not "
    "change either way.")

# ---------------------------------------------------------------------------
# The three specs. `slug` picks the accent and names the file; `doc_heading` is the
# ARCHITECTURE.md section whose mermaid block must carry the same edge numbers.
# ---------------------------------------------------------------------------
SPECS = [
    dict(slug="deploy", cols=DEPLOY_COLS, edges=DEPLOY_EDGES, bands=DEPLOY_BANDS,
         phases=DEPLOY_PHASES, note=DEPLOY_NOTE,
         flow="DEPLOY FLOW",
         title="Ripple — DEPLOY FLOW: how the system gets built and deployed",
         subtitle="Starts at an operator shell, ends at a live tagged runtime. One row "
                  "per numbered edge; the numbers match the deploy-flow edge table in "
                  "infra/ARCHITECTURE.md. Read top to bottom.",
         out="architecture-deploy-flow.png",
         doc_heading="Deploy flow"),
    dict(slug="delegation", cols=DELEG_COLS, edges=DELEG_EDGES, bands=DELEG_BANDS,
         phases=DELEG_PHASES, note=DELEG_NOTE,
         flow="DELEGATION FLOW  (M2)   —   no consent screen, ever",
         title="Ripple — DELEGATION FLOW (M2): answering from Google Drive with no "
               "consent screen",
         subtitle="Starts and ends at the user, in ONE invocation. The verified email "
                  "claim becomes the subject of a service-account assertion, so Google "
                  "returns a token scoped to that one user and Drive trims the results "
                  "itself. NOT a token exchange: Google requires an assertion signed "
                  "with a key we hold, which is why this can never become M3.",
         out="architecture-delegation-flow.png",
         doc_heading="Delegation flow"),
    dict(slug="consent", cols=CONSENT_COLS, edges=CONSENT_EDGES, bands=CONSENT_BANDS,
         phases=CONSENT_PHASES, note=CONSENT_NOTE,
         flow="CONSENT FLOW   —   one click per user, per source",
         title="Ripple — CONSENT FLOW: answering from GitHub after one consent click",
         subtitle="Starts and ends at the user, but across TWO invocations, because a "
                  "human acts in the middle and the runtime does not wait for one. The "
                  "guarantee at the end is identical to the delegation flow's.",
         out="architecture-consent-flow.png",
         doc_heading="Consent flow"),
]

# --- font sizes (points) ---------------------------------------------------
FS_TITLE, FS_SUB = 13.0, 7.4
FS_FLOW = 9.0
FS_HDR, FS_KIND, FS_BAND = 6.5, 4.8, 6.8
FS_PHASE, FS_EDGE, FS_NUM = 6.6, 6.5, 6.8
FS_MARK = 6.6
FS_NOTE, FS_LEG = 6.8, 6.5

DPI = 170
# Opaque white pad drawn behind label text so the dashed lifelines crossing the
# row do not strike through the glyphs.
MASK = dict(boxstyle="square,pad=0.14", facecolor="white", edgecolor="none")
M = 0.24            # figure margin, inches
BOX_PAD = 0.09      # padding inside a participant box, inches
COL_GAP = 0.11      # gap between participant boxes, inches
NUM_R = 0.095       # radius of a numbered circle, inches
CLEAR = 0.05        # generic clearance, inches
MARK_H = 0.24       # vertical slot reserved for a START / END badge, inches
DOT_R = 0.030       # radius of the departure dot drawn on the tail lifeline


class Ruler:
    """Measures rendered text in inches. Data units are inches, so the numbers
    it returns are directly usable as layout geometry."""

    def __init__(self):
        self.fig = plt.figure(figsize=(8, 8), dpi=DPI)
        self.rend = self.fig.canvas.get_renderer()

    def size(self, s, fontsize, weight="normal", style="normal"):
        """(width, height) in inches of a single line."""
        t = self.fig.text(0, 0, s, fontsize=fontsize, weight=weight, style=style)
        bb = t.get_window_extent(self.rend)
        t.remove()
        return bb.width / DPI, bb.height / DPI

    def block(self, s, fontsize, weight="normal", linespacing=1.35):
        """(width, height) in inches of a possibly multi-line string."""
        lines = s.split("\n")
        w = max(self.size(ln, fontsize, weight)[0] for ln in lines)
        line_h = self.size("Ag", fontsize, weight)[1]
        return w, line_h * (1 + linespacing * (len(lines) - 1))

    def line_h(self, fontsize, weight="normal"):
        return self.size("Ag", fontsize, weight)[1]

    def close(self):
        plt.close(self.fig)


def _draw(spec, out):
    """Sequence diagram: participants are columns, numbered edges are rows.

    Two passes. Pass 1 measures every string and derives the geometry; pass 2
    draws at those coordinates. Nothing is hand-positioned, so editing a label
    cannot introduce an overlap.

    Returns (path, overlaps, geom). `geom` is the per-edge endpoint record that
    check_geometry() asserts against, so "arrows land on lifelines" is verified from
    the coordinates actually drawn rather than from reading this function.
    """
    cols, edges = spec["cols"], spec["edges"]
    bands, phases, note = spec["bands"], spec["phases"], spec["note"]
    accent = ACCENT[spec["slug"]]

    r = Ruler()
    kind = {key: k for key, _, k in cols}
    order = [key for key, _, _ in cols]
    label_of = {key: lbl for key, lbl, _ in cols}

    # ---- pass 1a: column widths from the widest header line -----------------
    hdr_line_h = r.line_h(FS_HDR)
    kind_h = r.line_h(FS_KIND)
    col_w, hdr_lines = {}, {}
    for key, lbl, _ in cols:
        w, _ = r.block(lbl, FS_HDR)
        col_w[key] = max(0.62, w + 2 * BOX_PAD)
        hdr_lines[key] = len(lbl.split("\n"))
    max_lines = max(hdr_lines.values())
    hdr_h = kind_h + max_lines * hdr_line_h * 1.2 + 2 * BOX_PAD

    gutter_w = max(r.size(lbl, FS_PHASE, "bold")[0] for _, lbl in phases) + 0.14
    x_left = M + gutter_w
    cx, cursor_x = {}, x_left
    for key in order:
        cx[key] = cursor_x + col_w[key] / 2
        cursor_x += col_w[key] + COL_GAP
    cols_right = cursor_x - COL_GAP

    # ---- pass 1b: vertical stack, accumulating depth from the top ----------
    title_w, title_h = r.block(spec["title"], FS_TITLE, "bold")
    sub_w, sub_h = r.block(spec["subtitle"], FS_SUB)
    flow_w, flow_h = r.size(spec["flow"], FS_FLOW, "bold")
    band_h = r.line_h(FS_BAND, "bold")
    edge_h = r.line_h(FS_EDGE)
    phase_h = r.line_h(FS_PHASE, "bold")

    d = 0.0                                  # depth below the top margin
    d += title_h
    d += 0.07
    sub_d = d
    d += sub_h + 0.14
    flow_d = d                               # the flow-name banner
    banner_h = flow_h + 2 * 0.055
    d += banner_h + 0.20
    band_label_d = d                         # top of the band label
    d += band_h + 0.04
    bracket_d = d                            # bracket top rail
    d += 0.12
    hdr_top_d = d
    d += hdr_h + 0.12

    phase_start = dict(phases)
    y_arrow_d, sep_d, phase_label_d = {}, {}, {}
    start_mark_d = end_mark_d = None
    last_i = len(edges) - 1
    for i, (src, dst, num, lbl, style) in enumerate(edges):
        if num in phase_start:
            d += phase_h + 0.05
            sep_d[num] = d
            phase_label_d[num] = d - 0.03    # label sits just above the rule
            d += 0.09
        if i == 0:                           # slot for the START badge
            start_mark_d = d + MARK_H / 2
            d += MARK_H
        d += edge_h                          # the edge label
        d += CLEAR + NUM_R
        y_arrow_d[num] = d
        d += NUM_R + 0.13
        if i == last_i:                      # slot for the END badge
            end_mark_d = d + MARK_H / 2
            d += MARK_H
    rows_bottom_d = d

    note_w, note_h = r.block(note, FS_NOTE, linespacing=1.5)
    d += 0.16
    note_d = d
    d += note_h + 0.13
    used = [e for e in LEGEND if e[0] in set(kind.values())]
    leg_line = r.line_h(FS_LEG) * 1.65
    leg_d = d
    d += leg_line * len(used)
    content_h = d

    # ---- figure size, then convert depths to y ----------------------------
    leg_w = max(r.size(f"[{k}]  {desc}", FS_LEG)[0] for k, desc in used) + 0.50
    W = max(cols_right, M + max(title_w, sub_w, note_w, leg_w, flow_w + 0.6)) + M
    H = content_h + 2 * M
    r.close()

    def Y(depth):
        return H - M - depth

    fig = plt.figure(figsize=(W, H), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")
    ax.set_facecolor("white")

    ax.text(M, Y(0), spec["title"], fontsize=FS_TITLE, weight="bold", va="top")
    ax.text(M, Y(sub_d), spec["subtitle"], fontsize=FS_SUB, color="#555555",
            va="top", linespacing=1.35)

    # The flow-name banner. The filename says which flow this is; so must the
    # canvas, so that a screenshot pasted into a deck still says which one.
    ax.add_patch(FancyBboxPatch(
        (M, Y(flow_d + banner_h)), flow_w + 0.34, banner_h,
        boxstyle="round,pad=0,rounding_size=0.05",
        facecolor=accent, edgecolor="none", zorder=3))
    ax.text(M + 0.17, Y(flow_d + banner_h / 2), spec["flow"], fontsize=FS_FLOW,
            weight="bold", color="white", va="center", ha="left", zorder=4)

    # Column brackets. Shrink the label if it is wider than the span it labels.
    for a, b, lbl, colour in bands:
        xa = cx[a] - col_w[a] / 2
        xb = cx[b] + col_w[b] / 2
        y_top, y_bot = Y(bracket_d), Y(bracket_d + 0.12)
        ax.plot([xa, xa, xb, xb], [y_bot, y_top, y_top, y_bot],
                color=colour, linewidth=1.2, solid_capstyle="butt")
        fs = FS_BAND
        rr = Ruler()
        while fs > 4.6 and rr.size(lbl, fs, "bold")[0] > (xb - xa) - 0.06:
            fs -= 0.2
        rr.close()
        ax.text((xa + xb) / 2, Y(band_label_d), lbl, ha="center", va="top",
                fontsize=fs, style="italic", weight="bold", color=colour)

    lifeline_bottom = Y(rows_bottom_d + 0.05)
    for key in order:
        k, x = kind[key], cx[key]
        ax.add_patch(FancyBboxPatch(
            (x - col_w[key] / 2, Y(hdr_top_d + hdr_h)), col_w[key], hdr_h,
            boxstyle="square,pad=0", facecolor=FILL[k], edgecolor=LINE[k],
            linewidth=1.2, zorder=3))
        ax.text(x, Y(hdr_top_d + BOX_PAD * 0.6), k, ha="center", va="top",
                fontsize=FS_KIND, color=LINE[k], weight="bold", zorder=4)
        ax.text(x, Y(hdr_top_d + hdr_h / 2 + kind_h * 0.35), label_of[key],
                ha="center", va="center", fontsize=FS_HDR, zorder=4,
                linespacing=1.35)
        ax.plot([x, x], [Y(hdr_top_d + hdr_h + 0.02), lifeline_bottom],
                color=LINE[k], linewidth=0.8, linestyle=(0, (2, 3)),
                alpha=0.55, zorder=1)

    for num, lbl in phases:
        y = Y(sep_d[num])
        ax.plot([M, cols_right], [y, y], color="#c2c2c2", linewidth=0.7,
                linestyle=(0, (5, 4)), zorder=1)
        ax.text(M, Y(phase_label_d[num]), lbl, ha="left", va="bottom",
                fontsize=FS_PHASE, weight="bold", color="#666666", zorder=5,
                bbox=MASK)

    # ---- the numbered edges ------------------------------------------------
    # Endpoints are the lifeline x-coordinates EXACTLY: an arrow departs from the
    # centre line of its source participant and its head terminates on the centre
    # line of its destination, so which two participants a step runs between is
    # unambiguous. A dot marks the tail, distinguishing it from the head without
    # an activation box. Recorded in `geom` and asserted by check_geometry().
    geom = []
    rr = Ruler()
    for src, dst, num, lbl, style in edges:
        y = Y(y_arrow_d[num])
        sx, ex = cx[src], cx[dst]
        colour = LINE[kind[src]]
        ax.add_patch(FancyArrowPatch(
            (sx, y), (ex, y), arrowstyle="-|>", mutation_scale=11,
            linewidth=1.25, color=colour, zorder=4,
            linestyle="dashed" if style == ":" else "solid",
            shrinkA=0, shrinkB=0))
        ax.add_patch(Circle((sx, y), DOT_R, facecolor=colour, edgecolor="white",
                            linewidth=0.6, zorder=5))
        ax.text((sx + ex) / 2, y, str(num), ha="center", va="center",
                fontsize=FS_NUM, weight="bold", color="white", zorder=6,
                bbox=dict(boxstyle="circle,pad=0.22", facecolor=colour,
                          edgecolor="white", linewidth=0.8))
        # Left-anchor at the arrow tail so text runs into open space, then pull
        # it left if it would overrun the last column.
        lw = rr.size(lbl, FS_EDGE)[0]
        lx = min(min(sx, ex), cols_right - lw)
        # An opaque background so the dashed lifelines behind do not strike
        # through the text. The overlap detector measures the glyphs, not this
        # mask, so it stays a purely visual fix.
        ax.text(max(M, lx), Y(y_arrow_d[num] - NUM_R - CLEAR), lbl,
                ha="left", va="bottom", fontsize=FS_EDGE, color="#2b2b2b",
                zorder=5, bbox=MASK)
        geom.append(dict(num=num, src=src, dst=dst, sx=sx, ex=ex,
                         src_x=cx[src], dst_x=cx[dst]))
    rr.close()

    # ---- START and END badges, on the lifelines they belong to -------------
    # Drawn ON the first edge's source lifeline and the last edge's destination
    # lifeline, so "where does this flow begin and end" is answered by a glyph in
    # the right column rather than by counting rows.
    first_src, last_dst = edges[0][0], edges[-1][1]
    sy = Y(start_mark_d)
    ax.add_patch(Polygon([[cx[first_src] - 0.055, sy + 0.058],
                          [cx[first_src] - 0.055, sy - 0.058],
                          [cx[first_src] + 0.068, sy]],
                         closed=True, facecolor=accent, edgecolor="white",
                         linewidth=0.7, zorder=6))
    ax.text(cx[first_src] + 0.12, sy, f"START  ({first_src})", ha="left",
            va="center", fontsize=FS_MARK, weight="bold", color=accent,
            zorder=6, bbox=MASK)
    ey = Y(end_mark_d)
    ax.add_patch(Rectangle((cx[last_dst] - 0.053, ey - 0.053), 0.106, 0.106,
                           facecolor=accent, edgecolor="white", linewidth=0.7,
                           zorder=6))
    ax.text(cx[last_dst] + 0.12, ey, f"END  ({last_dst})", ha="left",
            va="center", fontsize=FS_MARK, weight="bold", color=accent,
            zorder=6, bbox=MASK)

    ax.text(M, Y(note_d), note, fontsize=FS_NOTE, color="#444444", va="top",
            linespacing=1.5)
    for i, (k, desc) in enumerate(used):
        ly = Y(leg_d + leg_line * (i + 0.5))
        ax.add_patch(FancyBboxPatch((M, ly - 0.055), 0.30, 0.11,
                                    boxstyle="square,pad=0",
                                    facecolor=FILL[k], edgecolor=LINE[k],
                                    linewidth=1.0, zorder=3))
        ax.text(M + 0.40, ly, f"[{k}]  {desc}", fontsize=FS_LEG, va="center",
                color="#444444")

    overlaps = _overlaps(fig, ax)
    fig.savefig(out, facecolor="white")
    plt.close(fig)
    return out, overlaps, geom


def _overlaps(fig, ax):
    """Every pair of text objects whose rendered boxes intersect.

    Boxes are shrunk slightly first so glyphs that merely touch (normal for
    adjacent lines) are not reported.
    """
    rend = fig.canvas.get_renderer()
    items = []
    for t in ax.texts:
        bb = t.get_window_extent(rend).expanded(0.97, 0.80)
        items.append((t.get_text().replace("\n", " / ")[:46], bb))
    hits = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i][1].overlaps(items[j][1]):
                hits.append((items[i][0], items[j][0]))
    return hits


def check_geometry(slug, geom) -> list:
    """Every arrow must begin and end ON a participant's lifeline.

    This check exists because the property is easy to lose and invisible in code
    review. A previous version offset both endpoints outward by half a participant
    box, so an arrow between adjacent columns rendered as a stub in the gutter,
    touching neither lifeline: the diagram drew motion between two things without
    showing which two. Asserting the drawn coordinates makes that unrepeatable.
    """
    bad = []
    for g in geom:
        if abs(g["sx"] - g["src_x"]) > 1e-9:
            bad.append(f"edge {g['num']}: tail is {g['sx'] - g['src_x']:+.3f}in off "
                       f"the {g['src']} lifeline")
        if abs(g["ex"] - g["dst_x"]) > 1e-9:
            bad.append(f"edge {g['num']}: head is {g['ex'] - g['dst_x']:+.3f}in off "
                       f"the {g['dst']} lifeline")
        if g["src"] == g["dst"]:
            bad.append(f"edge {g['num']}: self-edge on {g['src']} would render as a "
                       f"zero-length arrow")
    return bad


def check_endpoints() -> int:
    """Each flow must have exactly one start and one end, and be contiguous.

    The two request flows must also begin and end at the SAME participant, because
    "the person who asked gets an answer" is the claim each of them makes. The deploy
    flow is exempt: no end user takes part in it.
    """
    ok = True
    for spec in SPECS:
        edges, slug = spec["edges"], spec["slug"]
        nums = [e[2] for e in edges]
        problems = []
        if nums != list(range(1, len(nums) + 1)):
            problems.append(f"numbering is not contiguous 1..{len(nums)}: {nums}")
        if not edges[0][3].startswith("START"):
            problems.append("edge 1's label does not begin with START")
        if not edges[-1][3].startswith("END"):
            problems.append(f"edge {nums[-1]}'s label does not begin with END")
        if slug != "deploy" and edges[0][0] != edges[-1][1]:
            problems.append(f"starts at {edges[0][0]} but ends at {edges[-1][1]}; a "
                            f"request flow must return to whoever asked")
        if problems:
            ok = False
            print(f"  {slug}: ENDPOINT PROBLEMS")
            for p in problems:
                print(f"    {p}")
        else:
            print(f"  {slug}: {len(nums)} edges, START at {edges[0][0]}, "
                  f"END at {edges[-1][1]}")
    return 0 if ok else 1


def _doc_blocks() -> dict:
    """{heading: mermaid block} for every `##` section of ARCHITECTURE.md."""
    with open(DOC, encoding="utf-8") as fh:
        txt = fh.read()
    out = {}
    for chunk in re.split(r"\n##\s+", txt)[1:]:
        heading = chunk.split("\n", 1)[0].strip()
        m = re.search(r"```mermaid\n(.*?)```", chunk, re.S)
        if m:
            out[heading] = m.group(1)
    return out


def check_edges() -> int:
    """Fail if the PNG edge numbering drifts from ARCHITECTURE.md.

    Matched by SECTION HEADING rather than by position, so adding a section to the
    doc cannot silently pair a diagram with the wrong edge table.
    """
    blocks = _doc_blocks()
    ok = True
    for spec in SPECS:
        want = spec["doc_heading"].lower()
        found = [h for h in blocks if want in h.lower()]
        if not found:
            ok = False
            print(f"  {spec['slug']}: NO SECTION of ARCHITECTURE.md whose heading "
                  f"contains {spec['doc_heading']!r} has a mermaid block")
            continue
        block = blocks[found[0]]
        doc_nums = sorted(int(v) for v in re.findall(r'\|"(\d+)[^"]*"\|', block))
        png_nums = sorted(e[2] for e in spec["edges"])
        if doc_nums == png_nums:
            print(f"  {spec['slug']}: {len(png_nums)} edges match "
                  f"ARCHITECTURE.md § {found[0]!r}")
        else:
            ok = False
            print(f"  {spec['slug']}: MISMATCH against § {found[0]!r}")
            print(f"    only in ARCHITECTURE.md: {sorted(set(doc_nums) - set(png_nums))}")
            print(f"    only in renderer:        {sorted(set(png_nums) - set(doc_nums))}")
    return 0 if ok else 1


def main() -> int:
    check_only = "--check" in sys.argv
    rc = 0

    print("flow endpoints (one START, one END, contiguous numbering):")
    rc |= check_endpoints()

    if not check_only:
        for spec in SPECS:
            path, overlaps, geom = _draw(spec, os.path.join(HERE, spec["out"]))
            print(f"wrote {os.path.relpath(path, os.path.dirname(HERE))} "
                  f"({os.path.getsize(path) // 1024} KB)")
            if overlaps:
                rc = 1
                print(f"  {len(overlaps)} OVERLAPPING TEXT PAIR(S):")
                for a, b in overlaps:
                    print(f"    {a!r}  <->  {b!r}")
            else:
                print("  no overlapping text")
            bad = check_geometry(spec["slug"], geom)
            if bad:
                rc = 1
                print(f"  {len(bad)} ARROW GEOMETRY FAILURE(S):")
                for b in bad:
                    print(f"    {b}")
            else:
                print(f"  all {len(geom)} arrows begin and end on a lifeline")

    print("edge-number parity with ARCHITECTURE.md:")
    return check_edges() or rc


if __name__ == "__main__":
    raise SystemExit(main())
