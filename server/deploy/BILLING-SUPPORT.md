# Selling and supporting auto-processing: the plan

How a customer buys a monthly plan on the Starfront shop, how the collab
server learns of it, and how they get help. Written 2026-11-11. The server
side of everything here is built and tested but switched off; nothing in
Shopify, Appstle or Zendesk has been created or changed.

## 1. Buying a plan, step by step

1. The customer opens **collab.starfront.space/me** and signs in with Discord.
2. With no plan they see the plans on offer (name, price, storage) and a
   **Subscribe** button on each.
3. Subscribe opens the auto-processing product page on the Starfront shop.
   The link carries a *link code*: their Discord id plus a signature only the
   server can make. A small snippet on the product page tucks that code into
   the order line as a hidden field.
4. They check out as they would for a pier. Appstle bills monthly.
5. The shop tells the server about the payment. The server checks the
   message is really from the shop, reads the link code, and the plan is on:
   My imaging shows "Plan: Hobbyist", their storage, and a **Manage plan**
   link to the Appstle customer portal.
6. Their telescopes can upload their own targets straight away. Without a
   plan, uploads are refused with "needs a plan" (HTTP 402), as today.

Every month the renewal payment extends the plan by a month. If no payment
arrives, the plan runs out by itself (see section 3); the server does not
depend on being told about a cancellation.

### Where the working idea had to change

| The idea | What the docs say | What was built instead |
|---|---|---|
| Carry the Discord id as a **cart attribute** | Cart attributes land on the first order only. What stays with a subscription is the **line item property**. | The code travels as a line item property (`_starfront`). The server looks for it anywhere in a message, so either works. |
| Link straight to checkout with the id attached | Shopify's cart links cannot carry a subscription ("selling plans don't work with cart permalinks"). | The link opens the product page; a snippet (`shopify-link-snippet.liquid`) adds the code to the Add to cart form. |
| A plain Discord id in the link | Anyone could edit it, or mistype it, and pay for a stranger. | The id is signed. A missing or altered code leaves the purchase *unlinked* rather than giving it to the wrong person. |
| One webhook from Shopify/Appstle for create, renew, cancel, failed | Shopify's subscription webhooks only go to the app that owns the subscription (Appstle), not to us. Appstle has its own webhooks with all those events, but lists them under its **Enterprise** plans. | Three ways in, use whichever your plans allow (below). |
| Nightly reconcile against Appstle | Appstle's API can list every subscription with its status and next billing date. It needs an Appstle plan that includes API access. | Built as described; runs once a day and on demand. |

### The three ways the shop can tell the server

| Way | Needs | Tells us |
|---|---|---|
| **A. Shopify order webhooks** (`orders/paid`, `refunds/create`) | Nothing extra. Any Shopify plan. | Paid, renewed, refunded. Cancel and failed payment are learned by the plan simply not being renewed. |
| **B. Appstle webhooks** | Appstle Enterprise (or ask Appstle support to enable it) | Everything, as it happens: created, paid, failed, paused, cancelled, plan changed. |
| **C. Shopify Flow "Send HTTP request"** from Appstle's Flow triggers | A Shopify plan with that Flow action (you already use it for the deposit tickets) | The same events as B. Flow cannot sign a message, so it sends a shared secret instead. |

**Recommendation: start with A plus the nightly reconcile.** It needs no
plan upgrade, both messages are properly signed, and the reconcile fills in
cancellations within a day. Add B later if Appstle includes it. C is the
fallback if the Appstle API is not in your plan either.

## 2. Getting help

**Recommended: a "Get help" form on the server's pages, plus a link to the
help centre.** A customer presses Get help (in the footer of every page, or
next to a failed stack), types what is wrong and the email to answer at, and
presses Send. Zendesk gets a ticket from them containing only their words.
The server adds a private note for the agent:

- Discord name and id
- plan, its state (active, payment late, ended), storage used of allowed
- whether uploads are being refused, and why
- the project: name, id, their file counts, frames waiting or set aside with the reason
- the last stack: failed or not, and the error it reported
- each telescope: online or not, last check-in, what it was doing

Why this one:

- **Automatic tickets for every failure** were weighed and left out. A bad
  night on the stacking machine would open a ticket per customer for one
  cause, and a customer who has not noticed a problem gets an email about
  it. A failed stack shows on their page with Get help one click away, which
  gives the same context only when a person actually wants help.
- **Only a link to the help centre** loses all the context; the agent's
  first reply would be "what is your Discord name and which target?".
- The form is limited to signed-in people, five tickets a day each.

The help centre link sits beside the form, and is shown alone while the
form is switched off.

## 3. The awkward cases

