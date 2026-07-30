# Icon provenance

The PNGs in this directory come from three official sources, one per family:

| Family | Source |
|---|---|
| AgentCore (Runtime, Gateway, Identity, Memory, Observability) | `Agentcore-Bedrock-Icons.pptx`, AWS-provided deck |
| AWS services (Bedrock, S3, CodeBuild, ECR, Secrets Manager, CloudWatch) + Client/User | [AWS Architecture Icons](https://aws.amazon.com/architecture/icons/) package |
| Slack, GitHub, Auth0, Okta | each vendor's own brand mark |

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

Slack, GitHub, Auth0 and Okta are not AWS services, so no AWS icon exists for
them. Taken from the [gilbarbara/logos](https://github.com/gilbarbara/logos)
collection of full-colour vendor marks, rasterised the same way:

| File here | Source SVG | Note |
|---|---|---|
| `slack.png` | `logos/slack-icon.svg` | Slack's four-petal mark |
| `github.png` | `logos/github-icon.svg` | Octocat mark |
| `auth0.png` | `logos/auth0-icon.svg` | square logomark, not the wordmark |
| `okta.png` | `logos/okta-icon.svg` | **unused in this diagram** — see below |

The `-icon` suffix matters: the un-suffixed files are wide wordmarks that squash
badly on a square tile.

### Auth0, not Okta

The diagram shows the **Auth0** mark because Auth0 is the tenant this solution
actually authenticates against (`dev.env.example`, `AUTH0_DOMAIN`). Okta appears
only in `aiplc-docs/discovery/requirements/addendum-okta-federated-and-memory.md`,
which describes a target-state Cross-App Access design that is a different
diagram. `okta.png` is vendored ready for that one.

These marks are the vendors' trademarks, used here to identify their products in
an internal architecture diagram. That is nominative use; it is not an endorsement.

## Enforcement

`render_components.py` fails via `missing_art()` if a node references an icon file
that is not present here, so a deleted or renamed icon breaks the render rather
than shipping a diagram with a hole in it.
