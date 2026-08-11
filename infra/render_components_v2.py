#!/usr/bin/env python3
"""Component architecture diagram for the Ripple solution — AWS-icon style.

Companion to render_diagrams.py. That file renders the *flows* (deploy, OBO,
consent — one diagram each) as sequence diagrams; this one renders the
*components* and how they connect, as service tiles with labelled connections —
the conventional shape for an architecture diagram, and the one a reader can take
in at a glance without tracing a sequence.

WHAT THIS DIAGRAM IS FOR. Not "here are our boxes" — it exists to make one claim
legible: a user's identity reaches a third-party API without that user ever seeing
a consent screen, and the third party still does the authorization. Everything in
the layout serves that. The primary path is the thick horizontal spine through
row 2; the consent path (GitHub) is drawn OFF the spine and tagged EXTENSION,
because an earlier version put it inline and made consent look mandatory.

The two hops to Google are drawn as two separate arrows on purpose. Okta cannot
mint a token Drive accepts, so hop 1 is cryptographic validation and hop 2 is
admin-delegated impersonation. One arrow would assert a single credential spans
both authorities. It does not — see the NOTE block, which says so in the PNG
itself rather than relying on whoever presents it to remember.

Tiles not built yet are dashed and tagged TARGET. What is actually deployed is
described in README.md, which is the single source of truth for build state.

Every tile carries an official icon (see icons/SOURCE.md): the AgentCore family
from the AgentCore deck, the AWS services from the AWS Architecture Icons
package, and Slack / GitHub / Okta / Google from their own brand marks. Nothing is
approximated — a borrowed icon asserts the wrong service. Where no official icon
can be legitimately obtained, the tile is AUTHORED as a short text glyph instead;
that is honest, and it keeps missing_art() strict so a typo'd filename still fails
the render rather than shipping a broken image reference.

ONE ARROW IS A HUMAN, AND IT BELONGS TO THE CONSENT FLOW ALONE. Everything else on
this canvas is software calling software, so the consent click (End user -> GitHub
OAuth App, badged ★) is drawn arcing over the whole diagram. It answers the question
the numbering alone cannot: WHERE DOES A PERSON HAVE TO ACT. The OBO flow has no
counterpart to it at any step, which is the claim in the title, shown rather than
asserted — so the prose must never say "the one human action in EITHER flow", which
reads as one apiece and concedes exactly what the diagram is arguing. It is
deliberately unnumbered — see that edge's comment, where a step number made
flow_numbering() fail for a good reason.

Layout is measured, not hand-tuned, and these checks fail the render rather than
letting a wrong diagram ship:

    account_id_leaks()          no AWS account ID baked into a shareable PNG
    trust_boundary_violations() nothing sits on the wrong side of the AWS boundary
    zone_intrusions()           no zone box visually contains a non-member tile
    zone_overlaps()             no two trust boundaries drawn crossing each other
    flow_numbering()            the step numbers form a sequence a reader can walk
    arrows_through_tiles()      no arrow crosses a component it does not connect
    missing_glyphs()            no tile glyph absent from the render font

WHAT THE CHECKS CANNOT SEE, so read the PNG after changing anything: whether a badge
sits on the arrow it belongs to, and whether a label sits near the edge it describes.
Labels are anchored to the STRAIGHT-LINE midpoint while badges use the curve's, so on
a strongly bowed edge the two separate by inches — the consent arc's label first
landed beside Bedrock, annotating nothing, with every check green.

MEASURE WHAT IS PAINTED, NOT WHAT IS WRITTEN. A step badge is two or three characters
inside an opaque circle half again their size, so _overlaps() reading glyph extents
declared '10b' clear of the MCP server's caption by 1.3px while its circle covered
that caption by 8.5px. Both defects that survived to the shipped PNG were of this
shape. The fix is _painted_box(), and the general rule it encodes: if an element
paints something bigger than its text, the check must ask the renderer how big.

Data coordinates are INCHES.

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):

This file IS the diagram — it is the single source of truth every other file's
`WHERE THIS SITS` comment refers back to. NODES names the tiles (USER, CLI, SLACK,
OKTA, IGW, RT, BR, MEM, TGW, VAULT, MCP, SM, GDRIVE, GOOGLE, GH, GHAPP, S3, CB, ECR,
CW) and EDGES carries the step numbers, so those two structures are the authoritative
component vocabulary: a comment elsewhere naming a tile or a step that does not appear
here is stale by definition, and grepping NODES/EDGES is how to check.

It is not on either flow and creates nothing. Its output is read by humans, which is
exactly why it refuses to write a PNG that fails its checks — a diagram that
contradicts the code is worse than no diagram, because it will be believed.

The step tables under each flow are DERIVED from EDGES by steps_for(), so a badge and
its description cannot drift apart. That property is the reason to change wording here
rather than in the tables: there are no tables to change.
"""
import math
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.image as mpimg                     # noqa: E402
import matplotlib.pyplot as plt                      # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "architecture-components-v2.png")
ICONS = os.path.join(HERE, "icons")

DPI = 170
# AWS-console-ish service category colours, to read like the customer's diagram.
CAT = {
    "ml":       "#01a88d",   # teal   — Bedrock
    "agent":    "#7b27ff",   # purple — AgentCore (matches the official icon set)
    "security": "#dd344c",   # red    — secrets
    "storage":  "#7aa116",   # green  — S3 / ECR
    "mgmt":     "#e7157b",   # pink   — CloudWatch
    "devtools": "#c925d1",   # purple — CodeBuild
    "ext":      "#5a6b7b",   # slate  — outside AWS
    "user":     "#232f3e",   # navy   — people
    "google":   "#4285f4",   # blue   — Google Workspace / Drive
    "code":     "#1f7a8c",   # dark cyan — our own code, not a managed service
}
TILE = 0.74          # icon tile side, inches
COL_W = 2.15         # caption wrap width, inches
FS_GLYPH, FS_CAP, FS_SUB = 10.0, 7.4, 6.5
FS_EDGE, FS_ZONE, FS_TITLE, FS_NOTE = 6.4, 7.0, 13.5, 7.0
FS_TAG = 5.6

# Per-status rendering. A tile's status says whether it EXISTS, and separately
# whether it is on the primary path — those are different questions and an earlier
# version of this diagram conflated them, drawing the GitHub consent path as if it
# were the headline. It is built and working; it is simply not the path being
# demonstrated.
#
#   built      exists, on the primary path            solid, no tag
#   extension  exists, deliberately NOT the main path  solid, EXTENSION tag
#   target     does not exist yet                      dashed, TARGET tag
#   deferred   not built, and not next                 dashed, DEFERRED tag
STATUS = {
    "built":     (False, None,        None),
    "extension": (False, "EXTENSION", "#c8791d"),
    "target":    (True,  "TARGET",    "#7b27ff"),
    "deferred":  (True,  "DEFERRED",  "#5a6b7b"),
}
MASK = dict(boxstyle="square,pad=0.12", facecolor="white", edgecolor="none")

# --- grid ------------------------------------------------------------------
# Three constraints drive the placement:
#
#  1. TRUST BOUNDARY = COLUMNS. Cols 0-1 are outside AWS (the user, their
#     laptop, Okta); cols 2-5 are inside the AWS account; col 6 is outside AWS
#     (Google Workspace, GitHub). No external component may sit in cols 2-5 and
#     no AWS resource in cols 0-1 or 6 — otherwise the dashed account boundary
#     would be a lie, and that boundary is the whole point of this architecture.
#
#  2. ROW 2 IS THE PRIMARY REQUEST SPINE, read left to right: user -> front door
#     -> runtime -> claim verification -> Drive. This is the NO-CONSENT path, and
#     it is deliberately the straight horizontal line through the middle of the
#     diagram, because it is the only path being demonstrated.
#
#  3. ROW 0 IS THE CONSENT EXTENSION (vault -> GitHub OAuth App). It is drawn
#     ABOVE the spine and tagged EXTENSION so it reads as an alternative, not as
#     a step. Previously it sat on the spine, which made the consent flow look
#     mandatory — the single most misleading thing this diagram could say.
#
#  Cells on a line between two connected tiles are left empty;
#  arrows_through_tiles() enforces it and any unavoidable span is bowed instead.
#
#  COLUMN 1 -> 2 IS THE WIDEST GAP ON PURPOSE. It is where the trust boundary runs
#  (user's machine + IdP | AWS account), and the two zone rectangles have to be
#  visibly separated there. They were not: Okta's caption is wider than its tile, so
#  the "Outside AWS" box reached 4.73in while the "AWS account" box started at
#  4.63in — a 0.10in overlap sustained over 6.9in of height, which draws one trust
#  boundary passing THROUGH another. Nothing flagged it (both zones' tiles were
#  correctly assigned; only the boxes collided) and it is easy to miss by eye at
#  full-canvas zoom. zone_overlaps() now fails the render on it.
#
#  V2 ADDS THREE SOURCES TO COLUMN 6 AND DOES NOT ADD A COLUMN, which is the whole
#  reason the trust boundary survives the change: every source a user's identity
#  reaches is outside the AWS account, so they all belong in the one external
#  column, stacked. Putting the new ones in a col 7 would have read as a second,
#  lesser tier of source — and they are not architecturally lesser, they are simply
#  not built yet, which is what the dashed tile and the TARGET/DEFERRED tag say.
#
#  COLUMN 6 IS GROUPED BY MECHANISM, TOP TO BOTTOM, and the grouping is the argument:
#     rows 0-1  the CONSENT mechanism, exemplified by GitHub (built)
#     rows 2-3  the NO-CONSENT / OBO mechanism, exemplified by Google Drive (built)
#     rows 4-6  the three sources not yet built, which reuse those same two
#               mechanisms rather than needing a third
#  Each group gets its own zone box, so a reader counts five sources and two
#  mechanisms rather than five unrelated integrations.
CX = {0: 1.20, 1: 3.55, 2: 6.90, 3: 9.60, 4: 12.30, 5: 15.00, 6: 18.20}
# Row 6 is new in v2 and sits BELOW row 5. Its y is negative in body coordinates,
# which is fine: body_dy (the note + legend + step tables) shifts the whole body up
# by several inches before anything is painted. The canvas height is measured from
# the drawn extents, so this is checked rather than assumed — see draw().
CY = {0: 12.10, 1: 9.85, 2: 7.05, 3: 4.95, 4: 2.70, 5: 0.85, 6: -1.05}