| Case | What happens |
|---|---|
| **Payment failed** | Appstle retries (its "Failed Payment Recovery"). The plan keeps working for a **7 day grace period** after the paid month ends; My imaging says "Your last payment did not go through. Uploads stop on <date>." Then uploads and stacking stop. If a retry succeeds, everything comes back by itself. |
| **Plan ended (any reason)** | Uploads and new stacks are refused. Their images and projects stay visible. The page says they are kept until 90 days after the end. **Nothing is deleted automatically**; the owners' list shows which accounts are past 90 days, and deleting is a decision you make by hand. |
| **Cancel** | The plan runs to the end of the month already paid, then ends as above. |
| **Plan change** | The new plan's storage applies at once. Downgrading below what they store deletes nothing: uploads are refused as "storage full" until they are under. |
| **Discord id missing or wrong** | The purchase is recorded as paid but *unlinked*. The buyer fixes it themselves on My imaging ("Already subscribed?": order number plus the checkout email, both must match). Owners can also link it by hand. |
| **Refund** | A refund of a plan order ends the plan immediately; data is kept like any ended plan. |
| **Pier customers, as an add-on** | Same product and same flow. A lower price is a discount code or a customers-only variant in the shop; the server treats it as the same plan. If you would rather include it with a pier, owners can grant a plan by hand for a number of days (no shop order needed). |
| **Beta testers** | Stay on the beta list as now. If one buys a plan, the plan applies; if it ends, they fall back to beta. |
| **A message arrives twice, or never** | Twice: the second is ignored. Never: the nightly reconcile corrects it, and an unpaid plan lapses by its dates regardless. |

## 4. What you set up by hand

Nothing below has been done. Each is yours to do or approve.

**Shopify**
1. Create the product "Auto-processing" with one variant per plan. Give each
   variant a SKU (for example `SF-AUTO-100`); the server recognises plans by SKU or variant id.
2. In Appstle, attach a monthly subscription plan to it (subscription only, no one-time purchase).
3. Add a Custom liquid block to that product's page and paste in
   `server/deploy/shopify-link-snippet.liquid`.
4. Settings > Notifications > Webhooks: add `Order payment` and `Refund create`,
   both JSON, to `https://collab.starfront.space/api/v1/billing/shopify`.
   Note the signing secret shown on that page.

**Appstle**
5. Create a **read-only API key** (for the nightly reconcile).
6. Failed Payment Recovery: set the final action to **cancel** or **pause**,
   not "skip and keep active" (that would look like a paid month).
7. Optional, if your plan has it: More > Webhook Integration, add
   `https://collab.starfront.space/api/v1/billing/appstle`, note its signing secret.

**Zendesk**
8. Create credentials the server can open tickets with. Best: an OAuth client
   limited to `tickets:write`. Simpler: an API token on a dedicated agent
   (Zendesk API tokens can do anything that agent can, so do not use an admin's).
9. Optional: custom ticket fields for Discord id, plan and project, if you
   want to filter on them.

**On the server (a deploy, when you say so)**
10. Put the secrets in `/var/lib/starfront/billing.env` and `zendesk.env`
    (templates in `server/deploy/*.env.example`), and the plans in
    `/var/lib/starfront/plans.json` (template `plans.example.json`).
11. Turn on `STARFRONT_PERSONAL_UPLOADS=1`, `STARFRONT_BILLING=1`, `STARFRONT_SUPPORT=1`. Each is independent and off by default.
12. Place one real order with a test discount and check it appears at
    `/api/v1/admin/billing`.

## 5. Decisions for you

1. Plan names, prices and storage sizes. The $9 / $19 / $59 proposal is only in the example file.
2. Which Appstle plan are you on? It decides whether way B (webhooks) and the nightly reconcile (API access) are available.
3. Grace period after a failed payment: 7 days?
4. How long to keep data after a plan ends: 90 days? And who presses delete?
5. Refund: end the plan at once (as built), or let it run to the end of the month?
6. Pier customers: discount code, a customers-only variant, or included with the pier?
7. Should My imaging be read-only after a plan ends (as built), or should downloads stop too after some days?
8. Zendesk: OAuth client or a dedicated agent's token? Which group or form should these tickets land in?
9. Should Get help be open to everyone signed in (as built), or to plan holders only?

## 6. Not verified (needs the live accounts)

- No message from the real Shopify, Appstle or Flow has been received. The signature checks follow the published schemes and are tested against messages built to those schemes.
- Whether Appstle keeps the line item property on the subscription and on renewal orders. If it does not, way A still links the first order and matches renewals by customer, but way B alone would leave purchases unlinked.
- The field names in Appstle's subscription list (used by the reconcile) and webhook messages are taken from its docs, not from a real response.
- Whether Shopify's admin-created webhooks sign with the secret shown on the Notifications page (widely reported, not in the official reference).
- Which Appstle plan tiers include webhooks and API access for your account.
- Zendesk: that the private note is accepted as a second step, that ticket creation triggers the customer email you expect, and the custom field ids.
- The exact address of the Appstle customer portal on your store (the "Manage plan" link).
- The new parts of the My imaging page were checked by tests and a script syntax check, not looked at in a browser with real data.
- Nothing was deployed; the droplet runs the same code as before.
