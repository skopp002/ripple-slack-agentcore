#!/usr/bin/env python3
"""Component architecture diagram for the Ripple solution — AWS-icon style.

Companion to render_diagrams.py. That file renders the two *flows* (deploy time,
request time) as sequence diagrams; this one renders the *components* and how
they connect, in the same shape as the customer's own
Ripple-slack-integration-architecture.png so the two can be compared directly.

This draws the TARGET architecture (`aiplc-docs/discovery/requirements/`), which
includes both AgentCore Gateways. Tiles the MVP has not built yet are dashed and
carry a TARGET tag. What is actually deployed is described in README.md; the
internal build tracker (BUILD-STATUS.md, in the parent workspace) is not part of
this directory.

Every tile carries an official icon (see icons/SOURCE.md): the AgentCore family
from the AgentCore deck, the AWS services from the AWS Architecture Icons
package, and Slack / GitHub / Auth0 from their own brand marks. Nothing is
approximated — a borrowed icon asserts the wrong service.

Layout is measured, not hand-tuned, and five checks fail the render rather than
letting a wrong diagram ship:

    account_id_leaks()          no AWS account ID baked into a shareable PNG
    trust_boundary_violations() nothing sits on the wrong side of the AWS boundary
    zone_intrusions()           no zone box visually contains a non-member tile
    arrows_through_tiles()      no arrow crosses a component it does not connect
    missing_glyphs()            no tile glyph absent from the render font

Data coordinates are INCHES.

Usage:
    python3 infra/render_components.py
"""
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.image as mpimg                     # noqa: E402
import matplotlib.pyplot as plt                      # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "architecture-components.png")
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
}
TILE = 0.74          # icon tile side, inches
COL_W = 2.15         # caption wrap width, inches
FS_GLYPH, FS_CAP, FS_SUB = 10.0, 7.4, 6.5
FS_EDGE, FS_ZONE, FS_TITLE, FS_NOTE = 6.4, 7.0, 13.5, 7.0
FS_TAG = 5.6
MASK = dict(boxstyle="square,pad=0.12", facecolor="white", edgecolor="none")

# --- grid ------------------------------------------------------------------
# Two constraints drive the placement:
#
#  1. TRUST BOUNDARY = COLUMNS. Cols 0-1 are outside AWS (the user, their
#     laptop, Auth0); cols 2-5 are inside the AWS account; col 6 is outside AWS
#     (GitHub). No external component may sit in cols 2-5 and no AWS resource in
#     cols 0-1 or 6 — otherwise the dashed account boundary would be a lie, and
#     that boundary is the whole point of this architecture.
#
#  2. ROW 2 IS THE REQUEST SPINE, read left to right: user -> CLI -> ingress
#     gateway -> runtime -> tools gateway -> vault -> GitHub. Row 1 holds the
#     supporting AWS services the spine calls, row 3 memory, row 4 the build
#     plane. Cells on a line between two connected tiles are left empty;
#     arrows_through_tiles() enforces it and the one unavoidable span
#     (tools gateway -> GitHub, over the vault) is bowed instead.
CX = {0: 1.20, 1: 3.55, 2: 6.45, 3: 9.05, 4: 11.65, 5: 14.25, 6: 17.35}
CY = {0: 9.80, 1: 7.60, 2: 5.10, 3: 3.25, 4: 1.40}

