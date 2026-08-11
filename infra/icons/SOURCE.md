# Icon provenance

> **Where this sits in the architecture diagram
> (`../architecture-components.png`):** these are the tile glyphs, one per node in
> `render_components.py`'s `NODES`. Nothing here runs; it is the diagram's artwork. A
> missing file is caught by the renderer's `missing_art` check rather than silently
> drawn as a blank box, so adding a node means adding its icon here in the same change.

The PNGs in this directory come from four official sources, one per family:

| Family | Source |
|---|---|
| AgentCore (Runtime, Gateway, Identity, Memory, Observability) | `Agentcore-Bedrock-Icons.pptx`, AWS-provided deck |
| AWS services (Bedrock, S3, CodeBuild, ECR, Secrets Manager, CloudWatch) + Client/User | [AWS Architecture Icons](https://aws.amazon.com/architecture/icons/) package |
| Slack, GitHub, Auth0, Okta, MCP | each vendor's own brand mark |
| Google Drive, Google Docs, Google Cloud IAM | Google's own product-mark and Cloud icon endpoints |

Everything is vendored here on purpose: `render_components.py` must not depend on
a file in someone's `~/Downloads`, and the render has to work on a fresh clone.

## Family 1 — AgentCore deck: how they were extracted

A `.pptx` is a ZIP. Each icon is stored twice — as an SVG and as a high-res PNG
fallback — and the shape names are generic (`Graphic 55`), so the labels come from
matching each picture to the caption text box positioned directly beneath it:

```bash
unzip -o -q Agentcore-Bedrock-Icons.pptx 'ppt/media/*' 'ppt/slides/*' -d /tmp/acicons
# then pair ppt/media/imageN.png with the nearest caption below it, using the
# a:off / a:ext offsets in ppt/slides/slideN.xml
```

The PNG fallbacks are used rather than the SVGs because matplotlib renders PNG
natively and the repo has no SVG rasteriser (no cairosvg / no Node).

### Mapping

Slide 3 of the deck carries the full set at ~390-450 px square, purple `#7B27FF`
line art on transparent backgrounds:

| File here | Source | Deck caption |
|---|---|---|
| `agentcore.png` | `ppt/media/image26.png` | AgentCore |
| `ai-agent.png` | `ppt/media/image24.png` | AI Agent |
| `runtime.png` | `ppt/media/image36.png` | Runtime |
| `gateway.png` | `ppt/media/image42.png` | Gateway |
| `identity.png` | `ppt/media/image44.png` | Identity |
| `memory.png` | `ppt/media/image28.png` | Memory |
| `observability.png` | `ppt/media/image32.png` | Observability |
| `evaluations.png` | `ppt/media/image30.png` | Evaluations |

Also in the deck, not vendored because nothing in this architecture uses them:
Browser tool, Code interpreter, Policy Engine / Agentic Guardrails.

## Family 2 — AWS Architecture Icons package

Downloaded from <https://aws.amazon.com/architecture/icons/> (Q2 2026 release,
`Icon-package_04302026.4705b90f5aa45b019271a2699e9ce9b97b941ee1.zip`, 14 MB).
These ship as SVG only, so they are rasterised with `rsvg-convert` — which *is*
installed here, unlike cairosvg or Node:

```bash
A=Architecture-Service-Icons_04302026
R=Resource-Icons_04302026/Res_General-Icons/Res_48_Light
rsvg-convert -h 384 -a "$A/Arch_Storage/48/Arch_Amazon-Simple-Storage-Service_48.svg" -o s3.png
```

`-h 384` gives a tile roughly matching the AgentCore PNGs; `-a` preserves aspect.

| File here | Source SVG in the package |
|---|---|
| `s3.png` | `Arch_Storage/48/Arch_Amazon-Simple-Storage-Service_48.svg` |
| `codebuild.png` | `Arch_Developer-Tools/48/Arch_AWS-CodeBuild_48.svg` |
| `ecr.png` | `Arch_Containers/48/Arch_Amazon-Elastic-Container-Registry_48.svg` |
| `bedrock.png` | `Arch_Artificial-Intelligence/48/Arch_Amazon-Bedrock_48.svg` |
| `secrets-manager.png` | `Arch_Security-Identity/48/Arch_AWS-Secrets-Manager_48.svg` |
| `cloudwatch.png` | `Arch_Management-Tools/48/Arch_Amazon-CloudWatch_48.svg` |
| `laptop.png` | `Res_General-Icons/Res_48_Light/Res_Client_48_Light.svg` |
| `user.png` | `Res_General-Icons/Res_48_Light/Res_User_48_Light.svg` |