# key: (col, row, icon-or-glyph, caption, sub-caption, category, status)
# icon: "file.png" resolves under icons/; anything else is drawn as a glyph.
# status: see STATUS above.
NODES = {
    # --- outside AWS: the caller and the IdP (cols 0-1) ---
    "USER":   (0, 2, "user.png", "End user", "Okta-managed employee",
               "user", "built"),
    "SLACK":  (0, 1, "slack.png", "Slack front door", "needs Business+ plan",
               "ext", "deferred"),
    "CLI":    (1, 2, "laptop.png", "CLI client", "client/login.py, client/ask.py",
               "user", "built"),
    # THE ONLY IDENTITY IN THE SYSTEM. Every per-user decision downstream traces
    # back to a token this component signed — which is why it sits alone at the
    # top and why two separate arrows point at it (inbound validation, and the
    # OBO exchange).
    "OKTA":   (1, 0, "okta.png", "Okta (OIDC IdP)",
               "device-code flow · JWKS · email claim = Workspace primary",
               "ext", "built"),

    # --- inside the AWS account (cols 2-5) ---
    # Inbound auth (JWT validation against the IdP's JWKS) belongs to whichever
    # component is the front door. In the target that is the ingress gateway;
    # in today's MVP it is the runtime's own customJWTAuthorizer. See NOTE.
    "IGW":    (2, 2, "gateway.png", "Ingress AgentCore Gateway",
               "target: Runtime · inbound auth: validate Okta JWT · A/B traffic",
               "agent", "target"),
    "CW":     (2, 0, "observability.png", "CloudWatch / AgentCore Observability",
               "runtime + build logs, traces, tagged", "mgmt", "built"),
    # The endpoint is NOT a separate component — it is an addressable alias on
    # the runtime, so it is a sub-caption here rather than its own tile.
    #
    # identity_claims.py and gdrive_tool.py are deliberately NOT separate tiles:
    # they are modules inside this container, and drawing them as peers of managed
    # services would imply they are deployable components. What they DO is carried
    # by the edge labels instead — which is where it belongs, because the
    # load-bearing fact is not "a module exists" but "the subject on that wire was
    # cryptographically verified".
    "RT":     (3, 2, "runtime.png", "AgentCore Runtime",
               "agent.py Strands harness, ARM64 · verifies the Okta JWT itself "
               "before impersonating anyone", "agent", "built"),
    # "Claude, model from BEDROCK_MODEL_ID" rather than naming a version: nothing in
    # the code pins one. agent.py reads the env var, whose CFN default merely happens to
    # be an Opus 4.8 inference profile today. A caption that names a version is wrong
    # the first time anyone overrides the parameter, and wrong silently.
    "BR":     (3, 0, "bedrock.png", "Amazon Bedrock",
               "Claude · model from BEDROCK_MODEL_ID", "ml", "built"),
    # CREATED BUT NOT READ, and the caption has to say so. It previously read
    # "STM, actorId = Okta sub, 90d", which describes a design nobody implemented: the
    # runtime stack creates the resource and injects AGENTCORE_MEMORY_ID, and agent.py
    # then never calls it — no actorId is ever set, no event is ever written. A tile
    # captioned with the partitioning scheme it WOULD use reads as a deployed
    # capability, so a reviewer checking "is conversation history per-user?" gets a yes
    # from the diagram and a no from the code. Provisioned-and-unused is the honest
    # state, and it is deliberate (see invoke()'s STATEFUL recipe in agent/agent.py).
    "MEM":    (3, 3, "memory.png", "AgentCore Memory",
               "provisioned, NOT read — stateless today; see agent.py invoke()",
               "agent", "built"),
    "TGW":    (4, 2, "gateway.png", "Tools AgentCore Gateway",
               "targets: MCP · outbound auth: OAuth on-behalf-of", "agent", "target"),
    "VAULT":  (5, 2, "identity.png", "AgentCore Identity token vault",
               "brokers per-user tokens — replaces DynamoDB + KMS", "agent",
               "built"),
    "SM":     (4, 1, "secrets-manager.png", "Secrets Manager",
               "Google service-account key (domain-wide) · GitHub client secret",
               "security", "built"),
    # LAYER 1 — the piece that makes the propagation claim cryptographic rather
    # than narrated. Guarded by Okta, so reaching it requires a real Okta token
    # for THIS user, obtained by RFC 8693 exchange with no consent screen.
    #
    # PLACEMENT IS CONSTRAINED FROM BOTH SIDES, and two obvious cells are wrong.
    # (5,4) put it in the build plane's row, where it read as a CI step — something
    # that SHIPS the agent rather than something the agent calls at answer time.
    # (5,3) then landed inside the AgentCore zone's bounding box, which asserts the
    # opposite error: this server is ours to run, not a managed AgentCore component.
    # zone_intrusions() caught the second; only reading the PNG caught the first.
    # (5,1) satisfies both — inside the AWS account, outside the managed box, and in
    # VAULT's column, beside the vault that mints the token it validates.
    "MCP":    (5, 1, "mcp.png", "Okta-guarded MCP server",
               "Layer 1: validates an Okta token minted FOR IT, for this user",
               "code", "target"),
    "S3":     (2, 4, "s3.png", "S3 build source", "dedicated, TLS-only, versioned",
               "storage", "built"),
    "CB":     (3, 4, "codebuild.png", "CodeBuild", "ARM64 image builder",
               "devtools", "built"),
    "ECR":    (4, 4, "ecr.png", "ECR repository", "immutable tags, scan-on-push",
               "storage", "built"),

    # --- outside AWS: Google Workspace and GitHub (col 6) ---
    # THE PRIMARY SOURCE. Reached with no consent screen, and still ACL-trimmed by
    # Google, because the token minted for it names this user and nobody else.
    "GDRIVE": (6, 2, "google-drive.png", "Google Drive / Docs",
               "PRIMARY source — ACL-trimmed by Google itself", "google", "built"),
    # IAM, not a service-account icon: Google publishes none (all three of its icon
    # packages were searched). IAM is the service that issues and governs the service
    # account, so it is the honest label. The near-misses were worse than a glyph:
    # `permissions` is a person bust (asserts a USER, the opposite of a service
    # account) and `workload_identity_pool` denotes the KEYLESS alternative to the
    # key this design actually uses — it would assert the opposite architecture.
    "GSTS":   (6, 3, "google-cloud-iam.png", "Google OAuth2 token endpoint",
               "the ONLY issuer Drive accepts — not Okta", "google", "built"),
    "GH":     (6, 1, "github.png", "GitHub MCP / API",
               "code, issues, PRs — user-scoped", "ext", "extension"),
    "GHAPP":  (6, 0, "github.png", "GitHub OAuth App",
               "the one place a user still sees a consent screen", "ext",
               "extension"),

    # --- SOURCES 3-5: not built, and the point of drawing them is that they need
    # no new identity machinery. Each is tagged with the mechanism it will reuse
    # (OBO or CONSENT), because "which of the two paths above does this take" is
    # the only question a reader needs answered about an unbuilt source. If a
    # sixth source ever needed a THIRD mechanism, that would be the architecturally
    # interesting fact — and none of these three does.
    #
    # DEFERRED vs TARGET is not decoration. TARGET means next; DEFERRED means not
    # next and not blocked on us. Genie is DEFERRED because no Databricks
    # environment exists to build against (a dependency we do not control), while
    # Confluence and Slack-as-a-source are TARGET: buildable today, just unbuilt.
    "CONF":   (6, 4, "confluence.png", "Confluence (Atlassian)",
               "spaces, pages — CONSENT path, same as GitHub", "ext", "target"),
    # SLACK-AS-A-SOURCE IS A DIFFERENT COMPONENT FROM THE SLACK FRONT DOOR at
    # (0,1), and drawing one tile for both would be the subtlest error available
    # here: reading Slack messages as knowledge and receiving a question from Slack
    # are different integrations, on different sides of the diagram, with different
    # tokens and different scopes. Two tiles, and the caption on each says which is
    # which. See the note; the customer's own diagram conflates them.
    "SLACKSRC": (6, 5, "slack.png", "Slack as a SOURCE",
                 "channels, threads — NOT the front door tile at left",
                 "ext", "target"),
    "GENIE":  (6, 6, "databricks-genie.png", "Databricks Genie (MCP)",
               "MCP-native — governed by Unity Catalog, per-user", "ext",
               "deferred"),
}