# key: (col, row, icon-or-glyph, caption, sub-caption, category, status)
# icon: "file.png" resolves under icons/; anything else is drawn as a glyph.
# status: "built" (solid) | "target" (dashed + TARGET tag) | "deferred"
NODES = {
    # --- outside AWS: the caller and the IdP (cols 0-1) ---
    "USER":   (0, 2, "user.png", "End user", "Ripple employee", "user", "built"),
    "SLACK":  (0, 1, "slack.png", "Slack front door", "needs Business+ plan",
               "ext", "deferred"),
    "CLI":    (1, 2, "laptop.png", "CLI client", "client/login.py, client/ask.py",
               "user", "built"),
    # Auth0's own mark, not Okta's: Auth0 is the tenant this solution actually
    # authenticates against. Okta appears in the target-state federation
    # addendum (Cross-App Access), which is a different diagram — okta.png is
    # vendored for it, deliberately unused here.
    "AUTH0":  (1, 0, "auth0.png", "Auth0 (OIDC IdP)", "device-code flow, JWKS",
               "ext", "built"),

    # --- inside the AWS account (cols 2-5) ---
    # Inbound auth (JWT validation against the IdP's JWKS) belongs to whichever
    # component is the front door. In the target that is the ingress gateway;
    # in today's MVP it is the runtime's own customJWTAuthorizer. See NOTE.
    "IGW":    (2, 2, "gateway.png", "Ingress AgentCore Gateway",
               "target: Runtime · inbound auth: validate Auth0 JWT · A/B traffic",
               "agent", "target"),
    "CW":     (2, 1, "observability.png", "CloudWatch / AgentCore Observability",
               "runtime + build logs, traces, tagged", "mgmt", "built"),
    # The endpoint is NOT a separate component — it is an addressable alias on
    # the runtime, so it is a sub-caption here rather than its own tile.
    "RT":     (3, 2, "runtime.png", "AgentCore Runtime",
               "agent.py Strands harness, ARM64 · endpoint 'live'", "agent", "built"),
    "BR":     (3, 1, "bedrock.png", "Amazon Bedrock", "Claude (Opus 4.8)",
               "ml", "built"),
    "MEM":    (3, 3, "memory.png", "AgentCore Memory",
               "STM, actorId = Auth0 sub, 90d", "agent", "built"),
    "TGW":    (4, 2, "gateway.png", "Tools AgentCore Gateway",
               "targets: MCP · outbound auth: OAuth on-behalf-of", "agent", "target"),
    "VAULT":  (5, 2, "identity.png", "AgentCore Identity token vault",
               "provider ripple-github — replaces DynamoDB + KMS", "agent", "built"),
    "SM":     (5, 1, "secrets-manager.png", "Secrets Manager",
               "GitHub client secret", "security", "built"),
    "S3":     (2, 4, "s3.png", "S3 build source", "dedicated, TLS-only, versioned",
               "storage", "built"),
    "CB":     (3, 4, "codebuild.png", "CodeBuild", "ARM64 image builder",
               "devtools", "built"),
    "ECR":    (4, 4, "ecr.png", "ECR repository", "immutable tags, scan-on-push",
               "storage", "built"),

    # --- outside AWS: GitHub (col 6) ---
    "GH":     (6, 2, "github.png", "GitHub MCP / API",
               "code, issues, PRs — user-scoped", "ext", "built"),
    "GHAPP":  (6, 0, "github.png", "GitHub OAuth App", "per-user consent",
               "ext", "built"),
}

# (src, dst, label, style, label-nudge-x, label-nudge-y, curve)
# style: "solid" | "dashed" (a control/credential hop, not the request path)
#        | "target" (exists only in the target architecture)
# `curve` bows an arrow (matplotlib rad) so it can route AROUND a tile sitting on
# the straight line between its endpoints. arrows_through_tiles() traces the real
# curve, so bowing is verified rather than assumed.
EDGES = [
    ("USER", "CLI", "asks a question", "solid", 0, 0.10, 0),
    ("SLACK", "CLI", "future front door", "dashed", -0.20, -0.14, 0),
    ("CLI", "AUTH0", "device-code login", "solid", -0.42, 0.10, 0),
    ("AUTH0", "CLI", "user JWT", "solid", 0.30, -0.44, 0),
    ("CLI", "IGW", "invoke + Bearer JWT", "solid", 0, 0.10, 0),
    ("IGW", "AUTH0", "validate JWT via JWKS", "dashed", 0.30, 0.30, 0),
    ("IGW", "RT", "invoke runtime target", "target", 0, 0.10, 0),
    ("RT", "BR", "reason / tool-use loop", "solid", 0.94, 0, 0),
    ("RT", "CW", "logs, metrics, traces", "solid", -0.70, 0.10, 0),
    ("RT", "MEM", "read / write events", "solid", 0.68, 0, 0),
    ("RT", "TGW", "tool call + user context", "target", 0, 0.10, 0),
    ("TGW", "VAULT", "get this user's GitHub token", "target", 0, 0.10, 0),
    ("VAULT", "SM", "read client secret", "dashed", 0.62, 0, 0),
    ("VAULT", "GHAPP", "per-user OAuth consent", "dashed", 0.46, 0.26, 0),
    ("VAULT", "TGW", "token for THIS user only", "solid", 0, -0.24, 0),
    # Bowed OVER the vault: the gateway calls GitHub itself, it does not route
    # the call through the vault. Bowed rather than straight precisely so it
    # cannot be misread as such.
    ("TGW", "GH", "call under the USER's own token", "target", 1.55, 1.02, -0.30),
    ("ECR", "RT", "container image", "solid", -0.36, 0.14, 0),
    ("S3", "CB", "source.zip", "solid", 0, 0.10, 0),
    ("CB", "ECR", "push arm64 image", "solid", 0, 0.10, 0),
]