`cloudwatch.png` is vendored but **not** currently used: the CloudWatch tile shows
`observability.png` instead, because that tile represents CloudWatch *and*
AgentCore Observability together and the AgentCore mark is the more specific claim.

## Family 3 — vendor brand marks

Slack, GitHub, Auth0, Okta and Confluence are not AWS services, so no AWS icon
exists for them. Taken from the [gilbarbara/logos](https://github.com/gilbarbara/logos)
collection of full-colour vendor marks, rasterised the same way:

| File here | Source SVG | Note |
|---|---|---|
| `slack.png` | `logos/slack-icon.svg` | Slack's four-petal mark |
| `github.png` | `logos/github-icon.svg` | Octocat mark |
| `auth0.png` | `logos/auth0-icon.svg` | square logomark, not the wordmark |
| `okta.png` | `logos/okta-icon.svg` | radial "aura" logomark, not the `okta` wordmark |
| `confluence.png` | `logos/confluence.svg` | the blue "flowing" glyph; **not** `atlassian.svg`, which asserts the vendor rather than the product |

`confluence.svg` has no `-icon` variant in the collection because the base file is
already square-ish (400x384 after rasterising) rather than a wordmark lockup, so the
suffix rule above does not apply to it.

```bash
curl -sLO https://raw.githubusercontent.com/gilbarbara/logos/main/logos/confluence.svg
rsvg-convert -h 384 -a confluence.svg -o confluence.png
```

### Databricks Genie — first-party, and the product mark rather than the company

`databricks-genie.png` is **Genie's own** icon, taken from Databricks' site, not the
red Databricks corporate logo. The distinction is the rule at the top of this file:
the tile is a Genie space reached over MCP, and the company mark would assert
"Databricks" generally — which is wrong in a diagram whose whole subject is *which
specific source* a token reaches. gilbarbara carries `databricks.svg` (the company
mark) and nothing for Genie, so this one is first-party of necessity as well as
preference:

```bash
curl -sLO https://www.databricks.com/sites/default/files/2026-03/icon-genie.svg
rsvg-convert -h 384 -a icon-genie.svg -o databricks-genie.png
```

| File here | Source URL | Note |
|---|---|---|
| `databricks-genie.png` | `https://www.databricks.com/sites/default/files/2026-03/icon-genie.svg` | Genie product mark, not the Databricks corporate logo |

The `-icon` suffix matters: the un-suffixed files are wide wordmarks that squash
badly on a square tile.

`okta.png` was re-checked against Okta's own current assets: the wordmark at
<https://www.okta.com/sites/default/files/Okta_Logo_BrightBlue_Medium.png> and the
logomark in <https://www.okta.com/favicon.ico>. The favicon carries the same
radial mark as the vendored file (mean alpha delta 0.36/255 after normalising),
so `okta.png` is the **current** Okta logomark, not a retired one. It is the
logomark rather than the lowercase `okta` wordmark — correct for a square tile.

### Auth0, and Okta

The diagram has historically shown the **Auth0** mark because Auth0 is the tenant
this solution actually authenticates against (`dev.env.example`, `AUTH0_DOMAIN`).
Okta is the identity provider in the Cross-App Access design
([`../ARCHITECTURE.md`](../ARCHITECTURE.md)); `okta.png` is vendored for that one. Both
files stay, so whichever identity provider a given revision of the diagram
depicts, the correct mark is on hand.

### MCP — straight from the spec repo, not gilbarbara

`mcp.png` is the Model Context Protocol mark. gilbarbara does carry a
`model-context-protocol-icon.svg` and it renders identically (mean alpha delta
1.22/255), but the mark was taken from the protocol's own repository instead so
the provenance is first-party:

```bash
curl -sLO https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/docs/logo/light.svg
rsvg-convert -h 1600 -a light.svg -o mcp-h1600.png   # 1338x195 lockup: glyph + "Model Context Protocol"
```

`docs/logo/light.svg` is a horizontal *lockup*, so the wordmark has to come off.
The glyph occupies the left ~13% of the 1338-unit viewBox, and there is a clear
run of empty columns between glyph and text (viewBox x 174.3 → 223.7), so the
crop is unambiguous rather than eyeballed: crop to x < 1430 px at `-h 1600`, take
the alpha bounding box, then scale to height 384. Identical geometry appears in
`docs/favicon.svg` (a 180x180 square with the glyph knocked out of a black
rounded rect) — that version was **not** used, because the black plate fights the
light tile `render_components.py` puts behind line art.

| File here | Source | Note |
|---|---|---|
| `mcp.png` | `modelcontextprotocol/modelcontextprotocol` → `docs/logo/light.svg` | glyph only, wordmark cropped off |

## Family 4 — Google product marks and Cloud icons

Google splits its icons across two unrelated endpoints, and picking the wrong one
silently yields a **retired** logo, so both are pinned here explicitly.

### Workspace product marks (Drive, Docs)

Google publishes the Workspace product marks as PNG on `gstatic.com`. There are
two generations at the same path, and the difference matters:

| Path | Generation |
|---|---|
| `product/1x/drive_512dp.png` | **pre-2020**, drop-shadowed — do not use |
| `product/1x/drive_2020q4_512dp.png` | current flat 2020 Q4 refresh — use this |

The `_2020q4_` infix is the whole trick. Verified by rendering both: the
un-infixed Drive file is the old triangle with a shadow and no red facet; the
`2020q4` file is the current four-colour Drive triangle.

```bash
B=https://www.gstatic.com/images/branding/product/1x
curl -sLO $B/drive_2020q4_512dp.png
curl -sLO $B/docs_2020q4_512dp.png
```

These arrive as 512x512 PNGs with the mark letterboxed inside a transparent
canvas, so they are cropped to the alpha bounding box before scaling to height
384 — otherwise the mark renders visibly smaller than every other tile:

```python
im = Image.open(src).convert("RGBA")
im = im.crop(im.split()[3].getbbox())
im.resize((round(im.width * 384 / im.height), 384), Image.LANCZOS).save(dst)
```

`docs_2020q4_512dp.png` ships as 8-bit palette + `transparency`; the `.convert(
"RGBA")` above normalises it so it matches the other RGBA tiles.

| File here | Source URL |
|---|---|
| `google-drive.png` | `https://www.gstatic.com/images/branding/product/1x/drive_2020q4_512dp.png` |
| `google-docs.png` | `https://www.gstatic.com/images/branding/product/1x/docs_2020q4_512dp.png` |

Drive and Docs are kept as **two** files rather than collapsing both into one
"Google Workspace" tile. There is no square Workspace mark to collapse them into:
`gstatic` serves no `workspace_*dp.png` or `gsuite_*dp.png` (both 404), and
gilbarbara's `google-workspace.svg` is a 512x66 wordmark that would squash to
nothing on a 0.74in tile. The only square alternative is the plain Google "G",
which asserts *Google*, not *Drive* — the wrong claim under the rule at the top
of this file.

### Google Cloud IAM

From Google's own Cloud icon set, linked off <https://cloud.google.com/icons>:

```bash
curl -sLO https://services.google.com/fh/files/misc/google-cloud-legacy-icons.zip
unzip -o -q google-cloud-legacy-icons.zip 'identity_and_access_management/*'
rsvg-convert -h 384 -a identity_and_access_management/identity_and_access_management.svg \
  -o google-cloud-iam.png
```

| File here | Source SVG in the package | SVG `<title>` |
|---|---|---|
| `google-cloud-iam.png` | `identity_and_access_management/identity_and_access_management.svg` | `Icon_24px_IAM_Color` |

### No "Service Account" icon exists

The domain-wide-delegation credential is a *service account*, and Google ships
**no service-account icon**. All three packages on <https://cloud.google.com/icons>
were unzipped and searched (`core-products-icons.zip` 89 entries,
`category-icons.zip` 130, `google-cloud-legacy-icons.zip` 432); zero entries match
`service.?account` in any of the three. The nearest candidates were rejected:

| Rejected | Why |
|---|---|
| `permissions/permissions.svg` | a generic person bust — asserts a *user*, and the whole point of a service account is that it is not one |
| `workload_identity_pool/…` | Workload Identity Federation is the *keyless* alternative to a service-account key; using it here would assert the opposite architecture |
| `secret_manager/…` | that is Google's Secret Manager product; this solution holds the credential in **AWS** Secrets Manager (`secrets-manager.png`) |

So `google-cloud-iam.png` is the honest choice: IAM is the service that actually
issues and governs the service account, and the icon is Google's own IAM mark.
It does not claim to be a "service account" icon, because none exists.

These marks are the vendors' trademarks, used here to identify their products in
an internal architecture diagram. That is nominative use; it is not an endorsement.

## Enforcement

`render_components.py` fails via `missing_art()` if a node references an icon file
that is not present here, so a deleted or renamed icon breaks the render rather
than shipping a diagram with a hole in it.