# (src, dst, label, style, label-nudge-x, label-nudge-y, curve, steps)
# style: "solid" | "dashed" (a control/credential hop, not the request path)
#        | "target" (exists only in the target architecture)
# `curve` bows an arrow (matplotlib rad) so it can route AROUND a tile sitting on
# the straight line between its endpoints. arrows_through_tiles() traces the real
# curve, so bowing is verified rather than assumed.
# EDGE STYLE CARRIES THE ARCHITECTURAL CLAIM, so it is chosen deliberately:
#   "solid"     the primary NO-CONSENT request path — what the demo exercises
#   "extension" the consent path, which exists and works but is not the point
#   "target"    not built yet
#   "dashed"    a control/credential hop, not the request path
#
# `steps` NUMBERS THE TWO END-TO-END FLOWS, and is the reason the diagram can be
# read as a story rather than a wiring map:
#
#   Na  FLOW A — answering from Google Drive with NO consent screen (GREEN)
#   Nb  FLOW B — answering from GitHub, which DOES prompt once (ORANGE)
#
# Each flow is numbered from 1 independently, so either can be traced start to
# finish on its own. An edge in BOTH carries both badges (e.g. "1a 1b") and is drawn
# NEUTRAL DARK, not green or orange — it cannot be one flow's colour without lying
# about the other. That neutral run is doing real work: it shows the two flows are
# IDENTICAL until a source is reached, so consent is a property of the source, not
# of the architecture. The divergence point is visible rather than argued.
#
# COLOUR AND LINESTYLE ARE INDEPENDENT AXES, deliberately: colour = which flow,
# linestyle = whether it is built (dashed = target, not yet built). A green dashed
# arrow is therefore "flow A, not built yet" — not a contradiction.
#
# WHERE THE `label` OF A NUMBERED EDGE IS DRAWN: in the step tables under the diagram,
# NOT on the arrow. With both flows plus a return lane there were thirty-odd captions
# competing for the same middle band, and a reader could not tell which line any given
# phrase belonged to — the numbering was supposed to make the diagram traceable and the
# text was undoing it. So each numbered edge carries a BADGE ONLY, and its sentence
# appears once, under its own flow's heading, keyed by that badge. Unnumbered
# infrastructure edges keep their inline label: there is no number to look up, and
# there are few enough of them not to crowd anything.
#
# That also lifts a length limit. These labels used to be terse because they were drawn
# between tiles; in a table they can say what actually happens, which is why several
# read as sentences below.
EDGES = [
    # --- login (once per day, no per-source consent) — SHARED by both flows ---
    ("USER", "CLI", "The user asks a question in plain language. No source is named "
     "and no permission is requested.", "solid", 0, 0.10, 0, ("1a", "1b")),
    ("SLACK", "CLI", "future front door", "dashed", -0.24, -0.14, 0, ()),
    ("CLI", "OKTA", "Device-code login against Okta — ONCE for the session, not once "
     "per source.", "solid", -0.46, 0.12, 0, ("2a", "2b")),
    ("OKTA", "CLI", "Okta returns a signed JWT whose `email` claim is the user's "
     "Workspace primary address.", "solid", 0.34, -0.46, 0, ("3a", "3b")),

    # --- the primary spine, left to right along row 2 — still SHARED ---
    ("CLI", "IGW", "The CLI invokes the agent, presenting that JWT as a Bearer "
     "token.", "solid", 0, 0.10, 0, ("4a", "4b")),
    # NUDGES ON THE FOUR INFRASTRUCTURE LABELS BELOW ARE COMPUTED, NOT INHERITED.
    # They came from v1 unchanged and orphaned_edge_labels() flagged all four the
    # moment it existed — worst was this file's own vault->Okta caption, 3.39in from
    # its arrow and sitting directly beneath the green 6a curve, so it read as
    # labelling the wrong edge entirely (confirmed by eye in the PNG, not just by the
    # number). Each is now derived by walking the traced Bezier and offsetting 0.26in
    # along the local normal at a tile-clear point.
    ("IGW", "OKTA", "validate JWT via JWKS", "dashed", 0.34, -0.99, 0, ()),
    ("IGW", "RT", "The front door validates the JWT against Okta's JWKS and forwards "
     "the request to the runtime.", "target", 0, 0.10, 0, ("5a", "5b")),
    ("RT", "BR", "reason / tool-use loop", "solid", -0.26, -0.84, 0, ()),
    # DASHED because it does not happen yet. Drawn solid, this arrow claimed a
    # read/write that no code performs — the tile is created and injected, and never
    # called. Dashed keeps the wire visible (turning memory on is a code change, not an
    # infrastructure one) without asserting traffic that is not there.
    # +0.26 x / -0.62 y: the computed +0.25 y put it on the runtime's own sub-caption
    # (this edge drops straight down from RT, so "just off the line" is also "just
    # under the tile"). Pushed further down the arrow instead of sideways — sideways
    # would have walked it toward the Bedrock column.
    ("RT", "MEM", "read / write events (not wired up)", "dashed",
     0.26, -0.62, 0, ()),
    # THE LOAD-BEARING EDGE OF THE WHOLE DIAGRAM. The agent re-verifies the token
    # against Okta's JWKS *itself* rather than trusting the front door, because the
    # authorizer's guarantee is narrower than what impersonation requires: it proves the
    # token is AUTHENTIC, not that the holder owns the mailbox named in `email`. An
    # issuer permitting self-signup will genuinely sign a token claiming a colleague's
    # address, so gates 2 and 3 below have no counterpart at the front door and an
    # unverified claim here would be a domain-wide read, not a bug.
    #
    # FLOW A ONLY (6a), and that is the point: this step exists BECAUSE hop 2 is
    # impersonation. Flow B never needs it — GitHub is called with a token GitHub
    # itself issued after the user consented, so no claim of ours selects the user.
    # Numbering it 'a' only is what makes the extra cost of the no-consent path
    # visible instead of implied.
    # SIGNATURE IS THE FIRST OF THREE GATES, and saying only "signature" understated
    # the step. identity_claims.py additionally requires `email_verified is True` (not
    # merely "not False": an issuer that omits the claim must not pass) and that the
    # address sit inside ALLOWED_EMAIL_DOMAINS, failing closed on an empty allowlist.
    # Both are load-bearing rather than defensive: a correctly-SIGNED token for a
    # self-signed-up ceo@ address would otherwise become step 8a's impersonation
    # subject, and the tests that prove it (tests/test_identity_claims.py) use tokens
    # that are all validly signed. A one-gate description of a three-gate check invites
    # someone to "simplify" the other two away.
    ("RT", "OKTA", "The agent re-verifies the JWT ITSELF before naming a user — it "
     "does not trust the front door. Three gates, all fail-closed: signature/issuer/"
     "audience against Okta's JWKS, then `email_verified` must be TRUE, then the "
     "address must be inside the configured domain allowlist. Only flow A needs this: "
     "it is the step that makes impersonation safe.", "dashed",
     -1.10, -0.34, 0.22, ("6a",)),
    ("RT", "TGW", "The agent calls its tools, carrying the verified identity — never "
     "a user id the caller could name for itself.", "target", 0, 0.10, 0,
     ("7a", "6b")),

    # --- FLOW A: Google Drive, no consent screen ---
    # Two hops by two mechanisms, drawn as two arrows on purpose. Collapsing them
    # into one would assert a single credential spans Okta and Google. It does not.
    # Bowed BELOW the vault (positive rad here), not through it: the Drive path does
    # not touch the token vault at all — there is no vaulted token to fetch. Routing
    # this arrow through the vault tile would assert exactly the mechanism this
    # architecture replaces. Curvature chosen by tracing the Bezier, not by eye.
    ("TGW", "GSTS", "A JWT assertion is signed with the Google service-account key, "
     "with `sub` = the email VERIFIED in step 6a. Google, not us, decides what that "
     "subject may read.", "solid", 1.10, -0.62, 0.28, ("8a",)),
    ("GSTS", "GDRIVE", "Google returns an access token scoped to THAT ONE USER. No "
     "consent screen: the Workspace admin granted this once, domain-wide.", "solid",
     0.90, 0, 0, ("9a",)),
    ("SM", "TGW", "service-account key", "dashed", -0.16, -0.30, 0, ()),
    # rad=+0.24, RE-TRACED after _edge_paths() replaced the old tracer. The previous
    # -0.16 was traced between tile CENTRES, but matplotlib draws between the anchor
    # points where the line leaves each tile — a shorter chord, so the same rad bows
    # LESS, and this edge was in fact clipping the token vault the whole time. The
    # shared tracer measures what is drawn, and immediately failed on it.
    # -0.18..-0.40 and +0.18..+0.40 are both clear; positive is chosen because it bows
    # ABOVE the spine, on the same side as this edge's own label, and stays inside the
    # AgentCore zone whose box it terminates in (+0.32 and up arc out over the top of
    # that box and dive back in, which reads as leaving and re-entering the boundary).
    ("GDRIVE", "TGW", "Drive returns only documents this user can already open — "
     "ACL-trimmed by Google itself, not filtered by us afterwards.", "solid",
     -1.05, 0.62, 0.24, ("10a",)),

    # --- Layer 1: the Okta-guarded MCP server (true propagation) ---
    # 7b AND 8b ARE TWO ERAS, NOT TWO CONSECUTIVE STEPS OF ONE STORY, and reading them
    # as a sequence produces nonsense: "exchange the token for one the target accepts,
    # then discover there is no token and ask the user to consent." Worse, that exact
    # combination — an exchange-configured source returning a consent URL — is what
    # agent.py's `_capture` classifies as a POLICY FAILURE and deliberately refuses to
    # show a user (FR-5a). So the caption has to name which era it belongs to. 7b is the
    # target MCP layer, drawn dashed; 8b is what GitHub does today, because GitHub
    # answers the exchange grant with `unsupported_grant_type` and cannot be reached
    # this way at all.
    ("TGW", "VAULT", "TARGET STATE, not today's GitHub path: the user's Okta JWT is "
     "exchanged (RFC 8693) for a token minted for the target — same identity, no user "
     "interaction, no 8b. Reaching a source this way is what removes the consent step "
     "entirely; GitHub cannot, so it takes 8b instead.", "target",
     0, 0.10, 0, ("7b",)),
    # Long haul back to the IdP, bowed clear of the whole middle row. Traced, not
    # eyeballed: rad=-0.34 clipped RT and the tools gateway, and +0.28..+0.45 clipped
    # Bedrock/CloudWatch/Secrets Manager.
    #
    # +0.65, and the criterion is not just "hits no tile". The previous -0.50 was
    # clear until columns 2-6 shifted right to separate the trust boundaries, and
    # then clipped the ingress gateway. Re-tracing found four tile-clear bands, but
    # tile-clear is not sufficient: every negative one dives BELOW the spine and
    # spends 25-64% of its length inside the AgentCore zone box, drawing a hop to the
    # IdP as though it were routed through managed AgentCore internals. +0.65 bows
    # ABOVE the spine instead (its lowest point is 7.25in, the spine is at 7.05in),
    # touches no tile, and is 3% inside the zone. Middle of the 0.50..0.80 band, so a
    # future layout nudge does not silently re-break it.
    # LABEL MOVED IN V2, arrow untouched. At (-1.30, 0.34) the caption sat 3.39in from
    # its own 0.65-bowed arc and directly under the green 6a curve — so the single most
    # confusable phrase on the canvas ("token-exchange: no user interaction", which is
    # M3) appeared to annotate the DELEGATION edge, which is M2 and the one thing M3
    # must not be confused with. Now on its own arc, and the mechanism is stated by the
    # M3 badge rather than by loose prose.
    ("VAULT", "OKTA", "token-exchange: no user interaction", "target",
     3.20, 1.80, 0.65, ()),
    ("VAULT", "MCP", "Okta token minted FOR the MCP server", "target",
     -0.30, -0.20, 0, ()),

    # --- FLOW B: the consent EXTENSION — GitHub, drawn off the spine ---
    # 8b is where a human first gets interrupted, and nothing in the OBO flow ever
    # does — which is the entire reason both flows are drawn. Everything before it is
    # shared; this is where they diverge.
    # THE REQUEST DOES NOT WAIT HERE, and the earlier wording ("the vault redirects
    # their browser") said it did. CONSENT_WAIT_SECONDS = 0 and NoWaitTokenPoller mean
    # the invocation returns immediately with the consent URL in `auth_required`; the
    # URL travels back down the return lane (11b-14b) and client/ask.py is what opens
    # it. Drawing a mid-request browser redirect hid the most surprising property of
    # this flow: a first-time user's question is answered ungrounded and only the
    # RE-ASK is grounded, so a single invocation never walks 1b to 14b.
    ("VAULT", "GHAPP", "The vault has no token for this user yet, so it issues a "
     "consent URL. The request does NOT block — the URL returns to the client, which "
     "opens GitHub's consent screen.", "extension",
     0.66, 0.30, 0, ("8b",)),
    # THE HUMAN. Every other arrow in this diagram is a machine calling a machine, so a
    # reader can trace the whole consent flow and still not see WHERE A PERSON IS ASKED
    # TO DO SOMETHING — which is the one operational difference between the two flows
    # and the entire reason both are drawn. 8b above is the redirect (software moving a
    # browser); this is the user reading a screen and clicking Approve. Without it the
    # diagram argues consent is costly while showing nobody paying it.
    #
    # DELIBERATELY UNNUMBERED ("b*"), and this is the interesting part. Numbering it 9b
    # made flow_numbering() fail, correctly: 8b ends at GHAPP and 9b would start at
    # USER, and NOTHING IN THE DIAGRAM CONNECTS VAULT TO USER — so the number promised a
    # hop a reader could not walk. The fix is not to relax the check, because the check
    # was right: the human is not a hop in the machine chain at all. They are a person
    # standing at one end of 8b's round trip, which is why no number fits. So it is
    # drawn at full flow weight in flow B's orange, badged ★ instead of a digit. Being
    # the only ★ on the canvas makes it MORE conspicuous than a digit would, not less.
    #
    # DRAWN FROM 'USER', NOT 'CLI', on purpose: CLI->GHAPP would be the browser making
    # an HTTP request, which is true but is not the thing being pointed at. The claim is
    # about a human's attention, so the arrow starts at the human.
    #
    # SOLID, unlike the other consent edges: it is built and it really happens. The
    # remaining orange edges are dashed because they route through the not-yet-deployed
    # Gateways, and this one touches neither.
    #
    # rad=-0.50 arcs it OVER the whole diagram rather than through it, which is also the
    # honest route: this hop is out of band — it leaves the product entirely and comes
    # back. Traced, not eyeballed: -0.42 clips CloudWatch and -0.60 clips Okta;
    # -0.45..-0.58 are tile-clear, and -0.50 puts the apex at 13.97in, above every zone
    # box in otherwise empty canvas. It is the only edge here that rises above the body,
    # so draw() sizes the canvas from traced edge paths as well as zone boxes — see
    # _edge_paths(). Its text is a legend row like every other numbered step, so the
    # nudge is irrelevant and left at zero.
    # THE WORDING IS LOAD-BEARING: "the only human action ON THIS DIAGRAM, and it belongs
    # to THIS FLOW ALONE". An earlier version read "the one human action in either flow",
    # which says the opposite of what is meant — "either" reads as "each of the two", so
    # it implied the OBO flow has a human step too, when having NONE is the entire claim.
    # It also contradicted the diagram's own encoding, where a step both flows perform is
    # badged neutral dark and this one is badged flow B's orange.
    ("USER", "GHAPP", "★  THE ONLY HUMAN ACTION ANYWHERE ON THIS DIAGRAM — and it "
     "exists in the CONSENT FLOW ONLY. The user reads a consent screen and clicks "
     "Approve. Once per source, then never again — but it is a PERSON, and the delegation flow "
     "asks nobody, at any step.",
     "solid", 0, 0, -0.50, ("b*",)),
    # "VAULTED AFTER THEY APPROVED" WAS FALSE, and expensively so. Approving does not
    # vault anything: AgentCore redirects the browser to the client's return URL with a
    # `session_id`, and the token is vaulted only when the client calls
    # CompleteResourceTokenAuth with it (client/consent.py). Miss that call and GitHub
    # lists the app as authorized while the session stays IN_PROGRESS forever and this
    # step's precondition is never met — the exact failure that cost a long debugging
    # session, and one this arrow's own caption used to assert away.
    #
    # THAT COMPLETION HOP IS NOT DRAWN, which is the one honest gap left in flow B. It
    # would be two arrows, GHAPP -> CLI (the redirect carrying session_id) then
    # CLI -> VAULT (the completion call), which is a renumber of everything from here
    # down and a very long traced arrow across the canvas. Recorded here and in the
    # NOTE rather than approximated: a single arrow implying the vault fills itself is
    # how the wording above went wrong in the first place.
    ("VAULT", "GH", "GitHub is called under the user's OWN OAuth token — vaulted not "
     "by the approval itself but by the client completing the session afterwards.",
     "extension", 0.72, -0.12, 0, ("9b",)),
    # rad=+0.15, not +0.20: at +0.20 the '10b' badge sat ON the MCP server's caption
    # ("Layer 1: validates an Okta token..."), which no check saw because _overlaps()
    # measured the badge's GLYPHS while the collision was with its painted CIRCLE.
    # Traced across the range: the badge is caption-clear only in -0.20..+0.15
    # (below that the arc clips the VAULT tile, above it the MCP caption), so +0.15 is
    # the largest bow that still clears — keeping the arc visibly distinct from the
    # near-straight orange edges around it.
    ("GH", "TGW", "GitHub returns only what this user's own grants allow — same "
     "trimming property as flow A, reached by consent instead.", "extension",
     -1.30, -0.50, 0.15, ("10b",)),

    # --- RETURN PATH: the answer travelling back to the user ---
    # WHY THIS IS DRAWN AT ALL. A diagram that stops at the data source shows how a
    # request is authorized but not what the user actually receives, and the claim
    # being made here is about the ANSWER ("trimmed to what THEY can see"). Without
    # the return leg a reader has to assume the trimmed result survives the trip
    # back — which is exactly the step where a careless implementation would merge
    # another user's cached content, so it is the leg worth showing.
    #
    # The two flows CONVERGE here and share it, so every return edge carries both
    # badges and is drawn neutral. That convergence is the point: the answers are
    # indistinguishable by the time they reach the user, which is what makes consent
    # a property of the source rather than of the product. Numbering continues from
    # each flow's own count (11a.. / 11b..) so either can still be traced alone.
    # rad=-0.50 on ALL FOUR return edges: they bow BELOW the forward spine so the
    # request and the response are two visible lanes rather than one crowded line.
    # -0.30 was not enough — the labels and badges landed on the forward arrows and
    # the overlap check reported eleven collisions. Traced: at -0.50 each return leg
    # sits ~0.6in clear of the spine and touches no tile.
    ("TGW", "RT", "The passages come back to the agent per-source, ALREADY trimmed. "
     "Nothing here re-widens them.", "solid",
     0, -0.92, -0.50, ("11a", "11b")),
    # Grounding and the confidence band are computed HERE, from what came back — not
    # from what was asked. That is why this is a step and not an arrowhead.
    ("RT", "IGW", "The agent grounds its answer in those passages and attaches "
     "citations and a confidence band.", "target",
     0, -0.92, -0.50, ("12a", "12b")),
    ("IGW", "CLI", "The answer returns over the invocations endpoint.", "solid",
     0, -1.02, -0.50, ("13a", "13b")),
    ("CLI", "USER", "The user reads an answer they can check against its citations — "
     "and cannot tell which flow produced it.", "solid",
     0, -0.92, -0.50, ("14a", "14b")),

    # --- SOURCES 3-5: the fan-out that makes the architecture a PLATFORM ---
    # UNNUMBERED ON PURPOSE, and this is the load-bearing decision in v2. A number
    # would enrol these in flow A or flow B, and flow_numbering() would be right to
    # reject it: 11a already runs TGW->RT, so a "12a" leaving TGW for Confluence
    # promises a sequence that forks, which is not a sequence. More importantly the
    # claim here is not temporal — it is that each new source SLOTS INTO one of the
    # two flows already drawn, taking the same numbered path from step 1 to the
    # gateway and diverging only at the last hop. Unnumbered-but-labelled says
    # exactly that; a third numbered flow would say the opposite.
    #
    # STYLE = "target" so they render dashed: none of the three is built. Their
    # colour therefore comes from the style fallback (AgentCore purple) rather than
    # a flow colour, which is correct — an unbuilt source belongs to no flow yet.
    #
    # NO INLINE PROSE ON THESE THREE — they carry a MECHANISM BADGE instead (MECH_OF
    # above), and the badge is why. Three sources fanning out of one gateway into a
    # narrow column put three captions in the same band, and each had to be nudged so
    # far to avoid the others that it ended up nearer a different arrow than its own:
    # "which line does this text belong to" became unanswerable, which is the exact
    # failure the numbered step badges were introduced to fix on the main flows. So
    # the same remedy applies here — a square M1/M2/M3 badge on the line, and the
    # sentence lives once in the mechanism table under the diagram.
    ("TGW", "CONF", "", "target", 0, 0, 0.14, ()),
    ("TGW", "SLACKSRC", "", "target", 0, 0, 0.10, ()),
    ("TGW", "GENIE", "", "target", 0, 0, 0.06, ()),

    # --- build plane (deploy-time, in NEITHER request flow, hence no numbers) ---
    ("ECR", "RT", "container image", "solid", -0.40, 0.14, 0, ()),
    ("S3", "CB", "source.zip", "solid", 0, 0.10, 0, ()),
    ("CB", "ECR", "push arm64 image", "solid", 0, 0.10, 0, ()),
]