# (node keys enclosed, label, colour, pad-in-inches, dash pattern)
# Rectangles are DERIVED from the measured extents of their members, so a zone
# encloses exactly its members. zone_intrusions() additionally proves no
# non-member tile lands inside one — that check exists because an early draft
# drew CloudWatch inside the AgentCore box, which claimed CloudWatch is part of
# AgentCore. It is a separate service that AgentCore reports into.
ZONES = [
    (["USER", "SLACK", "CLI", "AUTH0"],
     "Outside AWS — user's machine + identity provider", "#5a6b7b", 0.30, (0, (3, 3))),
    (["GH", "GHAPP"],
     "Outside AWS — GitHub (self-owned OAuth App)", "#5a6b7b", 0.30, (0, (3, 3))),
    # No account ID here on purpose — see account_id_leaks(). Diagrams get pasted
    # into decks and tickets; the region is useful context, the account number is
    # only useful to someone enumerating your resources.
    (["CW", "BR", "SM", "IGW", "RT", "TGW", "VAULT", "MEM", "S3", "CB", "ECR"],
     "AWS account  ·  us-west-2", "#232f3e", 0.60, (0, (7, 4))),
    (["IGW", "RT", "TGW", "VAULT", "MEM"],
     "Amazon Bedrock AgentCore  (managed — no Lambda, no API Gateway)",
     "#7b27ff", 0.22, (0, (6, 4))),
    (["S3", "CB", "ECR"],
     "Build plane (CI) — CloudFormation stack 1", "#7aa116", 0.22, (0, (6, 4))),
]

LEGEND = [
    ("solid", "#232f3e", "built and deployed today"),
    # Legend text is rendered INTO the PNG, which ships to customers — so it must not
    # cite BUILD-STATUS.md, an internal file that lives in the parent workspace.
    ("target", "#7b27ff", "TARGET architecture — not yet built"),
    ("dashed", "#5a6b7b", "control / credential hop, not the request path"),
]

