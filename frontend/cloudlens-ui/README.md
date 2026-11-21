# cloudlens-ui

React + Vite + TypeScript + Tailwind UI for AWS CloudLens.

## Scripts

```bash
npm install
npm run dev      # http://localhost:5173
npm run build
npm run preview
```

Set `VITE_API_BASE_URL` (see repo `.env.example`) to point at the API.

```bash
npm test         # vitest (pure graph logic: layout, filtering, search, styling, filenames)
```

## Architecture explorer

The **Architecture** tab renders the graph served by the backend `/graph` endpoints
(see `docs/ARCHITECTURE_DISCOVERY.md`). It is lazy-loaded, so React Flow and ELK are only
downloaded when the tab is opened.

- **Views:** application (entry point or `Key=Value` app tag), VPC, service, account. The
  backend caps nodes per request (`max_nodes=400`); a banner appears when a view is truncated.
- **Grouping:** account › (global | region › VPC › subnet), laid out with ELK `layered` and
  `hierarchyHandling=INCLUDE_CHILDREN` (`src/lib/layout.ts`). Nodes are placed by the
  backend-provided `parent_group`; VPC/subnet headers open their detail when collected.
- **Edges:** verified = solid, inferred = dashed, unknown = dotted; colour = category.
  Security-group/WAF associations are hidden by default because they are not evidence of traffic.
- **Nodes:** dashed border + "not collected" = an external target referenced by an AWS API
  record (for example an IP target or a custom CloudFront origin) that CloudLens did not collect.
  Faded = stale or context-only.
- **Detail panel:** identifiers, placement, freshness, tags, raw `observed_state`, and every
  inbound/outbound edge with its confidence and evidence (API field, collector, observation time).
- **Exports:** Mermaid / SVG / JSON come from the backend for the current query; PNG and SVG
  "as shown" are rendered from the canvas with `html-to-image`. File names include project,
  account, view and a UTC timestamp.

### AWS Architecture Icons (optional, not bundled)

CloudLens does not ship AWS icons. To use the official
[AWS Architecture Icons](https://aws.amazon.com/architecture/icons/), download the asset
package yourself (subject to AWS's terms), copy the SVGs you need into
`public/aws-icons/` (gitignored), and create `public/aws-icons/manifest.json`:

```json
{
  "load-balancer": "Arch_Elastic-Load-Balancing_48.svg",
  "ecs-service": "Arch_Amazon-Elastic-Container-Service_48.svg",
  "rds-db-instance": "Arch_Amazon-RDS_48.svg"
}
```

Keys are CloudLens `resource_type` values; values must be plain `.svg`/`.png` file names
(paths and URLs are ignored). Types without an entry fall back to a neutral text badge.