# Flow colours. GREEN = the no-consent Google path, ORANGE = the consent path.
# Both are darkened from the "nice" hues: this PNG gets projected and printed, and
# a mid-green label on white is unreadable in both. Checked for deuteranopia too —
# green/orange separate by lightness here, not by hue alone, so the two flows stay
# distinguishable in greyscale.
FLOW_A = "#1a7f37"      # no consent (Google Drive)
FLOW_B = "#c8791d"      # consent (GitHub) — same amber the EXTENSION tag uses
FLOW_BOTH = "#232f3e"   # shared prefix: cannot be either colour without lying
MECH_C = "#1f5fa8"      # mechanism badges — blue, deliberately NEITHER flow colour

# ---- THE THREE MECHANISMS -------------------------------------------------------
# THERE ARE THREE, NOT TWO, and v1 of this diagram obscured it by drawing two flows
# and calling one of them "OBO". agent.py's SOURCES table has TWO ORTHOGONAL axes —
# `credential` (VAULTED_OAUTH | DELEGATED_SUBJECT) and, for vaulted sources only,
# `flow` (USER_FEDERATION | ON_BEHALF_OF_TOKEN_EXCHANGE) — and the reachable
# combinations are three distinct ways a user's identity arrives at a source:
#
#   M1  VAULTED_OAUTH + USER_FEDERATION          the user consents, once per source
#   M2  DELEGATED_SUBJECT                        a domain-wide service account
#                                                impersonates a verified claim
#   M3  VAULTED_OAUTH + ON_BEHALF_OF_TOKEN_EXCHANGE   RFC 8693 exchange (XAA-style)
#
# ⚠️ THE WORD "OBO" IS OVERLOADED, AND THAT IS THE WHOLE CONFUSION. In agent.py it
# names M3 specifically (`_FLOW_OBO = "ON_BEHALF_OF_TOKEN_EXCHANGE"`). v1 of this
# diagram used it for the GOOGLE DRIVE path, which is M2 — a different mechanism with
# a different credential, a different issuer and a different failure mode. So this
# version does not use the bare word at all: the green flow is the DELEGATION flow,
# and "token exchange" always means M3. Reviewers asked "is that three paths?" of the
# v1 PNG, which is the diagram failing rather than the reader.
#
# M2 AND M3 BOTH AVOID A CONSENT SCREEN BUT ARE NOT INTERCHANGEABLE. M2 needs a
# domain-wide key we hold and guard (hence the three fail-closed gates at 6a); M3
# holds no long-lived credential at all, which is why it is the target end state.
# M3 IS A DESTINATION, NOT YET A SOURCE'S HOME: no source uses it today. It is drawn
# on the Okta-guarded MCP server because FR-34 requires a source be movable between
# paths by configuration alone — so M3 is what sources migrate TO.
#
# (id, colour, heading, which sources)
MECHANISMS = [
    ("M1", FLOW_B, "CONSENT  —  3-legged OAuth; the user approves once per source",
     "GitHub (BUILT) · Confluence · Slack-as-a-source · Databricks Genie. "
     "AgentCore Identity vaults a refresh token. The only mechanism containing a "
     "human, and the ★ step is where they act."),
    ("M2", FLOW_A, "DELEGATION  —  a domain-wide service account impersonates a "
     "VERIFIED claim",
     "Google Drive / Docs (BUILT). No consent screen exists. Okta cannot mint a "
     "token Drive accepts, so the subject comes from a signature-verified Okta "
     "claim — which is why steps 6a and 8a exist and have no counterpart in M1."),
    ("M3", CAT["agent"], "TOKEN EXCHANGE (RFC 8693 / Cross-App Access)  —  no human, "
     "no long-lived credential",
     "Okta-guarded MCP server (TARGET). No source uses this yet; it is where "
     "sources MIGRATE, by configuration (FR-34), not by code. This is what "
     "agent.py calls ON_BEHALF_OF_TOKEN_EXCHANGE — the strict meaning of 'OBO' "
     "here, and NOT the green flow above."),
]

# Which mechanism an edge demonstrates, keyed by (src, dst). Drawn as a SQUARE badge
# so it cannot be mistaken for a round step badge: a step badge answers "when", a
# mechanism badge answers "how", and conflating the two is what the inline prose
# labels did. Only the last hop to a source carries one — the mechanism is decided
# at the moment identity leaves the gateway, and tagging earlier shared hops would
# claim they differ per mechanism, which is precisely what they do not do.
MECH_OF = {
    ("TGW", "GSTS"): "M2",      # the JWT-assertion hop: delegation, in one place
    ("VAULT", "GHAPP"): "M1",   # where the consent screen is issued
    ("VAULT", "MCP"): "M3",     # the exchanged token, minted for the target
    ("TGW", "CONF"): "M1",
    ("TGW", "SLACKSRC"): "M1",
    # GENIE IS M1, NOT M3, and this is a correction worth recording: an earlier draft
    # of v2 labelled it "OBO / token exchange" on the strength of Genie being
    # MCP-native. MCP-native says how the TOOL is invoked, not how identity reaches
    # it — the requirements addendum puts Databricks Genie on the 3-legged path with
    # a vaulted refresh token, and nothing has been verified against a Databricks
    # token endpoint (there is no environment yet, which is why it is DEFERRED).
    # Asserting M3 would have claimed a consent-free integration nobody has tested,
    # for the source least able to prove it.
    ("TGW", "GENIE"): "M1",
}

# A mechanism badge is drawn in ITS MECHANISM'S OWN COLOUR, matching the table row
# below, so M1 badges are the same amber as the consent flow and M2 the same green as
# the delegation flow. Derived from MECHANISMS rather than restated: a badge whose
# colour disagreed with its table row would teach the wrong key.
MECH_COLOUR = {mid: colour for mid, colour, _h, _w in MECHANISMS}


def _in_flow(steps, flow):
    """Does this edge belong to `flow`? Handles numbered and unnumbered steps.

    A step is "3a" (numbered) or "b*" (in the flow, deliberately outside its
    numbering — the consent click; see the USER->GHAPP edge). Testing endswith()
    alone silently dropped the unnumbered one out of BOTH flows, so the most
    important arrow on the canvas was drawn in a default style colour rather than
    flow B's orange — a bug no check catches, because every check reasons about
    numbers and this edge has none.
    """
    return any(s.endswith(flow) or s.startswith(flow) for s in steps)

# (node keys enclosed, label, colour, pad-in-inches, dash pattern)
# Rectangles are DERIVED from the measured extents of their members, so a zone
# encloses exactly its members. zone_intrusions() additionally proves no
# non-member tile lands inside one — that check exists because an early draft
# drew CloudWatch inside the AgentCore box, which claimed CloudWatch is part of
# AgentCore. It is a separate service that AgentCore reports into.
ZONES = [
    (["USER", "SLACK", "CLI", "OKTA"],
     "Outside AWS — user's machine + the ONE identity provider", "#5a6b7b",
     0.30, (0, (3, 3))),
    # Google and GitHub share col 6 but NOT a zone: they are different authorities
    # reached by different mechanisms, and one box around both would imply the
    # no-consent property is a column-wide fact rather than a per-source one.
    (["GDRIVE", "GSTS"],
     "Google Workspace — reached with NO consent screen", "#4285f4", 0.26,
     (0, (3, 3))),
    (["GH", "GHAPP"],
     "GitHub — the consent EXTENSION", "#5a6b7b", 0.26, (0, (3, 3))),
    # THE FIVE SOURCES ARE NOT ONE ZONE, for the same reason Google and GitHub are
    # not: a single box around all of column 6 would say the no-consent property is
    # a column-wide fact. It is per-source, and which mechanism a source takes is
    # the thing this diagram exists to make legible. So the unbuilt three get their
    # own box, labelled by what they have in common — being unbuilt, and needing no
    # new identity machinery — rather than by a vendor name.
    (["CONF", "SLACKSRC", "GENIE"],
     "SOURCES 3-5 — not built; NO new identity machinery required",
     "#5a6b7b", 0.26, (0, (3, 3))),
    # No account ID here on purpose — see account_id_leaks(). Diagrams get pasted
    # into decks and tickets; the region is useful context, the account number is
    # only useful to someone enumerating your resources.
    (["CW", "BR", "SM", "IGW", "RT", "TGW", "VAULT", "MEM", "MCP",
      "S3", "CB", "ECR"],
     "AWS account  ·  us-west-2", "#232f3e", 0.60, (0, (7, 4))),
    (["IGW", "RT", "TGW", "VAULT", "MEM"],
     "Amazon Bedrock AgentCore  (managed — no Lambda, no API Gateway)",
     "#7b27ff", 0.22, (0, (6, 4))),
    (["S3", "CB", "ECR"],
     "Build plane (CI) — CloudFormation stack 1", "#7aa116", 0.22, (0, (6, 4))),
]

# The legend leads with the two flows, because they are what the numbering asks the
# reader to trace. Legend text is rendered INTO the PNG, which ships to customers — so
# it must stay self-explanatory and cite nothing the reader does not have in hand.
LEGEND = [
    ("solid", FLOW_A, "FLOW A (green) — DELEGATION (M2), no consent"),
    ("solid", FLOW_B, "FLOW B (orange) — CONSENT (M1)"),
    ("solid", FLOW_BOTH, "dark = both flows do this step"),
    ("target", "#7b27ff", "dashed = not yet built (incl. M3 token exchange)"),
    ("dashed", "#5a6b7b", "thin, labelled inline = infrastructure, not a step"),
    # Two badge SHAPES, and the legend has to say so or a reader assumes the square
    # ones are just more step numbers. Round = when, square = how. The swatch draws
    # the two SHAPES rather than an arrow: every other legend row is a line colour
    # that appears in the body, so an arrow here would assert a fourth flow colour
    # that exists nowhere on the canvas.
    ("badges", MECH_C, "round badge = STEP (when) · square = MECHANISM (how)"),
]

# The two step tables, keyed by badge. (flow letter, heading, colour, sub-heading.)
# The headings name the MECHANISM rather than the destination, because "Google" and
# "GitHub" are the incidental part — the transferable fact is that one path exchanges a
# token on the user's behalf and the other asks the user. Reading them as a pair is the
# point: identical through step 5, and only one of them ever contains a human.
STEP_TABLES = [
    # "DELEGATION FLOW (M2)", not "OBO FLOW". v1's heading was the origin of the
    # three-vs-two confusion: it labelled the Google Drive path "OBO" while agent.py
    # reserves that name for token exchange (M3), so a reader who knew the code and a
    # reader who knew the diagram disagreed about which mechanism the green arrows
    # showed. The heading now names the mechanism and cites its badge.
    ("a", "DELEGATION FLOW  (M2)  —  identity propagates, NO consent screen  "
     "(green, 1a→14a)",
     FLOW_A,
     "A domain-wide service account impersonates a signature-verified claim, and the "
     "source does its own authorization. Nobody is ever prompted. NOT token exchange "
     "— that is M3, which no source uses yet."),
    ("b", "CONSENT FLOW  (M1)  —  the user is asked once  (orange, 1b→14b + ★)",
     FLOW_B,
     "Identical to the delegation flow through step 5b. It diverges at 8b, and the ★ "
     "step belongs to THIS FLOW ALONE — the only point at which a HUMAN has to act."),
]

# The subtitle is a CONSTANT, and it is wrapped to the canvas width at render time
# rather than written as one long line. v1 drew it unwrapped, which was survivable
# only because it happened to be shorter than the diagram is wide; v2's is longer
# (it has to explain the unnumbered source edges) and it ran 10.19in off the right
# edge on the first render. _clipped() caught it — the same check that caught the
# NOTE overflowing for the same reason. Anything long enough to need wrapping is
# long enough to need MEASURING, so head_h below is derived from the wrapped line
# count instead of assuming one line.
SUBTITLE = (
    "Follow the numbered badges on the arrows; each number is explained in its "
    "flow's table below — DELEGATION FLOW / M2 (green) on the left, CONSENT FLOW / M1 (orange) on "
    "the right. Dark badges are steps BOTH flows perform. The ★ arrow arcing over "
    "the top belongs to the CONSENT FLOW ALONE and is the only place on this diagram "
    "where a human has to act: the user clicks Approve.   ·   NEW IN V2: all five "
    "target sources are drawn (column 6), and the SQUARE badges name which of the "
    "THREE mechanisms each one uses — see the mechanism table below the two flow "
    "tables. There are three, not two: consent (M1), delegation (M2) and token "
    "exchange (M3), and only the first two are built. The unbuilt sources hang off "
    "the Tools Gateway dashed and unnumbered because each reuses a mechanism already "
    "drawn rather than adding a flow. That is the claim: sources are configuration, "
    "not architecture."
)

