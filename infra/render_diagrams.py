#!/usr/bin/env python3
"""Render the ARCHITECTURE.md flows to PNG — no node/npm/mmdc required.

Why not mermaid-cli: it needs node plus a headless Chromium download, and this
box has neither (nor docker). The diagrams are drawn with matplotlib, matching
aiplc-docs/discovery/requirements/render_png.py.

Why a SEQUENCE layout rather than a copy of the Mermaid boxes-and-arrows graph:
both flows are strictly ordered 1..N, so one row per numbered edge is guaranteed
to have no crossing arrows and no label collisions — the free-form graph layout
attempt was unreadable at this node count.

Why the layout is MEASURED rather than hand-tuned: every box width, row height
and label position is derived from the real rendered text extent, so nothing can
overlap regardless of how the labels below are edited. Data coordinates are
INCHES (the axes fills the figure and 1 data unit == 1 inch), which makes
"width of this string in points / 72" directly comparable to layout geometry.
`--check` then verifies the result two ways: edge numbers still match
ARCHITECTURE.md, and no two text objects overlap.

Usage:
    python3 infra/render_diagrams.py            # write both PNGs
    python3 infra/render_diagrams.py --check    # verify parity, no re-render
"""
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                      # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

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

# ---------------------------------------------------------------------------
# Diagram A — deploy time.  Participants in column order.
# ---------------------------------------------------------------------------
A_COLS = [
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
# Column-spanning brackets drawn above the headers.
A_BANDS = [
    ("OP", "TAGS", "operator + deploy engine", "#3f8f43"),
    ("SM", "GH", "outside CFN", "#6b6b6b"),
    ("S3", "ECR", "STACK 1  ripple-foundation  (01-foundation.yaml)", "#2d6ca2"),
    ("MEM", "EP", "STACK 2  ripple-runtime  (02-runtime.yaml)", "#2d6ca2"),
    ("RTLOG", "RTLOG", "service-made", "#c8791d"),
]
# (src, dst, number, label, style)   style: '-' solid, ':' dashed
A_EDGES = [
    ("OP", "SM", 1, "one-time: create ripple/github-oauth holding {\"client_secret\": ...}", "-"),
    ("OP", "CFN", 2, "deploy stack 1  ->  S3, ECR, CodeBuild, build role, log group", "-"),
    ("CB", "CBROLE", 3, "assumes the build role (ECR push + S3 read only)", ":"),
    ("OP", "BUILD", 4, "run the image build — the one step CloudFormation cannot do", "-"),
    ("BUILD", "S3", 5, "zip Dockerfile + requirements.txt + agent/  ->  ripple/source.zip", "-"),
    ("BUILD", "CB", 6, "start_build with IMAGE_TAG = UTC timestamp", "-"),
    ("S3", "CB", 7, "CodeBuild fetches source.zip", "-"),
    ("CB", "ECR", 8, "build linux/arm64 image, push under the timestamp tag", "-"),
    ("CB", "CBLOG", 9, "build logs (30-day retention)", "-"),
    ("BUILD", "OP", 10, "prints IMAGE_TAG=<tag> as the last line", "-"),
    ("OP", "CFN", 11, "deploy stack 2 with ImageTag + Auth0 / GitHub parameters", "-"),
    ("PROV", "SM", 12, "read the client secret via ClientSecretSource=EXTERNAL", "-"),
    ("RT", "RTROLE", 13, "assumes the project-scoped execution role", ":"),
    ("ECR", "RT", 14, "runtime pulls the container image by ContainerUri", "-"),
    ("MEM", "RT", 15, "!GetAtt Memory.MemoryId injected as an env var", ":"),
    ("RT", "OP", 16, "GithubCallbackUrl stack output", "-"),
    ("OP", "GH", 17, "MANUAL: paste the callback URL, or GitHub auth fails", "-"),
    ("RT", "RTLOG", 18, "service creates the log group on first invoke", "-"),
    ("OP", "TAGS", 19, "run the tagger — covers what CFN cannot declare", "-"),
    ("TAGS", "RTLOG", 20, "apply project_name=ripple_slack_assistant", "-"),
    ("TAGS", "SM", 21, "apply the same tag to the Identity-managed secret", "-"),
]
A_PHASES = [(1, "ONE-TIME"), (2, "STACK 1"), (4, "BUILD THE IMAGE  (outside CloudFormation)"),
            (11, "STACK 2"), (19, "POST-DEPLOY TAGGING")]
A_NOTE = ("Runtime endpoint 'live' is created by stack 2 (edge 11) but sends no deploy-time "
          "message — it carries traffic in Diagram B.\n"
          "Edges 2 and 11 target the CloudFormation service, which then creates the [CFN] "
          "lifelines bracketed as STACK 1 / STACK 2.")

# ---------------------------------------------------------------------------
# Diagram B — request time
# ---------------------------------------------------------------------------
B_COLS = [
    ("USER",  "User", "OPS"),
    ("CLI",   "client/login.py\nclient/ask.py", "OPS"),
    ("AUTH0", "Auth0 tenant\ndevice code + JWKS", "EXT"),
    ("EP",    "Runtime endpoint live\nCUSTOM_JWT", "CFN"),
    ("AGENT", "agent/agent.py\nStrands harness", "CFN"),
    ("BR",    "Amazon Bedrock\nClaude model", "CFN"),
    ("VAULT", "AgentCore Identity\ntoken vault ripple-github", "CFN"),
    ("GH",    "GitHub API", "EXT"),
    ("MEM",   "AgentCore Memory\nactorId = Auth0 sub", "CFN"),
]
B_BANDS = [
    ("USER", "CLI", "caller", "#3f8f43"),
    ("EP", "MEM", "inside AWS — CloudFormation-owned", "#2d6ca2"),
]
B_EDGES = [
    ("USER", "CLI", 1, "runs client/login.py", "-"),
    ("CLI", "AUTH0", 2, "device code grant request", "-"),
    ("USER", "AUTH0", 3, "approves in browser (user_code + verification_uri)", ":"),
    # The Auth0 IDENTITY token — cached client-side because the caller needs it to
    # authenticate to the runtime. Distinct from the GitHub OAuth token (edges
    # 9-12), which lives only in the AgentCore Identity vault, never on disk.
    ("AUTH0", "CLI", 4, "returns the user's Auth0 JWT, cached in client/.token.json", "-"),
    ("CLI", "EP", 5, "client/ask.py POSTs the question, JWT as Bearer", "-"),
    ("EP", "AUTH0", 6, "CUSTOM_JWT authorizer validates signature, audience, issuer via JWKS", ":"),
    ("EP", "AGENT", 7, "invokes the container entrypoint", "-"),
    ("AGENT", "BR", 8, "Strands calls the model; may loop with 9-15", "-"),
    ("AGENT", "VAULT", 9, "GetResourceOauth2Token for the calling user", "-"),
    ("VAULT", "GH", 10, "per-user OAuth consent, first time only", "-"),
    ("GH", "VAULT", 11, "user-scoped access token, stored in the vault", "-"),
    ("VAULT", "AGENT", 12, "returns a token scoped to THIS user", "-"),
    ("AGENT", "GH", 13, "github_tool searches code, issues, PRs under that token", "-"),
    ("GH", "AGENT", 14, "returns only repos that user can access — the ACL trim", "-"),
    ("AGENT", "MEM", 15, "events partitioned by actorId = Auth0 sub", "-"),
    ("AGENT", "EP", 16, "cited, confidence-scored answer", "-"),
    ("EP", "CLI", 17, "HTTP response", "-"),
    ("CLI", "USER", 18, "prints the answer with citations", "-"),
]
B_PHASES = [(1, "AUTHENTICATE THE USER  (Auth0 device flow)"),
            (5, "AUTHENTICATED INVOKE"),
            (8, "AGENT LOOP — edges 9-14 are the per-user permission trim"),
            (16, "RESPONSE")]
B_NOTE = ("The runtime endpoint is the JWT-authenticated front door: no ingress gateway.\n"
          "GitHub is never read with a service credential — only with the calling user's own "
          "OAuth token, so GitHub itself does the ACL filtering.")

# --- font sizes (points) ---------------------------------------------------
FS_TITLE, FS_SUB = 13.0, 7.4
FS_HDR, FS_KIND, FS_BAND = 6.5, 4.8, 6.8
FS_PHASE, FS_EDGE, FS_NUM = 6.6, 6.5, 6.8
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


def _draw(cols, edges, bands, phases, note, title, subtitle, out):
    """Sequence diagram: participants are columns, numbered edges are rows.

    Two passes. Pass 1 measures every string and derives the geometry; pass 2
    draws at those coordinates. Nothing is hand-positioned, so editing a label
    cannot introduce an overlap.
    """
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
    title_w, title_h = r.block(title, FS_TITLE, "bold")
    sub_w, sub_h = r.block(subtitle, FS_SUB)
    band_h = r.line_h(FS_BAND, "bold")
    edge_h = r.line_h(FS_EDGE)
    phase_h = r.line_h(FS_PHASE, "bold")

    d = 0.0                                  # depth below the top margin
    d += title_h
    d += 0.07
    sub_d = d
    d += sub_h + 0.20
    band_label_d = d                         # top of the band label
    d += band_h + 0.04
    bracket_d = d                            # bracket top rail
    d += 0.12
    hdr_top_d = d
    d += hdr_h + 0.12

    phase_start = dict(phases)
    y_arrow_d, sep_d, phase_label_d = {}, {}, {}
    for src, dst, num, lbl, style in edges:
        if num in phase_start:
            d += phase_h + 0.05
            sep_d[num] = d
            phase_label_d[num] = d - 0.03    # label sits just above the rule
            d += 0.09
        d += edge_h                          # the edge label
        d += CLEAR + NUM_R
        y_arrow_d[num] = d
        d += NUM_R + 0.13
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
    W = max(cols_right, M + max(title_w, sub_w, note_w, leg_w)) + M
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

    ax.text(M, Y(0), title, fontsize=FS_TITLE, weight="bold", va="top")
    ax.text(M, Y(sub_d), subtitle, fontsize=FS_SUB, color="#555555", va="top")

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

    rr = Ruler()
    for src, dst, num, lbl, style in edges:
        y = Y(y_arrow_d[num])
        x0, x1 = cx[src], cx[dst]
        right = x1 > x0
        sx = x0 + (col_w[src] / 2 + 0.02) * (1 if right else -1)
        ex = x1 - (col_w[dst] / 2 + 0.02) * (1 if right else -1)
        colour = LINE[kind[src]]
        ax.add_patch(FancyArrowPatch(
            (sx, y), (ex, y), arrowstyle="-|>", mutation_scale=10,
            linewidth=1.15, color=colour, zorder=4,
            linestyle="dashed" if style == ":" else "solid",
            shrinkA=0, shrinkB=0))
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
    rr.close()

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
    return out, overlaps


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


def check_edges() -> int:
    """Fail if the PNG edge numbering drifts from ARCHITECTURE.md."""
    with open(DOC, encoding="utf-8") as fh:
        txt = fh.read()
    blocks = re.findall(r"```mermaid\n(.*?)```", txt, re.S)
    if len(blocks) != 2:
        print(f"FAIL: expected 2 mermaid blocks in ARCHITECTURE.md, found {len(blocks)}")
        return 1
    ok = True
    for name, block, edges in (("A", blocks[0], A_EDGES), ("B", blocks[1], B_EDGES)):
        doc_nums = sorted(int(v) for v in re.findall(r'\|"(\d+)[^"]*"\|', block))
        png_nums = sorted(e[2] for e in edges)
        if doc_nums == png_nums:
            print(f"  Diagram {name}: {len(png_nums)} edges match ARCHITECTURE.md")
        else:
            ok = False
            print(f"  Diagram {name}: MISMATCH")
            print(f"    only in ARCHITECTURE.md: {sorted(set(doc_nums) - set(png_nums))}")
            print(f"    only in renderer:        {sorted(set(png_nums) - set(doc_nums))}")
    return 0 if ok else 1


def main() -> int:
    if "--check" in sys.argv:
        return check_edges()

    specs = [
        (A_COLS, A_EDGES, A_BANDS, A_PHASES, A_NOTE,
         "Ripple — Diagram A: Deploy time (CloudFormation)",
         "One row per numbered edge; numbers match the edge table in "
         "infra/ARCHITECTURE.md. Read top to bottom.",
         "architecture-a-deploy.png"),
        (B_COLS, B_EDGES, B_BANDS, B_PHASES, B_NOTE,
         "Ripple — Diagram B: Request time",
         "One row per numbered edge; numbers match the edge table in "
         "infra/ARCHITECTURE.md. Read top to bottom.",
         "architecture-b-request.png"),
    ]
    rc = 0
    for *spec, fname in specs:
        path, overlaps = _draw(*spec, os.path.join(HERE, fname))
        print(f"wrote {os.path.relpath(path, os.path.dirname(HERE))} "
              f"({os.path.getsize(path) // 1024} KB)")
        if overlaps:
            rc = 1
            print(f"  {len(overlaps)} OVERLAPPING TEXT PAIR(S):")
            for a, b in overlaps:
                print(f"    {a!r}  <->  {b!r}")
        else:
            print("  no overlapping text")
    return check_edges() or rc


if __name__ == "__main__":
    raise SystemExit(main())