NOTE = (
    "What changed from the current architecture (Ripple-slack-integration-architecture.png):\n"
    "•  API Gateway + Receiver Lambda + Worker Lambda are GONE — inbound auth moves onto AgentCore. "
    "In the target the Ingress Gateway validates the Auth0 JWT and the runtime is locked to it "
    "(allowedWorkloadConfiguration); in today's MVP the runtime's own customJWTAuthorizer does that job, "
    "which is why the runtime is directly invocable right now.\n"
    "•  DynamoDB (KMS-encrypted user tokens) + AWS KMS are GONE — AgentCore Identity's token vault "
    "stores per-user OAuth tokens; no token table to encrypt, rotate or leak.\n"
    "•  MCP fan-out (Atlassian Rovo / Google Drive / Slack / Databricks Genie) still lands on the Tools "
    "Gateway; the MVP wires GitHub only, because it is self-owned so per-user consent needs no tenant admin.\n"
    "•  Preserved guarantee: GitHub is read ONLY under the calling user's own OAuth token, so results are "
    "ACL-trimmed by GitHub itself — the agent can never widen a user's access.\n"
    "•  Added: AgentCore Memory (per-user STM), and every resource inside the account boundary carries "
    "project_name=ripple_slack_assistant natively via CloudFormation.\n"
    "•  MVP delta: both Gateways are drawn but not deployed — the agent currently calls the GitHub tool "
    "in-process and the CLI invokes the runtime endpoint directly."
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

    def block(self, s, fontsize, weight="normal", spacing=1.3):
        """Wrapped text plus the (width, height) of the block it will occupy."""
        wrapped = self.wrap(s, fontsize, COL_W, weight)
        lines = wrapped.split("\n")
        line_h = self.size("Ag", fontsize, weight)[1]
        widest = max(self.size(ln, fontsize, weight)[0] for ln in lines)
        return wrapped, widest, line_h * (1 + spacing * (len(lines) - 1))

    def close(self):
        plt.close(self.fig)


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

    # Canvas derives from the content: a note block whose measured height
    # reserves space so the bullets can never be clipped, plus a legend row.
    title_h = r.size("Ag", FS_TITLE, "bold")[1]
    sub_h = r.size("Ag", FS_SUB)[1]
    note_h = r.size("Ag", FS_NOTE)[1] * (1 + 1.55 * (len(NOTE.split("\n")) - 1))
    legend_h = r.size("Ag", FS_SUB)[1] + 0.26
    head_h = title_h + sub_h + 0.34

    body_dx = 0.40 - min(rc[0] for rc in rects)     # left-align the body at 0.40
    body_dy = note_h + legend_h + 0.34              # lift clear of note + legend
    # A zone's label starts at its left edge and can run wider than the zone
    # itself (the GitHub zone is narrow, its caption long), so the canvas has to
    # clear the label, not just the rectangle.
    right = max(max(rc[2] for rc in rects),
                max(rc[0] + 0.12 + r.size(rc[4], FS_ZONE, "bold")[0] for rc in rects))
    W = right + body_dx + 0.40
    H = max(rc[3] for rc in rects) + body_dy + head_h + 0.30

    fig = plt.figure(figsize=(W, H), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")

    ax.text(0.40, H - 0.26,
            "Ripple Slack Knowledge Assistant — component architecture (target)",
            fontsize=FS_TITLE, weight="bold", va="top")
    ax.text(0.40, H - 0.26 - title_h - 0.10,
            "Bedrock AgentCore replaces the API Gateway + Lambda + DynamoDB token layer. "
            "Numbered deploy-time and request-time flows: see ARCHITECTURE.md.",
            fontsize=FS_SUB, color="#555555", va="top")

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
    for src, dst, label, style, nx, ny, curve in EDGES:
        x0, y0 = P(src)
        x1, y1 = P(dst)
        sx, sy = _anchor(x0, y0, x1, y1, TILE / 2 + 0.05)
        ex, ey = _anchor(x1, y1, x0, y0, TILE / 2 + 0.05)
        colour = CAT["agent"] if style == "target" else CAT[NODES[src][5]]
        ax.add_patch(FancyArrowPatch(
            (sx, sy), (ex, ey), arrowstyle="-|>", mutation_scale=11,
            linewidth=1.25, color=colour, alpha=0.95, zorder=3,
            linestyle="dashed" if style in ("dashed", "target") else "solid",
            connectionstyle=f"arc3,rad={curve}", shrinkA=0, shrinkB=0))
        ax.text((sx + ex) / 2 + nx, (sy + ey) / 2 + ny, label,
                ha="center", va="center", fontsize=FS_EDGE, color="#2b2b2b",
                zorder=6, bbox=MASK)

    for key, (_col, _row, icon, cap, sub, cat, status) in NODES.items():
        x, y = P(key)
        colour = CAT[cat]
        planned = status != "built"
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
        if status == "target":
            ax.text(x + TILE / 2, y + TILE / 2 + 0.05, "TARGET", ha="right",
                    va="bottom", fontsize=FS_TAG, weight="bold",
                    color=CAT["agent"], zorder=7)
        elif status == "deferred":
            ax.text(x + TILE / 2, y + TILE / 2 + 0.05, "DEFERRED", ha="right",
                    va="bottom", fontsize=FS_TAG, weight="bold",
                    color=CAT["ext"], zorder=7)
        cap_t, _cw, cap_h = r.block(cap, FS_CAP, "bold")
        sub_t, _sw, _sh = r.block(sub, FS_SUB)
        ax.text(x, y - TILE / 2 - 0.10, cap_t, ha="center", va="top",
                fontsize=FS_CAP, weight="bold", color="#111111", zorder=6,
                linespacing=1.3, bbox=MASK)
        ax.text(x, y - TILE / 2 - 0.14 - cap_h, sub_t, ha="center", va="top",
                fontsize=FS_SUB, color="#555555", zorder=6,
                linespacing=1.3, bbox=MASK)

    # Legend, between the note and the body.
    lx, ly = 0.40, note_h + 0.30 + legend_h / 2
    for style, colour, text in LEGEND:
        ax.add_patch(FancyArrowPatch(
            (lx, ly), (lx + 0.42, ly), arrowstyle="-|>", mutation_scale=11,
            linewidth=1.25, color=colour, zorder=4,
            linestyle="solid" if style == "solid" else "dashed"))
        ax.text(lx + 0.52, ly, text, fontsize=FS_SUB, color="#333333",
                va="center", zorder=4)
        lx += 0.52 + r.size(text, FS_SUB)[0] + 0.55

    ax.text(0.40, note_h + 0.20, NOTE, fontsize=FS_NOTE, color="#333333",
            va="top", linespacing=1.55, zorder=7)
    r.close()

    overlaps = _overlaps(fig, ax)
    fig.savefig(OUT, facecolor="white")
    plt.close(fig)
    return overlaps


def _overlaps(fig, ax):
    """Pairs of text objects whose rendered boxes intersect (glyphs, not masks)."""
    rend = fig.canvas.get_renderer()
    items = [(t.get_text().replace("\n", " / ")[:44],
              t.get_window_extent(rend).expanded(0.98, 0.86)) for t in ax.texts]
    hits = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i][1].overlaps(items[j][1]):
                hits.append((items[i][0], items[j][0]))
    return hits


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


def arrows_through_tiles():
    """Edges whose drawn path passes through a tile that is not an endpoint.

    The text-overlap detector cannot see this: the arrow would be drawn *under*
    an unrelated component, which reads as a connection that does not exist.
    Curved edges are traced along the same quadratic Bezier matplotlib's
    `arc3,rad=` produces, so bowing an edge around a tile is verified, not assumed.
    """
    pos = {k: (CX[spec[0]], CY[spec[1]]) for k, spec in NODES.items()}
    pad = TILE / 2 + 0.05
    hits = []
    for src, dst, _label, _style, _nx, _ny, curve in EDGES:
        x0, y0 = pos[src]
        x1, y1 = pos[dst]
        # matplotlib Arc3: control point is the midpoint pushed perpendicular by rad.
        cx = (x0 + x1) / 2 + curve * (y1 - y0)
        cy = (y0 + y1) / 2 - curve * (x1 - x0)
        for key, (bx, by) in pos.items():
            if key in (src, dst):
                continue
            # Sample the path; cheap and exact enough at this tile size.
            for i in range(1, 100):
                t = i / 100
                u = 1 - t
                px = u * u * x0 + 2 * u * t * cx + t * t * x1
                py = u * u * y0 + 2 * u * t * cy + t * t * y1
                if abs(px - bx) < pad and abs(py - by) < pad:
                    hits.append((f"{src}->{dst}", key))
                    break
    return hits


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
    (arrows_through_tiles, "ARROWS PASSING THROUGH UNRELATED TILES"),
]


def main() -> int:
    for check, headline in CHECKS:
        problems = check()
        if problems:
            print(f"  {headline}:")
            for p in problems:
                print(f"    {p if isinstance(p, str) else '  ->  '.join(map(str, p))}")
            return 1

    overlaps = draw()
    print(f"wrote {os.path.relpath(OUT, os.path.dirname(HERE))} "
          f"({os.path.getsize(OUT) // 1024} KB)")
    if overlaps:
        print(f"  {len(overlaps)} OVERLAPPING TEXT PAIR(S):")
        for a, b in overlaps:
            print(f"    {a!r}  <->  {b!r}")
        return 1
    print("  all checks pass, no overlapping text")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