NOTE = (
    "THE CLAIM THIS ARCHITECTURE MAKES: a user asks ONE question across FIVE systems and gets an answer "
    "trimmed to what THEY "
    "can see, without ever seeing a consent screen. Identity propagates; authorization stays with the "
    "source system.\n"
    "•  ONE identity provider. Okta authenticates the user once (device-code). Every per-user decision "
    "downstream traces back to a token Okta signed — including the Drive read, whose impersonation "
    "subject is the 'email' claim of that token.\n"
    "•  THERE ARE THREE MECHANISMS, NOT TWO, and the word 'OBO' is what obscures it — so this diagram no "
    "longer uses that word loosely. M1 CONSENT (3-legged OAuth, the user approves once; GitHub, and the "
    "three unbuilt sources). M2 DELEGATION (a domain-wide service account impersonates a signature-verified "
    "claim; Google Drive). M3 TOKEN EXCHANGE (RFC 8693 / Cross-App Access; the Okta-guarded MCP server, and "
    "NO source uses it yet). agent.py reserves the name ON_BEHALF_OF_TOKEN_EXCHANGE for M3 alone, while v1 "
    "of this diagram called the M2 path 'OBO' — two different mechanisms under one name, which is why "
    "reviewers asked whether there were two paths or three. M2 and M3 both avoid a consent screen but are "
    "NOT interchangeable: M2 needs a domain-wide key we hold and guard, M3 holds no long-lived credential "
    "at all, which is why M3 is the end state and M2 is what ships today.\n"
    "•  TWO HOPS, TWO MECHANISMS ON THE DELEGATION PATH — deliberately drawn as two arrows. Okta cannot mint a token Google "
    "Drive accepts: Google requires the assertion be signed by a Google service-account key, and "
    "Workforce Identity Federation returns Cloud tokens, not Workspace ones. So hop 1 (caller -> agent) "
    "is cryptographic Okta validation, and hop 2 (agent -> Drive) is admin-delegated impersonation. "
    "No single credential spans both authorities: Okta is authoritative over who the user is, Google "
    "over what that user may read in Drive.\n"
    "•  WHAT MAKES HOP 2 SAFE: the service account holds DOMAIN-WIDE delegation, so it could read any "
    "mailbox in the domain. The only thing keeping it per-user is that the impersonation subject comes "
    "from a signature-verified Okta claim (agent/identity_claims.py). The subject is read from the "
    "Authorization HEADER — the one value the authorizer authenticated — and never from the request "
    "body, which would be a second identity channel the authorizer never sees. The runtime still "
    "re-validates that token itself rather than trusting the front door, because the authorizer proves "
    "the token is AUTHENTIC and not that its holder owns the mailbox in 'email': an issuer permitting "
    "self-signup would sign a colleague's address quite genuinely. Hence 'email_verified' and the "
    "domain allowlist, neither of which the front door checks.\n"
    "•  FIVE SOURCES, AND THE SOURCE LIST IS CONFIGURATION RATHER THAN ARCHITECTURE — the claim v2 exists "
    "to make. Built: Google Drive (M2 delegation, no consent) and GitHub (M1 consent, once per user). Drawn "
    "and not built: Confluence, Slack-as-a-source AND Databricks Genie, all three on the M1 consent path. "
    "Genie is M1 despite being MCP-native — MCP describes how the TOOL is called, not how identity reaches "
    "it, and nothing has been verified against a Databricks token endpoint. Each "
    "unbuilt source needs its own AgentCore Identity credential provider and its own scopes; NONE needs a "
    "new identity mechanism, a new trust boundary, or a change to how permissions are enforced. That is "
    "why they are drawn unnumbered: they slot into a flow already on this canvas.\n"
    "•  WHAT EACH NEW SOURCE ACTUALLY COSTS, since 'configuration' should not be read as 'free': one "
    "credential provider per source, one env-var pair, and one callback URL pasted by hand into that "
    "vendor's OAuth app — because a credential provider's Name is createOnly, so it cannot be renamed or "
    "re-pointed later. Genie additionally has no environment to build against yet, which is why it is "
    "DEFERRED rather than TARGET: that one is blocked on a dependency, not on effort.\n"
    "•  PERMISSION TRIMMING IS ALWAYS THE SOURCE'S JOB, AT ALL FIVE — never ours, and never a service "
    "account acting for everyone. Drive trims by its own ACLs, GitHub by the user's own grants, Confluence "
    "by space permissions, Slack by channel membership, Genie by Unity Catalog. Our code never filters "
    "results after the fact, which is the property that makes adding a source safe: there is no per-source "
    "authorization logic of ours to get wrong.\n"
    "•  SLACK APPEARS TWICE, and it is two different components, not a duplicate tile. At the LEFT it is "
    "the front door — where a question arrives (still deferred; needs a Business+ plan). At the RIGHT it is "
    "a knowledge SOURCE — channels and threads read as context, on the consent path. Different tokens, "
    "different scopes, different side of the trust boundary. Conflating them is the easiest mistake to make "
    "about this architecture, and the customer's own current-state diagram makes it.\n"
    "•  LAYER 1 (target): an Okta-guarded MCP server reached by RFC 8693 token exchange. This is the "
    "piece that makes the propagation claim cryptographic end to end rather than narrated — the same "
    "Okta identity, exchanged for a token minted FOR that server, with no user interaction.\n"
    "•  THE CONSENT PATH STILL EXISTS, for GitHub, drawn as the EXTENSION above the spine. It is built "
    "and working. It is shown so the contrast is visible: consent is a per-source property, not a "
    "property of the architecture.\n"
    "•  HOW TO READ IT: the arrows carry NUMBERS ONLY and the sentences live in the two tables below, "
    "one per flow. That is deliberate — with both flows plus the return lane, thirty-odd captions "
    "competed for the same middle band and it was impossible to tell which phrase belonged to which "
    "line. Compare the two tables side by side and the structure falls out: steps 1-5 are word-for-word "
    "identical, they diverge only where a source is reached, and they converge again on the return "
    "lane.\n"
    "•  WHERE THE USER HAS TO ACT — the ★ arrow arcing over the diagram from the End user to the GitHub "
    "OAuth App, and it is part of the CONSENT FLOW ONLY. Clicking Approve is a HUMAN action, and it is "
    "the only one anywhere on this canvas; every other "
    "arrow on this canvas is software calling software. It is drawn out of band because that is what it "
    "is: the user leaves the product, reads a consent screen, and comes back. Once per source, then "
    "never again — and the delegation flow has no counterpart to it at any step, which is the claim in the "
    "title, drawn rather than asserted.\n"
    "•  WHAT THIS DIAGRAM DOES NOT DRAW, stated rather than left to be discovered. (1) Approving does not "
    "vault the token. AgentCore redirects the browser back to the client with a session_id, and the token "
    "is vaulted only when the client calls CompleteResourceTokenAuth (client/consent.py); skip it and "
    "GitHub lists the app as authorized while the vault stays empty forever. Those two hops would be "
    "GitHub OAuth App -> CLI -> token vault, and they sit between ★ and 9b. (2) A first-time consent-flow "
    "question is answered UNGROUNDED: the request does not block at 8b, so the consent URL travels back "
    "down 11b-14b and the grounded answer comes from the automatic re-ask — one invocation never walks 1b "
    "to 14b. (3) CloudWatch has no arrows, yet it is where the reason for a refused impersonation goes: "
    "the caller deliberately gets a generic message so that a failed forgery attempt learns nothing.\n"
    "•  No API Gateway, no Lambda, no DynamoDB token table, no KMS — AgentCore Identity's vault holds "
    "per-user tokens, and the two Gateways are drawn but not yet deployed (the agent calls its tools "
    "in-process and the CLI invokes the runtime endpoint directly, so 4a/4b and 13a/13b really terminate "
    "at the Runtime and 5a/5b is performed by its own CUSTOM_JWT authorizer)."
)


