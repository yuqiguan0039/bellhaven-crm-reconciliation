# Bellhaven CRM reconciler

Scrapes all paginated Bellhaven community pages, matches locations to CRM accounts, stores durable review decisions in SQLite, and only writes approved proposals.

## Run

Create a local environment file from the safe template:

```bash
cp .env.example .env
```

Replace the placeholder in `.env` with your personal API token, then load it into the current shell:

```bash
source .env
python3 app.py run
python3 app.py serve --port 8080
```

Open `http://127.0.0.1:8080` to inspect the evidence and review proposals. Decisions are keyed by a deterministic fingerprint of action, target, source, and payload, so unchanged findings are not proposed again.

CRM writes are disabled by default. In this mode, refreshes can read the website and CRM, but review decisions are simulations and cannot call CRM POST or PATCH endpoints.

To enable live writes, uncomment this line in the local `.env` file and restart the server:

```bash
export ALLOW_CRM_WRITES='1'
```

In live mode, **Approve and Apply** writes the selected proposal through the CRM API. **Reject** records the decision without changing the CRM. The header shows the last refresh, the next scheduled 7:00 AM Central refresh, and a **Refresh Now** button. Refreshing only reads data and generates proposals; it never approves proposals automatically.

Proposal cards are grouped into existing-account updates, new accounts, CHOW, duplicate deactivation, and website absence. Completed and rejected proposals move into separate history sections.

## Matching and disposition policy

- Normalized exact street address is the primary identity signal, supported by phone, ZIP, city/state, and fuzzy name similarity.
- Website care labels are mapped to the CRM vocabulary (`Memory Support` → `Memory Care`; `Short-Term Rehabilitation & Nursing` → `Skilled Nursing`).
- A website location with no strong match is proposed as a new account.
- A current Bellhaven child absent from the website is marked `Needs Review`; absence alone is insufficient proof of closure or sale.
- Exact-address duplicate accounts are resolved to the record with the strongest revenue/AR continuity. The losing copy is marked `Inactive`, linked using `duplicate_of_account`, and documented in `note` because the API has no merge/delete.
- Parent changes follow the billing SOP: when both `lifetime_revenue > 0` and `outstanding_ar > 0`, the old account keeps its original parent, a new current account is created under Bellhaven, and the old account's `chow_current_account` points to the new id. Otherwise the existing account is re-parented directly.

## Safety

Writes occur only after approval. Before PATCH, the current account is re-read; an already-applied payload becomes a no-op. Both approvals and rejections persist in `review.db`. Do not commit the database or token.

## Test

```bash
python3 -m unittest test_app.py
```
