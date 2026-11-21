# Configuration examples

| File | Purpose |
|------|---------|
| `../accounts.example.yaml` | Projects, accounts, regions, collectors, auth |
| `../schemas/accounts.schema.json` | JSON Schema for accounts YAML validation |
| `cloudlens.example.yaml` | Non-secret application settings sketch |

## Accounts configuration

```bash
cp config/accounts.example.yaml config/accounts.yaml
# Edit config/accounts.yaml with real aliases, account IDs, regions, and auth.
export CLOUDLENS_CONFIG_PATH=./config/accounts.yaml
```

`config/accounts.yaml` is gitignored. Never commit account IDs, role ARNs, profile names, or credentials.