class Ruler:
    """Measures rendered text in inches (data units are inches)."""

    def __init__(self):
        self.fig = plt.figure(figsize=(8, 8), dpi=DPI)
        self.rend = self.fig.canvas.get_renderer()

    def size(self, s, fontsize, weight="normal"):
        t = self.fig.text(0, 0, s, fontsize=fontsize, weight=weight)
        bb = t.get_window_extent(self.rend)
        t.remove()
        return bb.width / DPI, bb.height / DPI

    def wrap(self, s, fontsize, width, weight="normal"):
        """Greedy wrap to `width` inches; returns the wrapped string."""
        words, lines, cur = s.split(), [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if cur and self.size(trial, fontsize, weight)[0] > width:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return "\n".join(lines)

    def badge(self, glyph, fontsize):
        """Painted (w, h) of a circled badge in inches — MEASURED, not modelled.

        The circle from `boxstyle="circle,pad=0.22"` is about 0.06in larger than its
        glyphs on every side, and re-deriving that from font metrics would be a second
        implementation of matplotlib's layout. Row pitch and clearance only need to know
        what is actually painted, so ask.
        """
        t = self.fig.text(0, 0, glyph, fontsize=fontsize, weight="bold",
                          bbox=dict(boxstyle="circle,pad=0.22"))
        # The patch's extent is stale until matplotlib syncs it to the laid-out text.
        t.update_bbox_position_size(self.rend)
        bb = t.get_bbox_patch().get_window_extent(self.rend)
        t.remove()
        return bb.width / DPI, bb.height / DPI

    def block(self, s, fontsize, weight="normal", spacing=1.3):
        """Wrapped text plus the (width, height) of the block it will occupy."""
        wrapped = self.wrap(s, fontsize, COL_W, weight)
        lines = wrapped.split("\n")
        line_h = self.size("Ag", fontsize, weight)[1]
        widest = max(self.size(ln, fontsize, weight)[0] for ln in lines)
        return wrapped, widest, line_h * (1 + spacing * (len(lines) - 1))

    def close(self):
        plt.close(self.fig)


def _wrap_bullet(r, line, width):
    """Wrap one NOTE line to `width` inches, hanging-indented under its bullet.

    Without the indent a wrapped bullet's continuation starts hard against the left
    margin and reads as a new bullet, which silently changes how many claims the
    diagram appears to make.
    """
    if not line:
        return line
    prefix = "•  " if line.startswith("•  ") else ""
    body = line[len(prefix):]
    wrapped = r.wrap(body, FS_NOTE, width - r.size(prefix or "Ag", FS_NOTE)[0])
    pad = " " * len(prefix)
    return "\n".join((prefix if i == 0 else pad) + ln
                     for i, ln in enumerate(wrapped.split("\n")))


def _extent(r, key):
    """Measured bounding box of a node: its tile plus both caption blocks."""
    col, row, _icon, cap, sub, _cat, _status = NODES[key]
    x, y = CX[col], CY[row]
    _, cap_w, cap_h = r.block(cap, FS_CAP, "bold")
    _, sub_w, sub_h = r.block(sub, FS_SUB)
    half = max(TILE, cap_w, sub_w) / 2
    return (x - half, y - TILE / 2 - 0.14 - cap_h - sub_h, x + half, y + TILE / 2)


def _anchor(x0, y0, x1, y1, half):
    """Point where the segment leaves a square tile of half-width `half`."""
    dx, dy = x1 - x0, y1 - y0
    if dx == 0 and dy == 0:
        return x0, y0
    scale = half / max(abs(dx), abs(dy))
    return x0 + dx * scale, y0 + dy * scale


def steps_for(flow):
    """Ordered (badge, text, shared?) rows for one flow, DERIVED from EDGES.

    Not a hand-written table. A second list of step descriptions would be a second
    source of truth for the numbering, and the failure mode is silent and certain:
    renumber an edge, forget the table, and the diagram now labels an arrow '7a' while
    the key explains a different hop entirely. Deriving means the two cannot disagree,
    and flow_numbering() already guarantees the sequence is walkable — so the table is
    gap-free and correctly ordered for free.

    `shared` marks a step BOTH flows perform. Those rows are drawn in the neutral
    colour in both tables, which is what shows a reader that flow A and flow B are the
    same system until a source is reached rather than two parallel implementations.
    """
    rows = []
    for _src, _dst, label, _style, _nx, _ny, _c, steps in EDGES:
        for step in steps:
            if not _in_flow((step,), flow):
                continue
            shared = _in_flow(steps, "a") and _in_flow(steps, "b")
            # Unnumbered (the ★) sorts last within its flow; it is an aside to the
            # sequence, not a position in it.
            order = int(step[:-1]) if step[:-1].isdigit() else 10 ** 6
            rows.append((order, step, label, shared))
    return [(s, t, sh) for _o, s, t, sh in sorted(rows)]


FS_STEP = 7.2           # step-table body text
FS_STEP_HEAD = 8.6      # step-table heading
BADGE_COL = 0.42        # width reserved for the badge column, inches
ROW_GAP = 0.055         # vertical gap between wrapped rows


def _badge_glyph(step):
    """(glyph, fontsize) for a step badge. One rule, three call sites.

    The unnumbered consent click is badged ★, not "b*": the marker is bookkeeping for
    flow_numbering(), and printing it would put syntax in front of a reader instead of
    a symbol. Drawn larger — it is the one thing on this canvas a human has to DO, and
    at badge size a star reads as a smudge. Shared here because the arrows, the tables
    and the row-height measurement must all agree; a fourth copy is how the layout
    starts reserving space for a glyph it does not draw.
    """
    return ("★", FS_TAG * 1.5) if step.endswith("*") else (step, FS_TAG)


def _table_layout(r, table_w):
    """Wrap both step tables to `table_w` and return (rows-per-flow, total height).

    Measured, not estimated. The tables sit below the diagram and the canvas has to be
    tall enough for the TALLER of the two — guessing a row count is how a table gets
    clipped by savefig without any check noticing, since a text object that runs off the
    figure overlaps nothing.
    """
    text_w = table_w - BADGE_COL
    head_h = r.size("Ag", FS_STEP_HEAD, "bold")[1]
    sub_h = r.size("Ag", FS_SUB)[1]
    line_h = r.size("Ag", FS_STEP)[1]
    out, heights = [], []
    for flow, _head, _colour, _sub in STEP_TABLES:
        rows, h = [], head_h + 0.10 + sub_h * 2 + 0.20
        for badge, text, shared in steps_for(flow):
            wrapped = r.wrap(text, FS_STEP, text_w)
            n = len(wrapped.split("\n"))
            rows.append((badge, wrapped, shared, n))
            h += _row_h(r, wrapped, badge, line_h)
        out.append(rows)
        heights.append(h)
    return out, max(heights)


def _row_h(r, wrapped, badge, line_h):
    """Pitch of one table row: whichever is taller, the sentence or the BADGE.

    A single-line row is shorter than the circle beside it, so pitching on the text
    alone stacked consecutive badges into each other — every adjacent pair from 9a
    down reported an overlap once the check measured circles instead of glyphs. The
    text is what a reader reads, but the badge is what a reader sees first, and it is
    the taller of the two.
    """
    n = len(wrapped.split("\n"))
    glyph, gsize = _badge_glyph(badge)
    return max(line_h * (1 + 1.28 * (n - 1)),
               r.badge(glyph, gsize)[1]) + ROW_GAP


FS_MECH_HEAD = 8.2      # mechanism-table heading
FS_MECH = 7.2           # mechanism-table body


def _mech_layout(r, width):
    """Wrap the three mechanism rows to `width`; return (rows, total height).

    Same measure-don't-guess rule as _table_layout(): this block sits between the
    legend and the step tables, so an under-measured height silently pushes the
    diagram body down onto it — and a text object drawn over another is the one
    failure _overlaps() does catch, which is why it is worth measuring properly
    rather than relying on that check to find it later.
    """
    head_w = width - 0.62
    line_h = r.size("Ag", FS_MECH)[1]
    head_h = r.size("Ag", FS_MECH_HEAD, "bold")[1]
    rows, total = [], 0.0
    for mid, colour, heading, who in MECHANISMS:
        h_wrapped = r.wrap(heading, FS_MECH_HEAD, head_w, "bold")
        w_wrapped = r.wrap(who, FS_MECH, head_w)
        h = (head_h * (1 + 1.3 * (len(h_wrapped.split("\n")) - 1))
             + line_h * (1 + 1.3 * (len(w_wrapped.split("\n")) - 1)) + 0.20)
        rows.append((mid, colour, h_wrapped, w_wrapped, h))
        total += h
    return rows, total + head_h + 0.18


def _edge_paths(n=200):
    """Every edge's drawn Bezier, in body coordinates. One tracer, three callers.

    arrows_through_tiles() and the curvature notes above both trace `arc3,rad=`
    by hand; so does the canvas sizing below. Three copies of the same three lines
    is how they drift apart, and a sizing routine that disagrees with the renderer
    crops arrows silently — savefig does not complain.
    """
    pos = {k: (CX[s[0]], CY[s[1]]) for k, s in NODES.items()}
    half = TILE / 2 + 0.05
    out = []
    for src, dst, _l, _s, _nx, _ny, curve, _steps in EDGES:
        sx, sy = _anchor(*pos[src], *pos[dst], half)
        ex, ey = _anchor(*pos[dst], *pos[src], half)
        cx = (sx + ex) / 2 + curve * (ey - sy)
        cy = (sy + ey) / 2 - curve * (ex - sx)
        pts = []
        for i in range(n + 1):
            t = i / n
            u = 1 - t
            pts.append((u * u * sx + 2 * u * t * cx + t * t * ex,
                        u * u * sy + 2 * u * t * cy + t * t * ey))
        out.append((src, dst, pts))
    return out


def _zone_rects(r, zone_h):
    """Zone rectangles derived from the measured extents of their members."""
    ext = {k: _extent(r, k) for k in NODES}
    rects = []
    for keys, label, colour, pad, dash in ZONES:
        boxes = [ext[k] for k in keys]
        rects.append((min(b[0] for b in boxes) - pad,
                      min(b[1] for b in boxes) - pad,
                      max(b[2] for b in boxes) + pad,
                      max(b[3] for b in boxes) + pad + zone_h,
                      label, colour, dash, set(keys)))
    return ext, rects


def draw():
    r = Ruler()
    zone_h = r.size("Ag", FS_ZONE, "bold")[1] + 0.08
    _ext, rects = _zone_rects(r, zone_h)

    title_h = r.size("Ag", FS_TITLE, "bold")[1]
    sub_h = r.size("Ag", FS_SUB)[1]
    legend_h = r.size("Ag", FS_SUB)[1] + 0.26

    body_dx = 0.40 - min(rc[0] for rc in rects)     # left-align the body at 0.40
    # A zone's label starts at its left edge and can run wider than the zone
    # itself (the GitHub zone is narrow, its caption long), so the canvas has to
    # clear the label, not just the rectangle.
    right = max(max(rc[2] for rc in rects),
                max(rc[0] + 0.12 + r.size(rc[4], FS_ZONE, "bold")[0] for rc in rects))
    W = right + body_dx + 0.40

    # The subtitle is wrapped to the canvas and the header height comes FROM that
    # wrap, not from a one-line assumption — see SUBTITLE. Ordered after W for the
    # obvious reason: the wrap width is the canvas width, so it cannot be computed
    # before the canvas is.
    subtitle = r.wrap(SUBTITLE, FS_SUB, W - 0.90)
    subtitle_h = sub_h * (1 + 1.4 * (len(subtitle.split("\n")) - 1))
    head_h = title_h + subtitle_h + 0.34

    # THE NOTE IS WRAPPED TO THE CANVAS, not merely measured for height. Only its
    # height used to be computed, so a bullet longer than the diagram is wide ran
    # off the right edge and was cropped by savefig — and the checks could not see
    # it, because an overflowing text object does not overlap anything. Two of the
    # most load-bearing sentences in the PNG (the two-mechanisms explanation and
    # what keeps hop 2 per-user) were truncated mid-clause that way. Each source
    # line wraps independently so the bullet structure survives.
    note = "\n".join(_wrap_bullet(r, ln, W - 0.90) for ln in NOTE.split("\n"))
    note_h = r.size("Ag", FS_NOTE)[1] * (1 + 1.55 * (len(note.split("\n")) - 1))
    # TWO STEP TABLES SIDE BY SIDE, one per flow. Side by side and not stacked: the
    # whole reason to key the text by number is so the two sequences can be COMPARED,
    # and stacked tables make "identical through step 5" something a reader has to
    # verify by scrolling instead of see at a glance.
    table_gap = 0.60
    table_w = (W - 0.80 - table_gap) / 2
    table_rows, table_h = _table_layout(r, table_w)
    # THE BODY'S BOTTOM IS MEASURED, NOT ASSUMED TO BE y=0 — the vertical twin of
    # body_dx above, and v2 is what forced it. v1's lowest row was 0.85 and its
    # captions hung only ~0.2in below the axis, so "clear the tables by 0.62in"
    # happened to be true. v2 adds row 6 at y=-1.05 for the fifth source, and the
    # Databricks Genie tile then dipped straight into the right-hand step table:
    # visually inside it, yet every check green, because a tile is a PATCH and
    # _overlaps() compares TEXT — the caption cleared the nearest table row by a
    # hair while the tile sat in the table's band. Subtracting the lowest measured
    # zone bottom makes the gap mean what it says at any row count.
    # THE MECHANISM TABLE — three rows, one per M-badge, full canvas width. Sits
    # above the two step tables because it answers the coarser question: a reader
    # asking "how many ways in are there" needs three lines, and only then do the
    # per-step sequences mean anything. Measured here so the body can be shifted
    # clear of it, exactly like the step tables.
    mech_rows, mech_h = _mech_layout(r, W - 0.80)
    body_dy = (note_h + legend_h + table_h + mech_h + 0.80
               - min(rc[1] for rc in rects))
    # THE TALLEST THING IS NO LONGER NECESSARILY A ZONE BOX. The consent-click edge
    # (USER->GHAPP, the ★) arcs deliberately above the body, so sizing from `rects`
    # alone cropped both the arc and its label — and nothing failed: _clipped() compares
    # text against the AXES, which savefig then trims to the FIGURE, and an arrow is not
    # text at all. Both the traced paths and the LABEL ANCHORS are therefore included.
    #
    # Labels are measured, not allowed for by a constant. A fixed allowance above each
    # apex was the first attempt and it under-shot by 0.6in, because this label is nudged
    # above its own arc — so the title collided with it and the only reason that was
    # caught is that a collision with TEXT is exactly what _overlaps() sees. An arrow
    # nudged the same way would simply have been cropped in silence.
    line_h = r.size("Ag", FS_EDGE)[1]
    tops = [rc[3] for rc in rects]
    for (src, dst, pts), edge in zip(_edge_paths(), EDGES):
        _ny = edge[5]
        half_h = line_h * (1 + 1.3 * (len(edge[2].split("\n")) - 1)) / 2
        tops.append(max(p[1] for p in pts))
        # Label anchor: the STRAIGHT-line midpoint plus the nudge — which is where
        # draw() actually puts it, curvature notwithstanding.
        tops.append((pts[0][1] + pts[-1][1]) / 2 + _ny + half_h + 0.10)
    H = max(tops) + body_dy + head_h + 0.30

    fig = plt.figure(figsize=(W, H), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")

    ax.text(0.40, H - 0.26,
            "Ripple Knowledge Assistant — component architecture v2: "
            "FIVE sources, THREE mechanisms, ONE identity",
            fontsize=FS_TITLE, weight="bold", va="top")
    ax.text(0.40, H - 0.26 - title_h - 0.10, subtitle,
            fontsize=FS_SUB, color="#555555", va="top", linespacing=1.4)

    def P(key):
        """Node centre in canvas coordinates."""
        col, row = NODES[key][0], NODES[key][1]
        return CX[col] + body_dx, CY[row] + body_dy

    for x0, y0, x1, y1, label, colour, dash, _members in rects:
        x0, x1 = x0 + body_dx, x1 + body_dx
        y0, y1 = y0 + body_dy, y1 + body_dy
        ax.add_patch(FancyBboxPatch(
            (x0, y0), x1 - x0, y1 - y0, boxstyle="round,pad=0.04",
            facecolor="none", edgecolor=colour, linewidth=1.3,
            linestyle=dash, alpha=0.75, zorder=1))
        ax.text(x0 + 0.12, y1 - 0.08, label, fontsize=FS_ZONE, style="italic",
                weight="bold", color=colour, va="top", zorder=2, bbox=MASK)

    # Arrows first, so tiles and captions paint over their ends.
    for src, dst, label, style, nx, ny, curve, steps in EDGES:
        x0, y0 = P(src)
        x1, y1 = P(dst)
        sx, sy = _anchor(x0, y0, x1, y1, TILE / 2 + 0.05)
        ex, ey = _anchor(x1, y1, x0, y0, TILE / 2 + 0.05)
        # FLOW MEMBERSHIP DECIDES THE COLOUR, and it outranks style: the question a
        # reader asks of an arrow here is "which flow am I following", so an edge in
        # flow A is green even when it is also a credential hop. Style still carries
        # built-vs-target, on the linestyle axis. An edge in NEITHER flow keeps its
        # old style colour — those are the deploy-time and infrastructure hops, and
        # colouring them as a flow would put them in a story they are not part of.
        in_a = _in_flow(steps, "a")
        in_b = _in_flow(steps, "b")
        if in_a and in_b:
            colour = FLOW_BOTH
        elif in_a:
            colour = FLOW_A
        elif in_b:
            colour = FLOW_B
        else:
            colour = {"target": CAT["agent"],
                      "extension": "#c8791d"}.get(style, CAT[NODES[src][5]])
        ax.add_patch(FancyArrowPatch(
            (sx, sy), (ex, ey), arrowstyle="-|>", mutation_scale=11,
            # A numbered flow edge is drawn thicker: at this node count a reader finds
            # the two flows by weight before reading a single label. Unnumbered
            # infrastructure hops stay thin so they recede.
            linewidth=1.85 if steps else 1.25,
            color=colour, alpha=0.95, zorder=3,
            linestyle="dashed" if style in ("dashed", "target", "extension")
            else "solid",
            connectionstyle=f"arc3,rad={curve}", shrinkA=0, shrinkB=0))
        # A NUMBERED EDGE IS LABELLED IN ITS STEP TABLE, NOT ON THE CANVAS. Drawing
        # both put ~30 captions in the middle band, where a reader could not tell which
        # phrase belonged to which line — the numbering exists to make the diagram
        # traceable and the inline text was cancelling it out. Unnumbered edges keep
        # their inline label: there is no badge to look them up by.
        if not steps and label:
            lx_, ly_ = (sx + ex) / 2 + nx, (sy + ey) / 2 + ny
            ax.text(lx_, ly_, label, ha="center", va="center", fontsize=FS_EDGE,
                    color="#2b2b2b", zorder=6, bbox=MASK)
        # MECHANISM BADGE — square, so it reads as a different KIND of annotation
        # from the round step badges. A step badge answers "when does this happen";
        # this answers "by which of the three mechanisms". Placed on the arrow's own
        # drawn midpoint for the same reason step badges are: the one job of a badge
        # is to identify which line it belongs to, so it must sit ON that line.
        mech = MECH_OF.get((src, dst))
        if mech:
            bmx = (sx + ex) / 2 + curve * (ey - sy) / 2
            bmy = (sy + ey) / 2 - curve * (ex - sx) / 2
            # Offset along the arrow when a step badge is already on this midpoint
            # (TGW->GSTS carries 8a, VAULT->GHAPP 8b, VAULT->MCP none), so the two
            # badges sit beside each other instead of the square hiding the circle.
            if steps:
                seg = math.hypot(ex - sx, ey - sy) or 1
                bmx += 0.34 * (ex - sx) / seg
                bmy += 0.34 * (ey - sy) / seg
            ax.text(bmx, bmy, mech, ha="center", va="center", fontsize=FS_TAG,
                    weight="bold", color="white", zorder=9,
                    bbox=dict(boxstyle="square,pad=0.30",
                              facecolor=MECH_COLOUR[mech], edgecolor="white",
                              linewidth=0.9))
        if steps:
            # The badge sits ON the arrow's own midpoint, not beside its label. The
            # label may be nudged far off the line to dodge a tile (nx/ny above), and
            # a step number that travels with the label stops identifying which line
            # it belongs to — the one thing a numbered badge exists to do. Curvature
            # is applied so the badge lands on a bowed arrow too, using the same
            # quadratic Bezier midpoint (t=0.5) arrows_through_tiles() traces.
            mx = (sx + ex) / 2 + curve * (ey - sy) / 2
            my = (sy + ey) / 2 - curve * (ex - sx) / 2
            # A REVERSE edge between the same two tiles shares that midpoint exactly,
            # so its badges land on top of the forward edge's (CLI<->Okta: 2a/3a and
            # 2b/3b collided). Nudge perpendicular to the arrow, and only for the
            # reverse direction, so the pair straddles the line it belongs to instead
            # of one badge sitting on the wrong arrow.
            # ONLY when BOTH directions are straight. A bowed reverse edge is already
            # displaced from its partner by its own curvature, and adding the offset
            # on top pushed the return badge back toward the forward one ('1a' vs
            # '14a', the user<->CLI pair, where the return leg bows a clear 0.29in
            # below). The offset exists for the case curvature does not cover:
            # CLI<->Okta, where both arrows are straight and coincident.
            reverse = {(e[0], e[1]): e[6] for e in EDGES}
            if (dst, src) in reverse and curve == 0 and reverse[(dst, src)] == 0:
                # PERPENDICULAR OF THE CANONICAL DIRECTION, not of this arrow. Using
                # this arrow's own vector cancels the offset: the reverse edge flips
                # (ey-sy) as well as the sign, so both badges land on the SAME side
                # and still collide. That is what the first attempt did, and the
                # overlap check caught it unchanged.
                first, second = sorted(((src, dst), (dst, src)))
                (ax0, ay0), (ax1, ay1) = P(first[0]), P(first[1])
                seg = math.hypot(ax1 - ax0, ay1 - ay0) or 1
                off = 0.17 if (src, dst) == first else -0.17
                mx += off * (ay1 - ay0) / seg
                my -= off * (ax1 - ax0) / seg
            # Two badges on one edge (a shared step) stack ALONG the arrow, not
            # horizontally: a horizontal stack walks the second badge sideways off a
            # steep edge and into whatever is beside it — which is how '2a' ended up
            # on top of '3b' after the pair offset above was already correct.
            seg_ = math.hypot(ex - sx, ey - sy) or 1
            ux, uy = (ex - sx) / seg_, (ey - sy) / seg_
            for i, step in enumerate(sorted(steps)):
                bc = FLOW_A if _in_flow((step,), "a") else FLOW_B
                glyph, size = _badge_glyph(step)
                ax.text(mx + i * 0.30 * ux, my + i * 0.30 * uy, glyph,
                        ha="center", va="center",
                        fontsize=size, weight="bold", color="white", zorder=8,
                        bbox=dict(boxstyle="circle,pad=0.22", facecolor=bc,
                                  edgecolor="white", linewidth=0.9))

    for key, (_col, _row, icon, cap, sub, cat, status) in NODES.items():
        x, y = P(key)
        colour = CAT[cat]
        planned, tag, tag_colour = STATUS[status]
        art = os.path.join(ICONS, icon) if icon.endswith(".png") else None
        # Official icons are line art, so they need a light tile to sit on;
        # glyph tiles stay filled with the category colour as before.
        ax.add_patch(FancyBboxPatch(
            (x - TILE / 2, y - TILE / 2), TILE, TILE,
            boxstyle="round,pad=0.02",
            facecolor="#f7f4ff" if art else colour,
            edgecolor=colour, linewidth=1.6,
            linestyle="dashed" if planned else "solid", zorder=5))
        if art:
            pad = 0.10
            ax.imshow(mpimg.imread(art), aspect="auto", zorder=6,
                      extent=(x - TILE / 2 + pad, x + TILE / 2 - pad,
                              y - TILE / 2 + pad, y + TILE / 2 - pad))
        else:
            ax.text(x, y, icon, ha="center", va="center", fontsize=FS_GLYPH,
                    color="white", weight="bold", zorder=6)
        if tag:
            ax.text(x + TILE / 2, y + TILE / 2 + 0.05, tag, ha="right",
                    va="bottom", fontsize=FS_TAG, weight="bold",
                    color=tag_colour, zorder=7)
        cap_t, _cw, cap_h = r.block(cap, FS_CAP, "bold")
        sub_t, _sw, _sh = r.block(sub, FS_SUB)
        ax.text(x, y - TILE / 2 - 0.10, cap_t, ha="center", va="top",
                fontsize=FS_CAP, weight="bold", color="#111111", zorder=6,
                linespacing=1.3, bbox=MASK)
        ax.text(x, y - TILE / 2 - 0.14 - cap_h, sub_t, ha="center", va="top",
                fontsize=FS_SUB, color="#555555", zorder=6,
                linespacing=1.3, bbox=MASK)

    # --- the two step tables, between the legend and the body ---
    # Each row is BADGE + SENTENCE, and the badge is drawn by the same code path as the
    # badges on the arrows (same circle, same colour rule) so a reader matches them by
    # shape without being told to. A table using, say, plain bold "7a" against circled
    # badges on the canvas would make the reader do that mapping consciously.
    line_h_step = r.size("Ag", FS_STEP)[1]
    head_h_step = r.size("Ag", FS_STEP_HEAD, "bold")[1]
    # The mechanism block now sits between the legend and the step tables, so the
    # step tables start above it rather than directly above the legend.
    table_top = note_h + legend_h + mech_h + table_h + 0.62
    for i, ((flow, heading, colour, subhead), rows) in enumerate(
            zip(STEP_TABLES, table_rows)):
        tx = 0.40 + i * (table_w + table_gap)
        ty = table_top
        ax.text(tx, ty, heading, fontsize=FS_STEP_HEAD, weight="bold",
                color=colour, va="top", zorder=7)
        ty -= head_h_step + 0.06
        ax.text(tx, ty, r.wrap(subhead, FS_SUB, table_w), fontsize=FS_SUB,
                color="#555555", va="top", linespacing=1.35, zorder=7)
        ty -= sub_h * (1 + 1.35 * (len(r.wrap(subhead, FS_SUB, table_w)
                                       .split("\n")) - 1)) + 0.16
        # A rule under the heading, in the flow's own colour: it groups the rows below
        # it as one table, which matters when two tables sit side by side and the eye
        # otherwise reads across the gap.
        ax.plot([tx, tx + table_w], [ty + 0.04, ty + 0.04], color=colour,
                linewidth=0.9, alpha=0.55, zorder=4)
        ty -= 0.10
        for badge, wrapped, shared, _nlines in rows:
            # SHARED STEPS ARE NEUTRAL IN BOTH TABLES, matching their arrows. Colouring
            # them per-table would say each flow has its own step 1, when the point is
            # that it is the SAME step — the two flows are one system until 8b.
            bc = FLOW_BOTH if shared else colour
            glyph, gsize = _badge_glyph(badge)
            ax.text(tx + 0.14, ty - line_h_step / 2, glyph, ha="center",
                    va="center", fontsize=gsize, weight="bold", color="white",
                    zorder=8,
                    bbox=dict(boxstyle="circle,pad=0.22", facecolor=bc,
                              edgecolor="white", linewidth=0.9))
            ax.text(tx + BADGE_COL, ty, wrapped, fontsize=FS_STEP,
                    color="#2b2b2b" if not shared else "#4a4a4a", va="top",
                    linespacing=1.28, zorder=7)
            # Same pitch the canvas was SIZED with. Computing it differently here is
            # how a table overruns the height reserved for it and savefig crops the
            # last row without a word of complaint.
            ty -= _row_h(r, wrapped, badge, line_h_step)

    # --- the mechanism table: the key to the SQUARE badges on the canvas ---
    # Drawn with the same square badge the arrows carry, for the same reason the step
    # tables reuse the round one: a reader should match badge to row by shape, without
    # being told. THREE ROWS IS THE HEADLINE — reviewers of v1 asked whether there were
    # two paths or three, so the answer is now a block a reader cannot miss rather than
    # an inference from arrow colours.
    mech_line_h = r.size("Ag", FS_MECH)[1]
    mech_head_h = r.size("Ag", FS_MECH_HEAD, "bold")[1]
    my_ = note_h + legend_h + mech_h + 0.44
    ax.text(0.40, my_,
            "THE THREE MECHANISMS BY WHICH A USER'S IDENTITY REACHES A SOURCE  —  "
            "square badges on the arrows above; every source uses exactly one",
            fontsize=FS_MECH_HEAD, weight="bold", color="#111111", va="top",
            zorder=7)
    my_ -= mech_head_h + 0.14
    for mid, colour, h_wrapped, w_wrapped, row_h in mech_rows:
        ax.text(0.40 + 0.16, my_ - mech_head_h / 2, mid, ha="center", va="center",
                fontsize=FS_TAG, weight="bold", color="white", zorder=8,
                bbox=dict(boxstyle="square,pad=0.30", facecolor=colour,
                          edgecolor="white", linewidth=0.9))
        ax.text(0.40 + 0.62, my_, h_wrapped, fontsize=FS_MECH_HEAD, weight="bold",
                color=colour, va="top", linespacing=1.3, zorder=7)
        ax.text(0.40 + 0.62,
                my_ - mech_head_h * (1 + 1.3 * (len(h_wrapped.split("\n")) - 1))
                - 0.04,
                w_wrapped, fontsize=FS_MECH, color="#333333", va="top",
                linespacing=1.3, zorder=7)
        my_ -= row_h

    # Legend, between the note and the tables.
    lx, ly = 0.40, note_h + 0.30 + legend_h / 2
    for style, colour, text in LEGEND:
        if style == "badges":
            # Miniatures of the real things, at the real colours they carry in the
            # body: a step badge is drawn dark like FLOW_BOTH, a mechanism badge blue.
            ax.text(lx + 0.09, ly, "1", ha="center", va="center",
                    fontsize=FS_TAG - 1, weight="bold", color="white", zorder=5,
                    bbox=dict(boxstyle="circle,pad=0.26", facecolor=FLOW_BOTH,
                              edgecolor="white", linewidth=0.9))
            ax.text(lx + 0.34, ly, "M", ha="center", va="center",
                    fontsize=FS_TAG - 1, weight="bold", color="white", zorder=5,
                    bbox=dict(boxstyle="square,pad=0.26", facecolor=colour,
                              edgecolor="white", linewidth=0.9))
        else:
            # Mirrors the edge renderer above, weight included — a legend that draws a
            # numbered flow at a different thickness than the body teaches the wrong
            # key.
            ax.add_patch(FancyArrowPatch(
                (lx, ly), (lx + 0.42, ly), arrowstyle="-|>", mutation_scale=11,
                linewidth=1.85 if style == "solid" else 1.25, color=colour, zorder=4,
                linestyle="solid" if style == "solid" else "dashed"))
        ax.text(lx + 0.52, ly, text, fontsize=FS_SUB, color="#333333",
                va="center", zorder=4)
        lx += 0.52 + r.size(text, FS_SUB)[0] + 0.55

    ax.text(0.40, note_h + 0.20, note, fontsize=FS_NOTE, color="#333333",
            va="top", linespacing=1.55, zorder=7)
    r.close()

    problems = ([("OVERLAP", f"{a!r}  <->  {b!r}") for a, b in _overlaps(fig, ax)]
                + [("CLIPPED", f"{a!r}  —  {b}") for a, b in _clipped(fig, ax)])
    fig.savefig(OUT, facecolor="white")
    plt.close(fig)
    return problems


def _painted_box(t, rend):
    """What a text object actually COVERS, which for a badge is not its glyphs.

    A step badge is two or three characters inside an opaque circle
    (`boxstyle="circle,pad=0.22"`), and the circle is ~0.06in larger than the glyphs
    on every side. Measuring the glyphs let '10b' pass with 1.3px of clearance over
    the MCP server's caption while its circle covered that caption by 8.5px — a
    collision visible in the PNG with every check green. Masks are deliberately
    different: a MASK bbox exists to blank the arrow running behind a label, so it is
    sized for the line it hides, not for what it may sit beside; measuring it would
    fail pairs of captions that are set perfectly well.
    """
    box = t.get_bbox_patch()
    if box is not None and type(box.get_boxstyle()).__name__ == "Circle":
        # The patch's own extent is stale until matplotlib syncs it to the laid-out
        # text; without this it reports the position from before the draw.
        t.update_bbox_position_size(rend)
        return box.get_window_extent(rend)
    return t.get_window_extent(rend).expanded(0.98, 0.86)


def _overlaps(fig, ax):
    """Pairs of text objects whose painted areas intersect (glyphs/badges, not masks)."""
    rend = fig.canvas.get_renderer()
    items = [(t.get_text().replace("\n", " / ")[:44], _painted_box(t, rend))
             for t in ax.texts]
    hits = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i][1].overlaps(items[j][1]):
                hits.append((items[i][0], items[j][0]))
    return hits


def _clipped(fig, ax):
    """Text objects extending past the canvas, i.e. cropped by savefig().

    A DIFFERENT FAILURE FROM OVERLAP, and invisible to _overlaps(): text that runs
    off the edge collides with nothing, so every check passed while two of the NOTE
    block's most important sentences were cut mid-clause in the shipped PNG. The
    canvas width was derived from the zone rectangles only, and the note's width was
    never measured at all. Caught by eye once; caught here from now on.
    """
    rend = fig.canvas.get_renderer()
    W, H = (v * fig.dpi for v in fig.get_size_inches())
    bad = []
    for t in ax.texts:
        bb = t.get_window_extent(rend)
        # 1px tolerance: an exactly-flush box is fine, and float rounding in the
        # text extent should not fail an otherwise correct render.
        if bb.x0 < -1 or bb.y0 < -1 or bb.x1 > W + 1 or bb.y1 > H + 1:
            over = max(bb.x1 - W, W and 0 - bb.x0, bb.y1 - H, 0 - bb.y0)
            bad.append((t.get_text().replace("\n", " / ")[:56],
                        f"{over / fig.dpi:.2f}in outside the canvas"))
    return bad


def account_id_leaks():
    """Any 12-digit run in the diagram text — i.e. an AWS account ID.

    This PNG is meant to be shared (decks, tickets, the customer comparison), so
    an account ID baked into it travels further than intended. Region and service
    names are fine; the account number is not.
    """
    strings = [label for _keys, label, *_ in ZONES] + [NOTE]
    for spec in NODES.values():
        strings += [spec[3], spec[4]]
    strings += [e[2] for e in EDGES]
    return [m for s in strings for m in re.findall(r"\b\d{12}\b", s)]


def trust_boundary_violations():
    """Nodes whose column contradicts the trust boundary the zones assert.

    Cols 2-5 are inside the AWS account; cols 0-1 and 6 are outside it. Placing
    a node in the wrong column would make the dashed account rectangle enclose
    something that is not in the account — the one error in this diagram that
    would actively mislead a reader about the security model.
    """
    aws_cols = {2, 3, 4, 5}
    inside = {k for keys, label, *_ in ZONES if label.startswith("AWS account")
              for k in keys}
    bad = []
    for key, (col, *_rest) in NODES.items():
        if (col in aws_cols) != (key in inside):
            where = "cols 2-5 (in-account)" if col in aws_cols else "cols 0-1/6 (external)"
            listed = "listed in" if key in inside else "absent from"
            bad.append(f"{key}: sits in {where} but is {listed} the AWS account zone")
    return bad


def zone_intrusions():
    """Tiles that fall inside a zone box they are not a member of.

    A reader takes containment literally: a tile inside the AgentCore box is a
    part of AgentCore. An early draft put CloudWatch inside it, asserting that
    CloudWatch is an AgentCore component rather than a separate service AgentCore
    reports into. Nested zones are exempt — the build plane legitimately sits
    inside the account box.
    """
    r = Ruler()
    zone_h = r.size("Ag", FS_ZONE, "bold")[1] + 0.08
    _ext, rects = _zone_rects(r, zone_h)
    r.close()
    nested = {"AWS account", "Outside AWS"}          # zones that contain others
    hits = []
    for x0, y0, x1, y1, label, _c, _d, members in rects:
        if any(label.startswith(p) for p in nested):
            continue
        for key, (col, row, *_rest) in NODES.items():
            if key in members:
                continue
            x, y = CX[col], CY[row]
            if x0 < x < x1 and y0 < y < y1:
                hits.append((key, label))
    return hits


def flow_numbering():
    """Step numbers that do not form two complete, gap-free, connected flows.

    NUMBERS INVITE A READER TO TRACE, so a wrong number is worse than none: it
    promises a path that does not exist. Four ways it can lie, all checked here
    because none is visible in the rendered PNG:

      - a GAP (1a, 2a, 4a) — the reader hunts for a step that was never drawn;
      - a DUPLICATE (two edges both labelled 5b) — "which 5b?" has no answer;
      - a BREAK (step N+1 starts somewhere the reader cannot have got to) — the
        arrows are numbered as a sequence but do not actually join up, which is the
        failure a diagram this dense makes easiest to introduce and hardest to see;
      - a flow that does not START at the user, i.e. step 1 begins mid-diagram.

    WHAT COUNTS AS CONNECTED. Step N+1 may start at step N's destination OR at its
    source. The second is not a loosened rule, it is request/response: step 6a is the
    runtime calling Okta to re-verify a signature, and the runtime is still the actor
    afterwards — control returns to it, so 7a legitimately starts at RT. Same shape
    for 8b, where consent completes and the vault proceeds. What remains rejected is
    the case that actually misleads: step N+1 starting at a node the reader has no
    path to, which is a numbered sequence that cannot be walked.

    Continuity is also checked PER FLOW, over that flow's own steps only. Once the
    flows share a prefix, "the next step" is ambiguous by construction — 5a/5b's
    successor is 6a in one story and 6b in the other.

    AN UNNUMBERED FLOW EDGE ("b*") is exempt from continuity but still belongs to the
    flow for colouring. That is the consent click: a human standing at one end of the
    redirect's round trip, not a hop between two components. Numbering it made this
    check fail — 8b ends at the OAuth App while the click starts at the user, and no
    edge joins the vault to the user — and the check was right, so the number went
    rather than the rule. Marked rather than silently skipped: `_` would let a typo'd
    "5x" vanish from continuity checking altogether.
    """
    bad = []
    for flow in ("a", "b"):
        seq = {}
        for src, dst, _l, _s, _nx, _ny, _c, steps in EDGES:
            for step in steps:
                if not step.endswith(flow) or step.startswith(flow):
                    continue           # not this flow, or deliberately unnumbered
                if not step[:-1].isdigit():
                    bad.append(f"flow {flow}: step {step!r} is neither a number "
                               f"nor the unnumbered marker {flow + '*'!r}")
                    continue
                n = int(step[:-1])
                if n in seq:
                    bad.append(f"flow {flow}: step {step} is used twice "
                               f"({seq[n][0]}->{seq[n][1]} and {src}->{dst})")
                seq[n] = (src, dst)
        if not seq:
            bad.append(f"flow {flow}: no steps at all")
            continue
        nums = sorted(seq)
        missing = [n for n in range(1, max(nums) + 1) if n not in seq]
        if missing:
            bad.append(f"flow {flow}: gap at step(s) "
                       f"{', '.join(f'{n}{flow}' for n in missing)}")
        if nums[0] != 1:
            bad.append(f"flow {flow}: starts at {nums[0]}{flow}, not 1{flow}")
        for n in nums:
            if n + 1 in seq and seq[n + 1][0] not in seq[n]:
                bad.append(
                    f"flow {flow}: {n}{flow} runs {seq[n][0]}->{seq[n][1]} but "
                    f"{n + 1}{flow} starts at {seq[n + 1][0]}, which is neither "
                    "end of it — the sequence cannot be walked")
    return bad


def zone_overlaps():
    """Zone rectangles that intersect without one containing the other.

    A DIFFERENT FAILURE FROM zone_intrusions(), which asks whether a TILE is in the
    wrong box. This asks whether two BOXES collide — and every tile can be assigned
    correctly while the boundaries still cross, so the other check passes.

    It matters most at exactly the place it went wrong: the "Outside AWS" box
    overlapped the "AWS account" box by 0.10in down 6.9in of height, because Okta's
    caption is wider than its tile and zone boxes are sized from rendered text, not
    from the grid. Two trust boundaries drawn passing through each other is the most
    consequential thing this diagram can get wrong — it says the user's machine is
    partly inside the AWS account — and at full-canvas zoom a tenth of an inch is
    invisible. Nesting is legitimate and exempt: the build plane belongs inside the
    account box.
    """
    r = Ruler()
    zone_h = r.size("Ag", FS_ZONE, "bold")[1] + 0.08
    _ext, rects = _zone_rects(r, zone_h)
    r.close()

    def contains(a, b):
        return a[0] <= b[0] and a[1] <= b[1] and a[2] >= b[2] and a[3] >= b[3]

    bad = []
    for i, ra in enumerate(rects):
        for rb in rects[i + 1:]:
            a, b = ra[:4], rb[:4]
            ox = min(a[2], b[2]) - max(a[0], b[0])
            oy = min(a[3], b[3]) - max(a[1], b[1])
            if ox > 0 and oy > 0 and not contains(a, b) and not contains(b, a):
                bad.append((f"{ra[4][:34]!r} vs {rb[4][:34]!r}",
                            f"{ox:.2f}in x {oy:.2f}in"))
    return bad


def arrows_through_tiles():
    """Edges whose drawn path passes through a tile that is not an endpoint.

    The text-overlap detector cannot see this: the arrow would be drawn *under*
    an unrelated component, which reads as a connection that does not exist.
    Curved edges are traced along the same quadratic Bezier matplotlib's
    `arc3,rad=` produces, so bowing an edge around a tile is verified, not assumed —
    by _edge_paths(), which the canvas sizing uses too. Shared deliberately: when this
    check and the sizing traced separately, either could be right about a path the
    other got wrong.
    """
    pos = {k: (CX[spec[0]], CY[spec[1]]) for k, spec in NODES.items()}
    pad = TILE / 2 + 0.05
    hits = []
    for src, dst, pts in _edge_paths(n=200):
        for key, (bx, by) in pos.items():
            if key in (src, dst):
                continue
            if any(abs(px - bx) < pad and abs(py - by) < pad for px, py in pts):
                hits.append((f"{src}->{dst}", key))
    return hits


def orphaned_edge_labels(limit=0.55):
    """Inline edge labels drawn too far from the arrow they describe.

    THE DEFECT THIS FILE'S HEADER WARNS ABOUT, now checked instead of only narrated.
    Labels anchor to the STRAIGHT-line midpoint plus a nudge, while a bowed arrow is
    drawn along its Bezier — so on a curved edge the two separate, and a caption can
    end up in open canvas describing nothing. Every existing check stays green: the
    label overlaps no text (that is why it was moved) and is not clipped (it is well
    inside the canvas). It is only visible by reading the PNG, which is how the three
    v2 source labels shipped 0.76-0.86in adrift on the first render.

    Distance is to the NEAREST POINT ON THE WHOLE TRACED PATH, not to the midpoint.
    Measuring from the midpoint was the first version and it was wrong in a way worth
    recording: a label may legitimately sit anywhere ALONG its arrow, so on the long
    bowed vault->Okta edge a caption resting near one end measured 4.20in from the
    centre while sitting right on the line. Four such edges were flagged, all of them
    correctly set. A check that fires on good placement gets its limit widened until
    it fires on nothing, so the metric had to change instead. Reuses _edge_paths(),
    the same tracer draw() and arrows_through_tiles() use.

    Only edges carrying an inline label are checked (a numbered edge's text lives in
    its step table, so there is nothing on the canvas to strand). The tolerance is
    deliberately loose — a label SHOULD sit clear of the line, just near it.
    """
    paths = {(src, dst): pts for src, dst, pts in _edge_paths(n=200)}
    pos = {k: (CX[spec[0]], CY[spec[1]]) for k, spec in NODES.items()}
    half = TILE / 2 + 0.05
    bad = []
    for src, dst, label, _style, nx, ny, _curve, steps in EDGES:
        if steps or not label:
            continue                     # badge-only edge: no inline label to strand
        sx, sy = _anchor(*pos[src], *pos[dst], half)
        ex, ey = _anchor(*pos[dst], *pos[src], half)
        lx, ly = (sx + ex) / 2 + nx, (sy + ey) / 2 + ny
        d = min(math.hypot(lx - px, ly - py) for px, py in paths[(src, dst)])
        if d > limit:
            bad.append((f"{src}->{dst} {label[:30]!r}",
                        f"label {d:.2f}in from the nearest point on its arrow "
                        f"(limit {limit})"))
    return bad


def missing_art():
    """Icon files referenced by a node but absent from icons/."""
    return [(k, spec[2]) for k, spec in NODES.items()
            if spec[2].endswith(".png")
            and not os.path.exists(os.path.join(ICONS, spec[2]))]


def missing_glyphs():
    """Tile glyphs absent from the render font, which would draw as tofu boxes.

    matplotlib only warns about these, so check up front rather than shipping a
    PNG with empty rectangles in it.
    """
    try:
        from fontTools.ttLib import TTFont
        from matplotlib.font_manager import FontProperties, findfont
    except ImportError:
        return []          # fontTools absent: skip rather than fail the render
    font = TTFont(findfont(FontProperties(family="DejaVu Sans")))
    covered = set()
    for table in font["cmap"].tables:
        covered |= set(table.cmap)
    return [(key, ch) for key, spec in NODES.items() if not spec[2].endswith(".png")
            for ch in spec[2] if ord(ch) > 127 and ord(ch) not in covered]


CHECKS = [
    (missing_art, "ICON FILES MISSING FROM icons/"),
    (missing_glyphs, "GLYPHS MISSING FROM DejaVu Sans (would render as boxes)"),
    (account_id_leaks, "AWS ACCOUNT ID IN A SHAREABLE DIAGRAM (remove it)"),
    (trust_boundary_violations, "TRUST BOUNDARY VIOLATIONS"),
    (zone_intrusions, "TILES INSIDE A ZONE THEY DO NOT BELONG TO"),
    (zone_overlaps, "ZONE BOXES CROSSING EACH OTHER (two trust boundaries collide)"),
    (flow_numbering, "FLOW NUMBERING IS NOT A TRACEABLE SEQUENCE"),
    (arrows_through_tiles, "ARROWS PASSING THROUGH UNRELATED TILES"),
    (orphaned_edge_labels, "EDGE LABELS STRANDED AWAY FROM THEIR ARROW"),
]


def main() -> int:
    for check, headline in CHECKS:
        problems = check()
        if problems:
            print(f"  {headline}:")
            for p in problems:
                print(f"    {p if isinstance(p, str) else '  ->  '.join(map(str, p))}")
            return 1

    problems = draw()
    print(f"wrote {os.path.relpath(OUT, os.path.dirname(HERE))} "
          f"({os.path.getsize(OUT) // 1024} KB)")
    if problems:
        print(f"  {len(problems)} TEXT PROBLEM(S):")
        for kind, detail in problems:
            print(f"    {kind}: {detail}")
        return 1
    print("  all checks pass; no overlapping or clipped text")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
